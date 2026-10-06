"""Shadow layer: suggestions only, never sent.

Prose decisions: final messages of background chats that end in a question become suggested option sets
on the board card. Mode "shadow_drafts" in cp/config.json:
  dry-run  no model call: count candidates and log them once per chat turn (default; spends nothing)
  on       batch up to 30 items per call, one call per seat on THAT seat (a design choice: the chat's own seat
           pays), `claude -p --model sonnet --tools "" --setting-sources project --no-session-persistence`
           from a neutral cwd; paused when the seat is near or blocked; $2 per day total cap; never for the
           a read-only account.
Closed world: the model only proposes options for the question as written; it never answers it.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from .. import config
from . import db, sources

BATCH = 30
DAY_CAP_USD = 2.0
CLAUDE = config.claude_bin()
PROMPT = """You turn questions that coding agents asked their operator into decision cards. You never answer them.
For each item, read the agent's final message and output the decisions it asks the operator to make.
Rules: only questions actually asked; 1 to 4 decisions per item; 2 to 4 short options each, taken from the text
where possible; "recommended" is the option the agent itself recommends, else null. Never invent facts.
Output JSON only: [{"id": "<item id>", "parts": [{"question": "...", "options": ["...", "..."], "recommended": "<option or null>"}]}]
Items:
"""


def asks_question(text: str) -> bool:
    return "?" in (text or "").strip()[-250:]


def candidates(snap: dict, done: set[str]) -> list[dict]:
    """Idle background chats (not a read-only account) whose final message ends in a question and has no hook decision."""
    out = []
    for c in snap.get("chats") or []:
        if c.get("kind") != "bg" or c.get("excluded") or sources.excluded(c.get("config")):
            continue
        if c.get("state") not in ("idle", "stopped") or c.get("banner") or not asks_question(c.get("final")):
            continue
        key = f"{c['session_id']}:{c.get('final_at')}"
        if key not in done:
            out.append({"id": key, "session_id": c["session_id"], "seat": c["account"], "config": c["config"],
                        "final": (c.get("final") or "")[-3000:]})
    return out


def spent_today() -> float:
    day = time.strftime("%Y-%m-%d")
    r = db.rows("SELECT COALESCE(SUM(CAST(json_extract(evidence, '$.cost_usd') AS REAL)), 0) AS s FROM auto_log"
                " WHERE action='shadow_call' AND strftime('%Y-%m-%d', at, 'unixepoch', 'localtime')=?", (day,))
    return float(r[0]["s"] or 0)


def call_model(config: str, items: list[dict], runner=subprocess.run, prompt: str = PROMPT) -> tuple[list, float]:
    body = prompt + json.dumps([{k: v for k, v in i.items() if k in ("id", "final", "past", "facts")} for i in items])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_JOB_DIR"))}
    env["CLAUDE_CONFIG_DIR"] = config
    with tempfile.TemporaryDirectory() as cwd:
        p = runner([CLAUDE, "-p", body, "--model", "sonnet", "--tools", "", "--setting-sources", "project",
                    "--no-session-persistence", "--output-format", "json"], cwd=cwd, env=env,
                   capture_output=True, text=True, timeout=300)
    meta = json.loads(p.stdout or "{}")
    text = meta.get("result") or ""
    start, end = text.find("["), text.rfind("]")
    parsed = json.loads(text[start:end + 1]) if start >= 0 and end > start else []
    return parsed, float(meta.get("total_cost_usd") or 0)


class Shadow:
    def __init__(self, mode_fn=db.mode, model=call_model):
        self.mode_fn, self.model = mode_fn, model
        self.done: set[str] = {r["basis"] for r in db.rows("SELECT basis FROM suggestions WHERE class='prose_decision'")}
        self.logged: set[str] = set()
        self.last_run = 0.0

    def tick(self, snap: dict, now: float | None = None, every_s: float = 300) -> dict:
        now = now or time.time()
        mode = self.mode_fn("shadow_drafts")
        if mode == "off" or now - self.last_run < every_s:
            return {"mode": mode, "ran": False}
        self.last_run = now
        cands = candidates(snap, self.done)
        if mode != "on":
            new = [c for c in cands if c["id"] not in self.logged]
            if new:
                db.log_auto("shadow_call", mode, None, None, "would classify", f"{len(new)} prose questions",
                            {"items": [c["id"] for c in new][:50], "cost_usd": 0})
                self.logged.update(c["id"] for c in new)
            return {"mode": mode, "ran": False, "candidates": len(cands)}
        seats = {s["config"]: s for s in snap.get("seats") or []}
        made = 0
        for cfg in sorted({c["config"] for c in cands}):
            if spent_today() >= DAY_CAP_USD:
                db.log_auto("shadow_call", mode, None, None, "skipped", "daily cap reached", {"cost_usd": 0})
                break
            if (seats.get(cfg) or {}).get("state") in ("near", "blocked", "unknown"):
                continue
            batch = [c for c in cands if c["config"] == cfg][:BATCH]
            try:
                parsed, cost = self.model(cfg, batch)
            except Exception as e:
                db.log_auto("shadow_call", mode, None, os.path.basename(cfg), "failed", f"{type(e).__name__}: {e}"[:300], {"cost_usd": 0})
                continue
            db.log_auto("shadow_call", mode, None, os.path.basename(cfg), "called", f"{len(batch)} items",
                        {"cost_usd": cost, "items": [b["id"] for b in batch]})
            by_id = {b["id"]: b for b in batch}
            for r in parsed:
                b = by_id.get(r.get("id"))
                if not b or not r.get("parts"):
                    continue
                db.execute("INSERT INTO suggestions(session_id, turn, at, draft, class, basis, used) VALUES(?,?,?,?,?,?,0)",
                           (b["session_id"], None, now, json.dumps(r["parts"])[:8000], "prose_decision", b["id"]))
                self.done.add(b["id"])
                made += 1
        return {"mode": mode, "ran": True, "made": made}


def latest_for(session_id: str) -> dict | None:
    r = db.rows("SELECT * FROM suggestions WHERE session_id=? AND class='prose_decision' ORDER BY at DESC LIMIT 1", (session_id,))
    if not r:
        return None
    r = r[0]
    r["parts"] = json.loads(r["draft"] or "[]")
    return r
