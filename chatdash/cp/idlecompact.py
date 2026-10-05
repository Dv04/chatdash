"""Idle compaction: when keep-warm has pinged a chat twice with nothing real in between
(about 2 hours idle), stop keeping it warm. If its context is big, compact it first, while the cache is still
warm, so the eventual cold resume re-writes a small context instead of the whole one. Then let it go cold:
automatic keep-warm does not pick the chat up again until the user (or anything real) types into it.

Measured 2026-10-03: a keep-warm ping costs ~1.9k units (12 pings); a compaction leaves ~55-90k of context
(74 compactions on all seats) and costs roughly 60k units warm (a ~5k-token summary at 5x, plus ~35k written by
the first call after). A cold resume re-writes the whole context (1 unit per token). So compacting pays only
above ~150k of context (idle_compact_min_ctx); below it the chat is just left to go cold.

Mode "idle_compact": off / dry-run / on. Never types into a chat that is working or showing a dialog, never
on a read-only account, never on a seat at its limit (nothing can run there), and never when the cache is already
cold (compacting then would re-read the whole context at full price).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from . import db, sources

KEEPALIVE = "[keepalive]"
SCHEMA = """CREATE TABLE IF NOT EXISTS cp_idle_compact (
    session_id TEXT PRIMARY KEY, key TEXT, seat TEXT, basis TEXT, at REAL, action TEXT, ctx_before INTEGER,
    status TEXT, detail TEXT, ctx_after INTEGER);"""
WARM_MIN = 55.0
VERIFY_S = 900
REPLY_WAIT_S = 120      # the 2nd ping's reply refreshes the cache; decide only once it is in (or after this long)
PENDING = ("sending", "sent")


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None
    except ValueError:
        return None


def config() -> tuple[int, int]:
    try:
        cfg = json.load(open(db.config_path()))
    except (OSError, ValueError):
        cfg = {}
    return int(cfg.get("idle_compact_pings") or 2), int(cfg.get("idle_compact_min_ctx") or 150_000)


def ping_status(path: str | None, since_iso: str | None, tail: int = 524288) -> dict:
    """Keep-alive prompts in the transcript after the last real prompt (read from the tail only):
    n, the last one's time, and the time of the first model reply after it (None while unanswered)."""
    out = {"n": 0, "last": None, "reply": None}
    if not path:
        return out
    since = _epoch(since_iso) or 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            data = fh.read()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        is_ka = KEEPALIVE.encode() in raw and b'"user"' in raw          # spacing-independent prefilter
        if not is_ka and not (out["last"] and out["reply"] is None and b'"assistant"' in raw):
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        ts = _epoch(r.get("timestamp")) or 0
        if r.get("type") == "assistant":
            if out["last"] and out["reply"] is None and ts >= out["last"]:
                out["reply"] = ts
            continue
        c = (r.get("message") or {}).get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
        if text.lstrip().startswith(KEEPALIVE) and ts > since:
            out["n"] += 1
            out["last"], out["reply"] = ts, None
    return out


def pings_since(path: str | None, since_iso: str | None, tail: int = 524288) -> int:
    return ping_status(path, since_iso, tail)["n"]


def compact_info(path: str | None, after: float, tail: int = 524288) -> dict | None:
    """The first compaction recorded after `after`: {"post": tokens left (compactMetadata.postTokens) or None}."""
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            data = fh.read()
    except OSError:
        return None
    found = None
    for raw in data.split(b"\n"):
        if b"isCompactSummary" not in raw and b"compact_boundary" not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if (r.get("isCompactSummary") or r.get("subtype") == "compact_boundary") and (_epoch(r.get("timestamp")) or 0) > after:
            found = found or {"post": None}
            post = (r.get("compactMetadata") or {}).get("postTokens")
            if post is not None:
                found["post"] = post
                return found
    return found


def compacted_after(path: str | None, after: float, tail: int = 524288) -> bool:
    return compact_info(path, after, tail) is not None


