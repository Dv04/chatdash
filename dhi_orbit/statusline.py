"""`dhi-orbit-statusline`: the status line command DHI Orbit sets for an account (see usage_meter.py).

Reads the JSON Claude Code passes on stdin, appends the account's rate limits to DHI Orbit's meter log when they
changed, then prints the owner's own status line (kept from before) or, when there was none, a short usage line.
It never fails the status line: any error is swallowed.
"""
from __future__ import annotations

import json
import os
import sys
import time


def record(doc: dict, env=os.environ) -> None:
    from . import config
    limits = doc.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        return
    log = config.meter_log_path()
    os.makedirs(os.path.dirname(log), mode=0o700, exist_ok=True)
    cfg = os.path.abspath(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    blob = json.dumps(limits, separators=(",", ":"), sort_keys=True)
    state_path = log + ".state"
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    if state.get(cfg) == blob:
        return
    state[cfg] = blob
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\t".join([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cfg,
                            str(doc.get("session_id") or ""), blob]) + "\n")
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def short_line(doc: dict) -> str:
    lim = doc.get("rate_limits") or {}
    parts = []
    for key, tag in (("five_hour", "5h"), ("seven_day", "7d")):
        w = lim.get(key) or {}
        if w.get("used_percentage") is not None:
            parts.append(f"{tag} {round(w['used_percentage'])}%")
    model = ((doc.get("model") or {}).get("display_name")) or ""
    return "  ".join([p for p in [model] + parts if p])


def main(stdin=None, stdout=None, env=os.environ) -> int:
    if stdin is None and len(sys.argv) >= 3 and sys.argv[1] == "--home":   # the console script on Windows (usage_meter.command)
        os.environ["DHI_ORBIT_HOME"] = sys.argv[2]
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    raw = b""
    doc: dict = {}
    try:
        raw = stdin.read()
        doc = json.loads(raw or b"{}")
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    try:
        record(doc, env)
    except Exception:
        pass
    try:
        from . import usage_meter
        cfg = os.path.abspath(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
        out = usage_meter.run_saved(cfg, raw)
        if out is None:
            out = short_line(doc).encode()
        stdout.write(out)
        stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--home":      # Windows command line: no VAR=value prefix (usage_meter.command)
        os.environ["DHI_ORBIT_HOME"] = sys.argv[2]
    sys.exit(main())
