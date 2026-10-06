#!/bin/zsh
# SwiftBar plugin (optional, B10): the needs-you count in the macOS menu bar.
# Install: copy into your SwiftBar plugin folder and make it executable. Reads the DHI Orbit key at run time
# from the data dir ($DHI_ORBIT_HOME or ~/.config/dhi-orbit)/.token (never stored here). Talks only to 127.0.0.1.
# A chatdash install still works: $CHATDASH_HOME, $CHATDASH_PORT and ~/.config/chatdash are read when the new ones are absent.
PORT=${DHI_ORBIT_PORT:-${CHATDASH_PORT:-8787}}
HOME_DIR=${DHI_ORBIT_HOME:-$CHATDASH_HOME}
[[ -z "$HOME_DIR" && ! -d "$HOME/.config/dhi-orbit" && -d "$HOME/.config/chatdash" ]] && HOME_DIR="$HOME/.config/chatdash"
TOK=$(cat "${HOME_DIR:-$HOME/.config/dhi-orbit}/.token" 2>/dev/null)
J=$(curl -s -m 3 -H "X-Token: $TOK" "http://127.0.0.1:$PORT/api/cp/overview")
if [[ -z "$J" ]]; then
  echo "cp ? | sfimage=exclamationmark.triangle color=orange"; echo "---"; echo "DHI Orbit not reachable on $PORT"; exit 0
fi
/usr/bin/python3 - "$J" "$PORT" <<'PY'
import json, sys
d = json.loads(sys.argv[1]); port = sys.argv[2]
n = len(d.get("needs_you", [])); ok = d.get("health") == "ok"
print(f"{n} | sfimage={'bell.badge' if n else 'bell'}" + ("" if ok else " color=orange"))
print("---")
print(("Data OK" if ok else "Data UNKNOWN: " + "; ".join(d.get("health_reasons", [])[:2])) + " | color=gray")
for x in sorted(d.get("needs_you", []), key=lambda x: x.get("since") or 0)[:12]:
    t = (x.get("title") or "")[:60].replace("|", "/")
    href = f"http://127.0.0.1:{port}/v2/#/decision/{x.get('decision_id')}" if x.get("decision_id") else f"http://127.0.0.1:{port}/v2/"
    print(f"{t} ({x.get('kind')}) | href={href}")
print(f"Open mission board | href=http://127.0.0.1:{port}/v2/")
PY
