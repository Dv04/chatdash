#!/usr/bin/env python3
"""Optional: feed DHI Orbit's usage gauges from a Claude Code status line command.

Claude Code runs a status line command and passes it a JSON document on stdin. When that document carries
a "rate_limits" object (five_hour / seven_day with used_percentage and resets_at), this script appends one
line to DHI Orbit's meter log whenever the numbers change:

    <ISO time> TAB <config dir> TAB <session id> TAB <rate_limits JSON>

DHI Orbit reads that log (config meter_log, default <data dir>/meter.log). Without it the limits show as
UNKNOWN; nothing is ever guessed.

Use it from your own status line command, for example a script that runs
    python3 /path/to/statusline-meter.py < "$INPUT_FILE"
and then prints whatever status text you like. This script prints nothing and never fails the status line:
any error is swallowed.
"""
import json
import os
import sys
import time


def main(stdin=sys.stdin, env=os.environ) -> int:
    try:
        doc = json.load(stdin)
        limits = doc.get("rate_limits")
        if not isinstance(limits, dict) or not limits:
            return 0
        home = env.get("DHI_ORBIT_HOME") or env.get("CHATDASH_HOME")      # CHATDASH_HOME: pre-rename installs
        if not home:
            home = "~/.config/dhi-orbit"
            if not os.path.exists(os.path.expanduser(home)) and os.path.exists(os.path.expanduser("~/.config/chatdash")):
                home = "~/.config/chatdash"
        home = os.path.abspath(os.path.expanduser(home))
        os.makedirs(home, mode=0o700, exist_ok=True)
        cfg = os.path.abspath(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
        blob = json.dumps(limits, separators=(",", ":"), sort_keys=True)
        state_path = os.path.join(home, "meter.state")
        try:
            with open(state_path) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        if state.get(cfg) == blob:
            return 0
        state[cfg] = blob
        with open(os.path.join(home, "meter.log"), "a") as fh:
            fh.write("\t".join([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cfg,
                                str(doc.get("session_id") or ""), blob]) + "\n")
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, state_path)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