def decide(chat: dict, kw_on: bool, pings: int, seat: dict | None, row: dict | None,
           need_pings: int = 2, min_ctx: int = 150_000, ping: dict | None = None, now: float | None = None,
           pinging: bool = False) -> tuple[str, str]:
    """-> ('compact' | 'cold' | 'skip', reason). Pure. `ping` is ping_status(): when given, warmth is measured from
    the last ping's reply, because the decision would otherwise run between a ping and its reply and read cold."""
    if not kw_on:
        return "skip", "keep-warm is not on for this chat"
    if chat.get("excluded") or sources.excluded(chat.get("config")):
        return "skip", "read-only account"
    if chat.get("kind") != "bg" or not chat.get("job_id"):
        return "skip", "not a background chat with a job"
    if row and row.get("basis") == (chat.get("last_prompt_at") or "") and row.get("status") in ("sent", "sending", "compacted", "cold", "dry-run", "failed", "unverified"):
        return "skip", "already handled since the last real prompt"
    if pings < need_pings:
        return "skip", f"{pings} keep-warm ping(s) since the last real prompt, waiting for {need_pings}"
    if chat.get("state") != "idle":
        return "skip", f"chat is {chat.get('state')}"
    if pinging:
        return "skip", "a keep-warm ping is in flight: decide after its reply"
    age = chat.get("cache_age_min")
    if ping and ping.get("last"):
        now = now or time.time()
        if ping.get("reply") is None:
            if now - ping["last"] < REPLY_WAIT_S:
                return "skip", "waiting for the last keep-warm ping's reply"
            age = None                                    # unanswered for 2 min: treat as cold
        else:
            age = max(0.0, (now - ping["reply"]) / 60.0)
    five = ((seat or {}).get("five_hour") or {}).get("pct")
    seven = ((seat or {}).get("seven_day") or {}).get("pct")
    ctx = chat.get("ctx_tokens") or 0
    if ctx < min_ctx:
        return "cold", f"idle through {pings} pings; context {ctx // 1000}k is under {min_ctx // 1000}k, so compacting would not pay: stop keeping it warm"
    if (five or 0) >= 100 or (seven or 0) >= 100:
        return "cold", "seat is at its limit (a compaction cannot run): stop keeping it warm"
    if ping and ping.get("last") and ping.get("reply") is None:
        return "cold", "the last keep-warm ping got no reply within 2 min: stop keeping it warm"
    if age is None or age >= WARM_MIN:
        return "cold", "cache already cold: compacting now would re-read the whole context at full price"
    return "compact", f"idle through {pings} pings; context {ctx // 1000}k >= {min_ctx // 1000}k: compact while warm, then let it go cold"


