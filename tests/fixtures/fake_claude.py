#!/usr/bin/env python3
"""A stand-in for the `claude` CLI's auth commands, for tests: state lives in $CLAUDE_CONFIG_DIR/.fake-auth."""
import json
import os
import sys

d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
f = os.path.join(d, ".fake-auth")
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    on = os.path.exists(f)
    print(json.dumps({"loggedIn": on, "authMethod": "claude.ai" if on else "none", "subscriptionType": "pro" if on else None,
                      "email": "someone@example.com" if on else None, "orgName": None}))
elif args[:2] == ["auth", "logout"]:
    if os.path.exists(f):
        os.remove(f)
    print("Successfully logged out")
elif args[:2] == ["auth", "login"]:
    print("Opening browser to sign in...")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good-code":
        open(f, "w").write("1")
        print("Login successful")
        sys.exit(0)
    print("Invalid code")
    sys.exit(1)
else:
    sys.exit(2)
