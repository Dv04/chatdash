#!/usr/bin/env python3
"""PreToolUse hook on AskUserQuestion: put a background chat's question on the DHI Orbit v2 board.

Fails open: any error, any non-background session, a read-only account (config read_only_accounts) or mode
"off" -> print nothing, exit 0, and the chat shows its normal dialog. Never touches permission prompts
(it only ever sees AskUserQuestion, by matcher and by the tool_name check below).

Install: register `dhi-orbit-ask-hook` (or `python3 -m dhi_orbit.cp.hooks.ask_hook`) as a PreToolUse hook with
matcher AskUserQuestion, in each account you want covered (see the README).

Mode (<data dir>/config.json "decision_hook"):
  dry-run  write the decision row (no hold); the dialog shows at once; the board can answer the dialog.
  on       hold up to hold_s polling for your answer; return allow + updatedInput {questions, answers}.
Contract for the answer (Claude Code hooks docs):
  echo back the original questions array and add answers {question text: chosen label}.
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))     # the folder holding the dhi_orbit package
HOME = os.path.expanduser("~")


def log(msg: str) -> None:
    try:
        from dhi_orbit import config
        path = os.path.join(config.logs_dir(), "ask_hook.log")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def applicable(payload: dict, env=os.environ) -> tuple[bool, str]:
    cfg = env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    sys.path.insert(0, ROOT)
    from dhi_orbit import config
    if config.is_read_only(cfg=cfg):
        return False, "read-only account"
    if not env.get("CLAUDE_JOB_DIR"):
        return False, "not a background session (a terminal user answers the dialog directly)"
    if payload.get("tool_name") != "AskUserQuestion":
        return False, "not AskUserQuestion"
    if not (payload.get("tool_input") or {}).get("questions"):
        return False, "no questions"
    return True, ""


def emit(payload: dict, answers: dict, reason: str) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                  "permissionDecisionReason": reason,
                                  "updatedInput": {"questions": payload["tool_input"]["questions"],
                                                   "answers": answers}}}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main(stdin=sys.stdin, env=os.environ, sleep=time.sleep, clock=time.time) -> int:
    try:
        payload = json.load(stdin)
    except ValueError:
        return 0
    ok, why = applicable(payload, env)
    if not ok:
        return 0
    sys.path.insert(0, ROOT)
    from dhi_orbit import config
    from dhi_orbit.cp import db, decisions
    mode = db.mode("decision_hook")
    if mode == "off":
        return 0
    try:
        cfg = json.load(open(db.config_path(), encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    hold_s = float(cfg.get("decision_hold_s") or 540)
    db.init()
    cfg_dir = os.path.normpath(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")))
    seat = config.account_of_config(cfg_dir)
    job_id = os.path.basename(env.get("CLAUDE_JOB_DIR", "").rstrip("/\\")) or None
    row = decisions.create(payload, mode=mode, hold_s=hold_s, seat=seat, config=cfg_dir, job_id=job_id, now=clock())
    db.log_auto("decision_hook", mode, payload.get("session_id"), seat, "row written",
                f"risk {row['risk']} ({row['risk_why']}), on_timeout {row['on_timeout']}",
                {"decision": row["id"], "transcript": payload.get("transcript_path")})
    if mode != "on":
        return 0
    end = row["hold_until"]
    while clock() < end:
        d = decisions.get(row["id"])
        if d and d["state"] == "answered" and d.get("answer"):
            emit(payload, json.loads(d["answer"]), "answered by the user on the DHI Orbit board")
            decisions.mark_delivered(row["id"], "hook")
            return 0
        sleep(1.0)
    if decisions.release(row["id"]):
        if row["on_timeout"] == "default":
            rec = decisions.recommended_answers(json.loads(row["parts"]))
            if rec and decisions.db.execute(
                    "UPDATE decisions SET state='answered', answer=?, answered_at=?, answered_by='timeout-default',"
                    " delivered=1, delivery='hook-default' WHERE id=? AND state='open'",
                    (json.dumps(rec), clock(), row["id"])) == 1:
                db.log_auto("decision_hook", mode, payload.get("session_id"), seat, "default taken",
                            "low risk, every part had a recommended option, hold expired", {"answers": rec})
                emit(payload, rec, "low-risk question unanswered for the hold time: recommended option taken")
                return 0
        return 0                     # dialog shows; the card stays answerable through the dialog
    d = decisions.get(row["id"])     # an answer won the race against the release
    if d and d.get("answer"):
        emit(payload, json.loads(d["answer"]), "answered by the user on the DHI Orbit board")
        decisions.mark_delivered(row["id"], "hook")
    return 0


def cli() -> None:
    """Console entry point (`dhi-orbit-ask-hook`)."""
    try:
        sys.exit(main())
    except Exception:                # fail open, always
        log(traceback.format_exc())
        sys.exit(0)


if __name__ == "__main__":
    cli()
