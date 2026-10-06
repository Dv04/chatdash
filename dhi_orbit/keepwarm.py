"""Automatic keep-warm: one tiny ping into a chat just before its prompt cache expires.

The cache lives 1 hour from the LAST API call and every call renews it. A ping typed into
`claude attach <job>` (the route DHI Orbit already uses for replies) makes one call that reads the
whole context from cache and writes only a few tokens, so the hour starts over for about 1% of a
cold re-cache. Per chat and opt-in; nothing is pinged unless every guard in decide() passes.

Every ping is verified from the transcript. If it re-wrote the context (it was cold after all) or
fails twice, keep-warm turns itself off for that chat and notifies, so a wrong assumption can never
keep spending. The premise ("a hit renews the timer") is tested in
a throwaway-session benchmark (a ping into `claude attach` read the whole context from cache and wrote
a few tokens); do not trust it beyond that measurement.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time

from . import _plat, actions, config, extract

KEEPALIVE_PREFIX = "[keepalive]"
PING_TEXT = KEEPALIVE_PREFIX + " Reply with exactly: ok. Do not use tools or continue any task."

PING_AT_MIN = 55.0        # ping once the last call is this old: 5 minutes left of the 60
AUTO_ACTIVE_HOURS = 12.0  # automatic mode keeps a chat warm for this long after its last REAL activity
AUTO_MIN_CTX = 20_000     # ignore near-empty chats
AUTO_DEFAULT = False      # automatic mode is OFF until the toolbar switch turns it on: nothing is sent unasked
EXPIRE_MIN = 59.0         # older than this the cache is gone: do not pay a cold ping
DEFAULT_HOURS = 12.0
RETRY_S = 90              # minimum gap between two attempts on one chat
MAX_FAILS = 2
BUDGET_FRACTION = 0.5     # stop when the pings cost half of one cold re-cache
READ_WEIGHT = 0.01        # upper bound for a cache read vs a write (measured "at most ~1%")
WARM_SHARE = 0.8          # a ping is warm if >= 80% of its context was read from cache
VERIFY_S = 150


def is_keepalive(text: str | None) -> bool:
    return (text or "").lstrip().startswith(KEEPALIVE_PREFIX)


def can_type(chat: dict) -> bool:
    """A background chat with a job (typed into `claude attach`). A chat in a terminal tab cannot be
    submitted to: see the note in actions.py."""
    return chat["kind"] == "bg" and bool(chat.get("job_id"))


def cold_cost(chat: dict) -> float:
    """Limit units of one cold re-cache of this chat (context written once)."""
    return (chat.get("ctx_tokens") or 0) * extract.weight(chat.get("model"))


def seat_spent(seat: dict | None) -> str | None:
    """'5h' / '7d' when the seat's meter reads 100% (a ping cannot run, so it renews nothing), else None.
    An unknown reading never blocks."""
    if not seat:
        return None
    if (seat.get("five") or 0) >= 100:
        return "5h"
    if (seat.get("seven") or 0) >= 100:
        return "7d"
    return None


def decide(chat: dict | None, row: dict, now: float, *, queued: bool = False, busy: bool = False,
           pinging: bool = False, ping_at: float = PING_AT_MIN, expire: float = EXPIRE_MIN, seat: dict | None = None):
    """-> (action, reason); action is 'ping', 'wait' or 'stop'. Pure: the whole policy lives here."""
    if now >= row["stop_at"]:
        return "stop", "time limit reached"
    if chat is None:
        return "wait", "chat not visible"
    spent = seat_spent(seat)
    if spent:
        return "wait", f"seat is at its {spent} limit: a ping cannot run there, so it would renew nothing"
    if not can_type(chat):
        return "stop", "no way to send to this chat (only background chats with a job can be pinged)"
    if chat["state"] == "stopped":
        return "stop", "chat is stopped (attach would re-cache it cold)"
    if chat.get("ttl") == "5m":
        return "stop", ("this chat's cache uses the 5-minute TTL (seat over its plan usage, or API billing); "
                        "an hourly ping cannot keep it warm")
    if chat["state"] in ("working", "needs_you"):
        return "wait", "chat is busy" if chat["state"] == "working" else "waiting for you (not typing over a dialog)"
    if busy or queued or pinging:
        return "wait", "another action on this chat"
    age = chat.get("cache_age_min")
    if age is None:
        return "wait", "cache age unknown"
    if age >= expire:
        return "stop", f"cache already expired ({age:.0f} min since the last call)"
    if age < ping_at:
        return "wait", f"next ping in {ping_at - age:.0f} min"
    if row["fails"] >= MAX_FAILS:
        return "stop", f"{row['fails']} pings failed in a row"
    if now - row["last_attempt"] < RETRY_S:
        return "wait", "retry gap"
    per_ping = row["units"] / row["pings"] if row["pings"] else 0.0
    budget = BUDGET_FRACTION * cold_cost(chat)
    if row["units"] + per_ping > budget:
        return "stop", f"budget used: {row['units']:.0f} of {budget:.0f} units (half a cold re-cache)"
    return "ping", "cache is %.0f min old" % age


def new_calls(path: str, offset: int) -> list[dict]:
    """Assistant API calls appended to a transcript after byte `offset`: per message.id the
    cache read/write, input and the LARGEST output, in order."""
    best: dict[str, dict] = {}
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return []
    for raw in data.split(b"\n"):
        if b'"usage"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        m = r.get("message") or {}
        u, mid = m.get("usage"), m.get("id")
        if r.get("type") != "assistant" or not u or not mid or m.get("model") == "<synthetic>":
            continue
        out = u.get("output_tokens") or 0
        if mid not in best or out >= best[mid]["out"]:
            best[mid] = {"id": mid, "model": m.get("model"), "read": u.get("cache_read_input_tokens") or 0,
                         "write": u.get("cache_creation_input_tokens") or 0, "inp": u.get("input_tokens") or 0,
                         "out": out}
    return list(best.values())


def judge(calls: list[dict]) -> dict:
    """Verdict on a ping from its API calls: warm iff the FIRST call read >= WARM_SHARE of its
    context from cache. Cost counts every call, reads at the READ_WEIGHT upper bound."""
    if not calls:
        return {"warm": None, "units": 0.0, "read": 0, "write": 0}
    first = calls[0]
    ctx = first["read"] + first["write"] + first["inp"]
    units = sum(extract.weight(c["model"]) * (c["write"] + c["inp"] + 5 * c["out"] + READ_WEIGHT * c["read"])
                for c in calls)
    return {"warm": ctx > 0 and first["read"] / ctx >= WARM_SHARE, "units": units,
            "read": first["read"], "write": first["write"]}


class Awake:
    """Keeps the Mac from idle-sleeping while any chat has keep-warm on (pings cannot fire asleep)."""

    def __init__(self):
        self.p: subprocess.Popen | None = None

    def sync(self, want: bool) -> None:
        if want and (self.p is None or self.p.poll() is not None):
            self.p = _plat.keep_awake(os.getpid())      # caffeinate (macOS) or a SetThreadExecutionState helper (Windows)
        elif not want and self.p is not None:
            if self.p.poll() is None:
                self.p.terminate()
            self.p = None


class KeepWarm:
    def __init__(self, db: str | None = None, notify=None, record=None, sender=None, awake=None,
                 ping_at: float = PING_AT_MIN, expire: float = EXPIRE_MIN, verify_s: float = VERIFY_S,
                 poll_s: float = 1.0, settle_s: float = 3.0):
        if db is None:
            config.ensure_home()
            db = config.db_path()
        self.conn = sqlite3.connect(db, check_same_thread=False, timeout=10)
        self.lock = threading.Lock()
        self.skip = None          # fn(chat) -> True: automatic mode leaves this chat cold (cp idle compaction)
        self.hold = None          # fn(chat) -> True: do not ping now (cp idle compaction has a /compact pending)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS keepwarm (
                key TEXT PRIMARY KEY, active INTEGER, on_at REAL, stop_at REAL, pings INTEGER,
                units REAL, last_ping REAL, last_attempt REAL, fails INTEGER, status TEXT, detail TEXT);
        """)
        for col in ("ended_at REAL", "auto INTEGER"):
            try:
                self.conn.execute("ALTER TABLE keepwarm ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass                                # column already there
        self.conn.executescript("CREATE TABLE IF NOT EXISTS kw_setting (key TEXT PRIMARY KEY, val TEXT);")
        self.conn.row_factory = sqlite3.Row
        self.notify = notify or (lambda title, body: None)
        self.record = record or (lambda what, key, res: res)
        self.sender = sender or actions.type_into_attach
        self.awake = awake or Awake()
        self.ping_at, self.expire, self.verify_s = ping_at, expire, verify_s
        self.poll_s, self.settle_s = poll_s, settle_s
        self.pinging: set[str] = set()

    # ------------------------------------------------------------ rows
    def _row(self, key: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM keepwarm WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def _set(self, key: str, **kw) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        with self.lock:
            self.conn.execute(f"UPDATE keepwarm SET {cols} WHERE key=?", (*kw.values(), key))
            self.conn.commit()

    def active_rows(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM keepwarm WHERE active=1")]

    # ------------------------------------------------------------ on / off
    def auto_on(self) -> bool:
        with self.lock:
            r = self.conn.execute("SELECT val FROM kw_setting WHERE key='auto'").fetchone()
        return AUTO_DEFAULT if r is None else r[0] == "1"

    def set_auto(self, on: bool) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kw_setting(key, val) VALUES('auto', ?)", ("1" if on else "0",))
            self.conn.commit()
        if not on:
            self.end_auto_rows()

    def end_auto_rows(self) -> None:
        """Switching automatic mode off also ends the chats it already switched on. Chats turned on by
        hand (per-chat button) keep their own timer."""
        for row in self.active_rows():
            if row.get("auto"):
                self.disable(row["key"], "automatic keep-warm switched off")

    def auto_candidates(self, by_key: dict, now: float, seats: dict | None = None) -> list:
        """Chats automatic mode would start keeping warm: background chat with a job, 1h cache still alive,
        real activity (not pings) within AUTO_ACTIVE_HOURS, not already on, and not ended since its last call."""
        out = []
        for chat in by_key.values():
            if not can_type(chat) or chat["state"] == "stopped" or chat.get("ttl") == "5m":
                continue
            if seat_spent((seats or {}).get(chat.get("account"))):
                continue
            if self.skip and self.skip(chat):
                continue
            age = chat.get("cache_age_min")
            if age is None or age >= self.expire or (chat.get("ctx_tokens") or 0) < AUTO_MIN_CTX:
                continue
            real = max(filter(None, [extract.iso_epoch(chat.get("last_prompt_at")),
                                     extract.iso_epoch(chat.get("final_at"))]), default=None)
            if real is None or now - real >= AUTO_ACTIVE_HOURS * 3600:
                continue
            row = self._row(chat["key"])
            if row and row["active"]:
                continue
            if row and row.get("ended_at") and (now - age * 60) <= row["ended_at"]:
                continue                            # no new call since keep-warm ended on this chat
            out.append((chat, real))
        return out

    def auto_enable(self, by_key: dict, now: float, seats: dict | None = None) -> None:
        for chat, real in self.auto_candidates(by_key, now, seats):
            self.enable(chat, now=now, until=real + AUTO_ACTIVE_HOURS * 3600)

    def enable(self, chat: dict, hours: float = DEFAULT_HOURS, now: float | None = None,
               until: float | None = None) -> dict:
        now = now or time.time()
        if not can_type(chat):
            return {"ok": False, "error": "Only background chats with a job can be kept warm: a chat in a terminal tab cannot be sent "
                    "a message that submits."}
        if chat["state"] == "stopped":
            return {"ok": False, "error": "The chat is stopped; attach would re-cache it cold. Reply once first."}
        if chat.get("ttl") == "5m":
            return {"ok": False, "error": "This chat's cache is written with the 5-minute TTL (the seat is over "
                    "its plan usage or billing is by API key), so an hourly ping cannot keep it warm."}
        age = chat.get("cache_age_min")
        if age is not None and age >= self.expire:      # a "working" chat can sit waiting on a monitor for hours
            return {"ok": False, "error": f"The cache is already cold ({age:.0f} min since the last call), so a "
                    "ping would re-write the whole context. Reply once to re-cache, then turn keep-warm on."}
        if chat.get("warmth") not in ("warm", "cooling"):
            return {"ok": False, "error": "The cache is already cold, so a ping would re-write the whole "
                    "context. Reply once to re-cache, then turn keep-warm on."}
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO keepwarm(key, active, on_at, stop_at, pings, units, last_ping, "
                "last_attempt, fails, status, detail, auto) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (chat["key"], 1, now, until or now + hours * 3600, 0, 0.0, 0.0, 0.0, 0,
                 "on", "automatic" if until else f"for {hours:g} h", 1 if until else 0))
            self.conn.commit()
        return {"ok": True, "hours": hours}

    def disable(self, key: str, why: str = "turned off", status: str = "off") -> None:
        if self._row(key):
            self._set(key, active=0, status=status, detail=why, ended_at=time.time())

    # ------------------------------------------------------------ the loop step
    def tick(self, by_key: dict, queued=(), busy=(), now: float | None = None, seats: dict | None = None) -> None:
        now = now or time.time()
        seats = seats or {}
        if self.auto_on():
            self.auto_enable(by_key, now, seats)
        else:
            self.end_auto_rows()                    # belt and braces: no automatic row survives the switch
        for row in self.active_rows():
            key = row["key"]
            action, why = decide(by_key.get(key), row, now, queued=key in queued, busy=key in busy,
                                 pinging=key in self.pinging, ping_at=self.ping_at, expire=self.expire,
                                 seat=seats.get((by_key.get(key) or {}).get("account")))
            if action == "stop":
                ended = "time" if "time limit" in why else "budget" if "budget" in why else "expired" \
                    if "expired" in why else "stopped"
                self.disable(key, why, ended)        # a normal end is silent; only failures notify
            elif action == "ping":
                chat = by_key.get(key)
                if self.hold and chat:
                    try:
                        if self.hold(chat):
                            continue
                    except Exception:
                        pass
                self.pinging.add(key)
                self._set(key, last_attempt=now)
                threading.Thread(target=self._ping, args=(by_key[key],), daemon=True).start()
        self.awake.sync(bool(self.active_rows()))

    def _ping(self, chat: dict) -> None:
        key, path = chat["key"], chat.get("path")
        try:
            offset = os.path.getsize(path) if path and os.path.exists(path) else 0
            res = self.sender(chat["config"], chat["job_id"], PING_TEXT, chat.get("cwd"), path)
            if not res.get("ok"):
                return self._failed(chat, res.get("error") or "ping did not land")
            calls, end = [], time.time() + self.verify_s
            while time.time() < end and not calls:
                time.sleep(self.poll_s)
                calls = new_calls(path, offset)
            time.sleep(self.settle_s)             # let the reply's last lines land
            calls = new_calls(path, offset)
            v = judge(calls)
            row = self._row(key) or {"pings": 0, "units": 0.0}
            self._set(key, pings=row["pings"] + 1, units=row["units"] + v["units"], last_ping=time.time(),
                      fails=0 if v["warm"] else row["fails"])
            if v["warm"] is None:
                return self._failed(chat, "no reply seen in the transcript within %ds" % self.verify_s)
            if not v["warm"]:
                self.disable(key, f"the ping re-wrote {v['write']:,} tokens: the cache was already cold", "cold")
                self.notify("Keep-warm FAILED: " + chat["name"][:40],
                            f"ping re-wrote {v['write']:,} tokens; keep-warm is off for this chat")
                self.record("keep warm", key, {"ok": False, "error": f"cache was cold (wrote {v['write']:,})"})
            else:
                self.record("auto keep warm", key, {"ok": True, "route": f"read {v['read']:,}, wrote {v['write']:,}"})
        except Exception as e:                    # never let a ping thread die silently
            self._failed(chat, f"{type(e).__name__}: {e}")
        finally:
            self.pinging.discard(key)

    def _failed(self, chat: dict, why: str) -> None:
        row = self._row(chat["key"]) or {"fails": 0}
        self._set(chat["key"], fails=row["fails"] + 1, status="failing", detail=why[:200])
        self.record("auto keep warm", chat["key"], {"ok": False, "error": why[:200]})
        self.pinging.discard(chat["key"])

    # ------------------------------------------------------------ what the page shows
    def view(self, chat: dict, now: float | None = None) -> dict | None:
        row = self._row(chat["key"])
        if not row:
            return None
        now = now or time.time()
        age = chat.get("cache_age_min")
        return {"on": bool(row["active"]), "status": row["status"], "detail": row["detail"],
                "pings": row["pings"], "units": round(row["units"]),
                "next_min": max(0, round(self.ping_at - age)) if age is not None else None,
                "stop_in_h": round(max(0, row["stop_at"] - now) / 3600, 1)}