class IdleCompactor:
    def __init__(self, kw=None, sender=None, mode_fn=db.mode, path: str | None = None, every: float = 30.0):
        self.kw, self.sender, self.mode_fn, self.path, self.every = kw, sender, mode_fn, path, every
        self.busy: set[str] = set()
        self.last = 0.0
        c = db.connect(path)
        c.executescript(SCHEMA)
        c.commit()
        c.close()

    def row(self, sid: str) -> dict | None:
        r = db.rows("SELECT * FROM cp_idle_compact WHERE session_id=?", (sid,), self.path)
        return r[0] if r else None

    def _put(self, chat: dict, **kw) -> None:
        cur = self.row(chat["session_id"]) or {"session_id": chat["session_id"]}
        cur.update({"key": chat["key"], "seat": chat["account"]}, **kw)
        db.execute(f"INSERT OR REPLACE INTO cp_idle_compact({', '.join(cur)}) VALUES({', '.join('?' * len(cur))})",
                   tuple(cur.values()), self.path)

    def pending(self, chat: dict) -> bool:
        """For keep-warm: True while a /compact is being sent or awaits its boundary (a ping then is wasted
        and would count as one more ping)."""
        try:
            r = self.row(chat["session_id"])
        except Exception:
            return False
        return bool(r and r.get("status") in PENDING)

    def _absorb_compact_prompt(self, chat: dict) -> str | None:
        """The typed /compact is recorded as a user prompt, which moves last_prompt_at. It is ours, not real
        activity, so the row's basis follows it; otherwise automatic keep-warm would re-warm the chat at once
        and a failed compaction would be retried."""
        if (chat.get("last_prompt") or "").strip() == "/compact" and chat.get("last_prompt_at"):
            return chat["last_prompt_at"]
        return None

    def blocked(self, chat: dict) -> bool:
        """For keep-warm's automatic mode: True while a chat was let go cold and nothing real happened since."""
        try:
            r = self.row(chat["session_id"])
        except Exception:
            return False
        return bool(r and r.get("status") in ("sent", "compacted", "cold", "unverified")
                    and r.get("basis") == (chat.get("last_prompt_at") or ""))

    def tick(self, snap: dict, now: float | None = None) -> list[dict]:
        now = now or time.time()
        self._verify(snap, now)
        mode = self.mode_fn("idle_compact")
        if mode == "off" or not self.kw or now - self.last < self.every:
            return []
        self.last = now
        need, min_ctx = config()
        active = {r["key"] for r in self.kw.active_rows()}
        seats = {s["config"]: s for s in snap.get("seats") or []}
        out = []
        for c in snap.get("chats") or []:
            if c["key"] not in active or c["session_id"] in self.busy:
                continue
            row = self.row(c["session_id"])
            ps = ping_status(c.get("path"), c.get("last_prompt_at"))
            pings = ps["n"]
            action, why = decide(c, True, pings, seats.get(c.get("config")), row, need, min_ctx, ping=ps, now=now,
                                 pinging=c["key"] in (getattr(self.kw, "pinging", None) or ()))
            if action == "skip":
                continue
            basis = c.get("last_prompt_at") or ""
            ev = {"ctx_tokens": c.get("ctx_tokens"), "pings": pings, "cache_age_min": c.get("cache_age_min"), "transcript": c.get("path")}
            if mode == "dry-run":
                db.log_auto("idle_compact", mode, c["session_id"], c["account"], f"would {'compact, then let go cold' if action == 'compact' else 'stop keep-warm, let go cold'}", why, ev, self.path)
                self._put(c, basis=basis, at=now, action=action, ctx_before=c.get("ctx_tokens"), status="dry-run", detail=why)
                out.append({"session_id": c["session_id"], "action": "would " + action})
                continue
            if action == "cold":
                self.kw.disable(c["key"], "idle: letting it go cold")
                self._put(c, basis=basis, at=now, action=action, ctx_before=c.get("ctx_tokens"), status="cold", detail=why)
                db.log_auto("idle_compact", "on", c["session_id"], c["account"], "keep-warm stopped", why, ev, self.path)
                out.append({"session_id": c["session_id"], "action": "cold"})
                continue
            # compact FIRST with keep-warm still on; keep-warm stops only once _verify sees the boundary
            self.busy.add(c["session_id"])
            self._put(c, basis=basis, at=now, action=action, ctx_before=c.get("ctx_tokens"), status="sending", detail=why)
            threading.Thread(target=self._send, args=(c, ev, why), daemon=True).start()
            out.append({"session_id": c["session_id"], "action": "compact"})
        return out

    def _send(self, chat: dict, ev: dict, why: str) -> None:
        try:
            sent_at = time.time()
            res = self.sender(chat, "/compact")
            ok = bool(res.get("ok"))
            # `at` becomes the send time: _verify looks for a boundary after it
            self._put(chat, status="sent" if ok else "failed", at=sent_at if ok else self.row(chat["session_id"]).get("at"),
                      detail=("compact sent" if ok else "compact failed: " + str(res.get("error") or "send did not land"))[:300])
            db.log_auto("idle_compact", "on", chat["session_id"], chat["account"],
                        "compact sent" if ok else "compact failed: send did not land; keep-warm stays on", why,
                        dict(ev, result=res), self.path)
        except Exception as e:
            self._put(chat, status="failed", detail=f"compact failed: {type(e).__name__}: {e}"[:300])
            db.log_auto("idle_compact", "on", chat["session_id"], chat["account"],
                        "compact failed: send raised; keep-warm stays on", f"{type(e).__name__}: {e}"[:300], ev, self.path)
        finally:
            self.busy.discard(chat["session_id"])

    def _verify(self, snap: dict, now: float) -> None:
        """A sent compaction is confirmed from the transcript (a compact summary after the send), never assumed."""
        by_sid = {c["session_id"]: c for c in snap.get("chats") or []}
        for r in db.rows("SELECT * FROM cp_idle_compact WHERE status='sent'", (), self.path):
            c = by_sid.get(r["session_id"])
            if not c:
                continue
            info = compact_info(c.get("path"), r["at"] - 5)       # transcript timestamps vs our clock: 5 s slack
            basis = self._absorb_compact_prompt(c) or r.get("basis")
            if info is not None:
                after = info["post"] if info["post"] is not None else None
                self.kw.disable(c["key"], "idle: compacted, letting it go cold")
                self._put(c, status="compacted", ctx_after=after, basis=basis, detail="compact boundary confirmed in the transcript")
                db.log_auto("idle_compact", "on", c["session_id"], c["account"], "compacted, keep-warm stopped",
                            f"context {(r['ctx_before'] or 0) // 1000}k -> " + (f"{after // 1000}k" if after is not None else "unknown"),
                            None, self.path)
            elif now - r["at"] > VERIFY_S:
                self._put(c, status="failed", basis=basis, detail="compact failed: no boundary in 15 min; keep-warm stays on")
                db.log_auto("idle_compact", "on", c["session_id"], c["account"], "compact failed: no boundary in 15 min",
                            "keep-warm stays on; no retry for this basis", None, self.path)
