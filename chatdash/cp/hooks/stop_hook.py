#!/usr/bin/env python3
"""Stop hook: write a receipt for the turn (facts, not prose) and, where the per-chat gate is on, block the
chat from ending without verification evidence.

Background sessions only; read-only accounts (config read_only_accounts) exit at once; any error fails
open (prints nothing, the chat ends normally).
Install: register `chatdash-stop-hook` (or `python3 -m chatdash.cp.hooks.stop_hook`) as a Stop hook in each
account you want covered (see the README).
Gate: on by default for the work items listed in config evidence_gate_work_items (none by default), toggled
per chat on the board (table cp_gate). Mode "stop_gate" in <data dir>/config.json: dry-run logs "would block"; on blocks with
{"decision": "block", "reason": ...} at most 3 times per turn (platform cap is 8) and honours
stop_hook_active (a fourth stop in a row always ends).
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))     # the folder holding the chatdash package
HOME = os.path.expanduser("~")
MAX_BLOCKS = 3
REASON = ("Before you finish: this turn changed files, and {why}. Either run a real check (tests, build, or the "
          "command that proves the result) and quote its last output line verbatim in your reply, or say plainly "
          "what is not verified. (chatdash evidence gate, block {n} of {max})")
GATE_SCHEMA = """CREATE TABLE IF NOT EXISTS cp_gate (session_id TEXT PRIMARY KEY, on_ INTEGER, set_by TEXT, at REAL);
CREATE TABLE IF NOT EXISTS cp_gate_blocks (session_id TEXT, turn INTEGER, n INTEGER, at REAL, PRIMARY KEY (session_id, turn));"""


def log(msg: str) -> None:
    try:
        from chatdash import config
        path = os.path.join(config.logs_dir(), "stop_hook.log")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def job_name(env) -> str:
    try:
        return json.load(open(os.path.join(env["CLAUDE_JOB_DIR"], "state.json"))).get("name") or ""
    except (OSError, ValueError, KeyError):
        return ""


def gate_on(db, session_id: str, name: str) -> bool:
    r = db.rows("SELECT on_ FROM cp_gate WHERE session_id=?", (session_id,))
    if r:
        return bool(r[0]["on_"])
    from chatdash import config
    wi = config.work_item_of(name)
    return bool(wi and wi in config.gate_work_items())


def main(stdin=sys.stdin, env=os.environ) -> int:
    try:
        payload = json.load(stdin)
    except ValueError:
        return 0
    cfg = env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    sys.path.insert(0, ROOT)
    from chatdash import config
    if config.is_read_only(cfg=cfg):
        return 0
    if not env.get("CLAUDE_JOB_DIR") or not payload.get("transcript_path"):
        return 0
    from chatdash.cp import db, receipts
    db.init()
    c = db.connect()
    c.executescript(GATE_SCHEMA)
    c.commit()
    c.close()
    sid = payload.get("session_id") or ""
    last = payload.get("last_assistant_message") or ""
    cfg_dir = os.path.normpath(os.path.expanduser(cfg))
    rc = receipts.build(payload["transcript_path"], payload.get("cwd"), last)
    ok, why = receipts.verdict(rc, last)
    receipts.store(sid, cfg_dir, rc, ok)
    name = job_name(env)
    if ok or not gate_on(db, sid, name):
        return 0
    mode = db.mode("stop_gate")
    seat = config.account_of_config(cfg_dir)
    row = db.rows("SELECT n FROM cp_gate_blocks WHERE session_id=? AND turn=?", (sid, rc["turn"]))
    n = row[0]["n"] if row else 0
    ev = {"transcript": payload["transcript_path"], "turn": rc["turn"], "files": rc["diff"]["files"],
          "checks": [x["cmd"][:120] for x in rc["checks"]], "stop_hook_active": bool(payload.get("stop_hook_active"))}
    if n >= MAX_BLOCKS:
        db.log_auto("stop_gate", mode, sid, seat, "allowed", f"{MAX_BLOCKS} blocks used this turn; {why}", ev)
        return 0
    if mode != "on":
        if not row:
            db.log_auto("stop_gate", mode, sid, seat, "would block", why, ev)
            db.execute("INSERT OR REPLACE INTO cp_gate_blocks(session_id, turn, n, at) VALUES(?,?,?,?)", (sid, rc["turn"], 0, time.time()))
        return 0
    db.execute("INSERT OR REPLACE INTO cp_gate_blocks(session_id, turn, n, at) VALUES(?,?,?,?)", (sid, rc["turn"], n + 1, time.time()))
    db.log_auto("stop_gate", mode, sid, seat, "blocked", why, ev)
    sys.stdout.write(json.dumps({"decision": "block", "reason": REASON.format(why=why, n=n + 1, max=MAX_BLOCKS)}))
    return 0


def cli() -> None:
    """Console entry point (`chatdash-stop-hook`)."""
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        sys.exit(0)


if __name__ == "__main__":
    cli()
