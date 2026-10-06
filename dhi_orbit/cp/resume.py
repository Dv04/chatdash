"""Limit-resume: one of the three allowed auto-sends.

When a background chat's LAST message is a limit banner ("You've hit your session limit · resets
4:40pm (America/Chicago)") and the reset time has passed, send the context line below into that chat.
Guards, as keepwarm: background chat with a job, not a read-only account, idle (never working or showing a
dialog), the banner is still the last message, not cancelled by the user, the seat's meter does not still
read 100%, at most 2 re-arms per stall, every send verified in the transcript. Mode in cp/config.json
"limit_resume": off / dry-run (log what it would do, once per banner) / on. decide() is the whole policy.
"""
from __future__ import annotations

import threading
import time

from . import db, sources

RESUME_TEXT = "Your limit reset. Continue where you stopped; first say in one line what you were doing."
GRACE_S = 60            # wait this long after the reset time
MAX_ARMS = 2            # a resume that hits the limit again re-arms at most this many times
RETRY_S = 300
MAX_STALL_S = 12 * 3600  # a reset that passed longer ago than this is left to the user (a strip with Resume)

SCHEMA = """CREATE TABLE IF NOT EXISTS cp_resume (
    session_id TEXT PRIMARY KEY, key TEXT, seat TEXT, banner_at REAL, resets_at REAL, arms INTEGER DEFAULT 0,
    status TEXT, detail TEXT, last_at REAL, cancelled INTEGER DEFAULT 0, dry_logged_for REAL);"""


PREF_SCHEMA = """CREATE TABLE IF NOT EXISTS cp_resume_pref (
    session_id TEXT PRIMARY KEY, key TEXT, name TEXT, pref TEXT, set_at REAL);"""
PREFS = ("on", "off")


def _pref_table(path: str | None = None) -> None:
    c = db.connect(path)
    c.executescript(PREF_SCHEMA)
    c.commit()
    c.close()


def prefs(path: str | None = None) -> dict[str, str]:
    """Per-chat overrides of the global limit_resume mode: {session_id: 'on' | 'off'}."""
    _pref_table(path)
    return {r["session_id"]: r["pref"] for r in db.rows("SELECT session_id, pref FROM cp_resume_pref", (), path)}


def set_pref(chat: dict, pref: str | None, path: str | None = None) -> None:
    """pref 'on' resumes this chat even while the global mode is off or dry-run; 'off' never resumes it;
    None returns it to the global mode."""
    _pref_table(path)
    if pref is None:
        db.execute("DELETE FROM cp_resume_pref WHERE session_id=?", (chat["session_id"],), path)
    else:
        db.execute("INSERT OR REPLACE INTO cp_resume_pref(session_id, key, name, pref, set_at) VALUES(?,?,?,?,?)",
                   (chat["session_id"], chat.get("key"), chat.get("name"), pref, time.time()), path)
    db.log_auto("limit_resume_pref", "on", chat["session_id"], chat.get("account"), f"set {pref or 'default'}",
                "the user", None, path)


def effective(global_mode: str, pref: str | None) -> str:
    return "on" if pref == "on" else "off" if pref == "off" else global_mode


def rearm(chat: dict, row: dict | None) -> int:
    """Re-arms used for the current stall. A new banner after our own resume line (nothing else typed
    since) is a re-arm; if anything else was typed since, it is a new stall and the count starts over."""
    if not row:
        return 0
    b = chat.get("banner") or {}
    if row.get("banner_at") == b.get("shown_at"):
        return row.get("arms") or 0
    if row.get("status") == "sent" and (chat.get("last_prompt") or "").strip() == RESUME_TEXT:
        return (row.get("arms") or 0) + 1
    return 0


def decide(chat: dict, row: dict | None, seat: dict | None, now: float) -> tuple[str, str]:
    """-> (action, reason); action: 'resume', 'wait', 'skip'."""
    b = chat.get("banner")
    if not b:
        return "skip", "last message is not a limit banner"
    if chat.get("excluded") or sources.excluded(chat.get("config")):
        return "skip", "read-only account"
    if chat.get("kind") != "bg" or not chat.get("job_id"):
        return "skip", "not a background chat with a job (a terminal tab cannot be typed into)"
    if row and row.get("cancelled") and row.get("banner_at") == b["shown_at"]:
        return "skip", "cancelled by the user for this stall"
    if chat.get("state") in ("working", "needs_you"):
        return "wait", "chat is busy or showing a dialog"
    if now < b["resets_at"] + GRACE_S:
        return "wait", f"reset at {b['resets_at']:.0f}, {int(b['resets_at'] + GRACE_S - now)} s to go"
    if now - b["resets_at"] > MAX_STALL_S:
        return "skip", "reset passed over 12 h ago: left to the user (the stall may be parked on purpose)"
    five = ((seat or {}).get("five_hour") or {}).get("pct")
    seven = ((seat or {}).get("seven_day") or {}).get("pct")
    if (five is not None and five >= 100) or (seven is not None and seven >= 100):
        return "wait", "seat meter still reads 100%"
    arms = rearm(chat, row)
    if arms > MAX_ARMS:
        return "skip", f"{MAX_ARMS} re-arms used for this stall"
    if row and row.get("banner_at") == b["shown_at"] and row.get("status") == "sent":
        return "wait", "already sent for this banner; waiting for the chat to move"
    if row and row.get("last_at") and now - row["last_at"] < RETRY_S and row.get("status") == "failed":
        return "wait", "retry gap after a failed send"
    return "resume", f"reset passed {int(now - b['resets_at'])} s ago"


