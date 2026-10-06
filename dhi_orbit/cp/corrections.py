"""A6 corrections: your replies that correct or challenge a chat, stored with context; once a day (the
first check-in time, 10:00 by default) yesterday's corrections become proposed rules, one per kind with 2+ examples.
Never applied automatically: a rule card's Apply only records your OK (no CLAUDE.md is edited)."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from . import db

KINDS = {
    "honesty": re.compile(r"\b(you lied|lying|are you sure|that'?s (not true|false|wrong)|made (that|it) up|hallucinat\w*)\b", re.I),
    "verification": re.compile(r"\b(did you (check|test|verify|run)|show me (the )?(evidence|proof|output)|prove it|where is the (test|proof))\b", re.I),
    "instruction": re.compile(r"\b(i said|i told you|i asked (you )?(to|for)|i didn'?t ask|not what i (asked|said|meant)|read (my|the) (message|instructions))\b", re.I),
    "scope": re.compile(r"\b(only|just) (do|run|change|touch)\b|\b(don'?t|do not) (touch|change|expand)\b|\btoo much\b|\bout of scope\b", re.I),
    "tone": re.compile(r"\b(no no|wtf|what the|why the)\b", re.I),
    "why": re.compile(r"\bwhy (did|are|do|would) you\b", re.I),
}
RULES = {
    "honesty": ("Before stating a fact or result, cite where it was seen this session (command, file, line); say 'not verified' otherwise.",
                "Stop gate: block a final that asserts a result with no command or quoted output in the turn."),
    "verification": ("Before saying something works, run the check that proves it and quote its last output line verbatim.",
                     "Stop gate already blocks file-changing turns without a named check; consider turning it on for this chat."),
    "instruction": ("Re-read the user's last message before acting; restate the ask in one line when it changes direction.",
                    "Compare the reply's first line with the user's last prompt; flag a mismatch for review."),
    "scope": ("Do exactly the scope asked; list anything wider as a proposal instead of doing it.",
              "Receipt shows files outside the named folder: flag on the card."),
    "tone": ("When the user pushes back, stop and ask one clarifying question with options instead of continuing.",
             "None automatic; review examples."),
    "why": ("Explain the reason for a non-obvious action before taking it, in one line.",
            "None automatic; review examples."),
}
SCHEMA = """CREATE TABLE IF NOT EXISTS cp_corrections (
    key TEXT, idx INTEGER, at TEXT, kind TEXT, marker TEXT, reply TEXT, prior TEXT, chat TEXT, account TEXT,
    PRIMARY KEY (key, idx, kind));"""


def init() -> None:
    c = db.connect()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def classify(reply: str) -> list[tuple[str, str]]:
    out = []
    for k, rx in KINDS.items():
        m = rx.search(reply or "")
        if m:
            out.append((k, m.group(0)))
    return out


def scan(path: str | None = None) -> int:
    """Index every (final, your next reply) pair in turns_fts and store the corrections. Idempotent."""
    init()
    c = sqlite3.connect(f"file:{path or db.db_path()}?mode=ro", uri=True, timeout=10)
    rows = c.execute("SELECT key, name, account, idx, ts, prompt, final FROM turns_fts").fetchall()
    c.close()
    by = {}
    for k, n, a, i, ts, p, f in rows:
        by.setdefault(k, []).append((int(i) if str(i).isdigit() else 0, ts, n, a, p or "", f or ""))
    n_new = 0
    for k, v in by.items():
        v.sort()
        for prev, cur in zip(v, v[1:]):
            reply = cur[4]
            if not reply or reply.lstrip().startswith(("<", "[keepalive", "Caveat:")):
                continue
            for kind, marker in classify(reply):
                n_new += db.execute("INSERT OR IGNORE INTO cp_corrections(key, idx, at, kind, marker, reply, prior, chat, account)"
                                    " VALUES(?,?,?,?,?,?,?,?,?)",
                                    (k, cur[0], cur[1], kind, marker, reply[:1500], prev[5][-1500:], cur[2], cur[3]))
    return n_new


def day_bounds(day: str) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=config.tz())
    to_utc = lambda x: x.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")
    return to_utc(d), to_utc(d + timedelta(days=1))


def propose_for(day: str) -> list[int]:
    """Rules for one local day's corrections: a kind with 2+ examples gets one proposal."""
    lo, hi = day_bounds(day)
    made = []
    for kind in KINDS:
        ex = db.rows("SELECT chat, reply, prior, at FROM cp_corrections WHERE kind=? AND at>=? AND at<? ORDER BY at", (kind, lo, hi))
        if len(ex) < 2 or db.rows("SELECT id FROM rules_proposed WHERE week=? AND pattern=?", (day, kind)):
            continue
        rule, check = RULES[kind]
        db.execute("INSERT INTO rules_proposed(week, pattern, examples, draft_rule, check_cmd, created_at) VALUES(?,?,?,?,?,?)",
                   (day, kind, json.dumps(ex[:6]), rule, check, time.time()))
        rid = db.rows("SELECT id FROM rules_proposed WHERE week=? AND pattern=?", (day, kind))[0]["id"]
        db.execute("INSERT INTO cp_proposals(kind, target, why, created_at, detail) VALUES(?,?,?,?,?)",
                   ("rule", str(rid), f"{len(ex)} {kind} corrections on {day}: {rule}", time.time(), check))
        made.append(rid)
    return made


class Daily:
    """Runs scan + propose once per day at the first check-in window (10:00 by default)."""

    def __init__(self, at: str = "10:00"):
        self.at = at
        self.done_for = None

    def tick(self, snap: dict, now: float | None = None) -> list[int]:
        if db.mode("corrections") == "off":
            return []
        now = now or time.time()
        t = datetime.fromtimestamp(now, config.tz())
        win = (db.settings().get("focus") or {}).get("windows") or [self.at]
        first = sorted(win)[0] if win else self.at
        today = t.strftime("%Y-%m-%d")
        if t.strftime("%H:%M") < first or self.done_for == today:
            return []
        self.done_for = today
        scan()
        return propose_for((t - timedelta(days=1)).strftime("%Y-%m-%d"))