class Resumer:
    def __init__(self, sender=None, mode_fn=db.mode, path: str | None = None):
        self.sender = sender            # fn(chat, text) -> actions-style result
        self.mode_fn = mode_fn
        self.path = path
        self.busy: set[str] = set()
        c = db.connect(path)
        c.executescript(SCHEMA)
        c.commit()
        c.close()

    def row(self, sid: str) -> dict | None:
        r = db.rows("SELECT * FROM cp_resume WHERE session_id=?", (sid,), self.path)
        return r[0] if r else None

    def _put(self, chat: dict, **kw) -> None:
        cur = self.row(chat["session_id"]) or {"session_id": chat["session_id"]}
        cur.update({"key": chat["key"], "seat": chat["account"]}, **kw)
        cols = ", ".join(cur)
        db.execute(f"INSERT OR REPLACE INTO cp_resume({cols}) VALUES({', '.join('?' * len(cur))})",
                   tuple(cur.values()), self.path)

    def cancel(self, chat: dict) -> None:
        b = chat.get("banner") or {}
        self._put(chat, cancelled=1, banner_at=b.get("shown_at"), status="cancelled", detail="cancelled by the user")

    def tick(self, snap: dict, now: float | None = None) -> list[dict]:
        now = now or time.time()
        global_mode = self.mode_fn("limit_resume")
        pref = prefs(self.path)
        if global_mode == "off" and "on" not in pref.values():
            return []
        seats = {s["config"]: s for s in snap.get("seats") or []}
        out = []
        for c in snap.get("chats") or []:
            if not c.get("banner") or c["session_id"] in self.busy:
                continue
            mode = effective(global_mode, pref.get(c["session_id"]))
            if mode == "off":
                continue
            row = self.row(c["session_id"])
            action, why = decide(c, row, seats.get(c["config"]), now)
            b = c["banner"]
            ev = {"banner": b.get("text"), "shown_at": b["shown_at"], "resets_at": b["resets_at"],
                  "transcript": c.get("path"), "state": c.get("state")}
            if action != "resume":
                continue
            if mode == "dry-run":
                if not row or row.get("dry_logged_for") != b["shown_at"]:
                    db.log_auto("limit_resume", mode, c["session_id"], c["account"], "would resume", why, ev, self.path)
                    self._put(c, dry_logged_for=b["shown_at"], banner_at=b["shown_at"], resets_at=b["resets_at"],
                              status="dry-run", detail=why)
                    out.append({"session_id": c["session_id"], "action": "would resume"})
                continue
            arms = rearm(c, row)
            self.busy.add(c["session_id"])
            self._put(c, banner_at=b["shown_at"], resets_at=b["resets_at"], arms=arms, status="sending", last_at=now)
            threading.Thread(target=self._send, args=(c, ev, why), daemon=True).start()
            out.append({"session_id": c["session_id"], "action": "resume"})
        return out

    def _send(self, chat: dict, ev: dict, why: str) -> None:
        try:
            res = self.sender(chat, RESUME_TEXT)
            ok = bool(res.get("ok")) and res.get("confirmed") is not False
            self._put(chat, status="sent" if ok else "failed", detail=(res.get("error") or res.get("route") or "")[:300],
                      last_at=time.time())
            db.log_auto("limit_resume", "on", chat["session_id"], chat["account"], "sent" if ok else "failed",
                        why, dict(ev, result=res), self.path)
        except Exception as e:      # never let a resume thread die silently
            self._put(chat, status="failed", detail=f"{type(e).__name__}: {e}"[:300], last_at=time.time())
        finally:
            self.busy.discard(chat["session_id"])

    def view(self) -> list[dict]:
        return db.rows("SELECT * FROM cp_resume ORDER BY last_at DESC LIMIT 100", (), self.path)
