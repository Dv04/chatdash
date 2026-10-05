"""Usage limits for every account without setup: chatdash's status line meter, per account.

Claude Code passes its status line command a JSON document on stdin that carries "rate_limits" (five_hour and
seven_day, each with used_percentage and resets_at). It does so for every chat on the account, background ones
included (measured 2026-10-05: a background session wrote 18 meter lines). chatdash records those numbers from a
status line of its own, so limits show up without the owner writing a script.

turn_on(dir) sets the account's settings.json "statusLine" to `chatdash-statusline`. If the account already has a
status line, that one is saved next to settings.json (chatdash-statusline.json) and still runs: the meter records the
limits, then runs the saved command with the same input and prints its output, so the terminal looks the same.
turn_off(dir) puts the saved status line back exactly (or removes the key when there was none). A status line the
owner changed after chatdash set its own is never touched.

Claude Code only runs the status line while a chat is open, so a fresh account has no reading until its first
turn; the board says "waiting for the first chat" for that, not a guess.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from . import config

SAVED = "chatdash-statusline.json"      # the owner's own status line, kept while chatdash's is on
MARK = "chatdash.statusline"            # in every command chatdash writes; how its own status line is recognised


def _settings(d: str) -> str:
    return os.path.join(d, "settings.json")


def _load(path: str) -> dict:
    """settings.json as a dict; {} when missing. Raises ValueError for a file that is not a JSON object, so a
    hand-edited file is never overwritten."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def _write(path: str, data: dict) -> None:
    mode = 0o600
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    tmp = path + ".chatdash.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def command() -> str:
    """The status line command: this interpreter running chatdash.statusline, with chatdash's data dir pinned so the
    meter lands where this chatdash reads it."""
    return (f"CHATDASH_HOME={shlex.quote(config.home())} "
            f"{shlex.quote(sys.executable)} -m {MARK}")


def is_ours(sl) -> bool:
    return isinstance(sl, dict) and MARK in str(sl.get("command") or "")


def status(d: str) -> dict:
    """{"on": bool, "wrapped": bool (the owner's own status line still runs), "other": bool (a status line that is
    not chatdash's), "error": str|None}."""
    try:
        sl = _load(_settings(d)).get("statusLine")
    except (OSError, ValueError) as e:
        return {"on": False, "wrapped": False, "other": False, "error": f"settings.json unreadable: {e}"[:200]}
    return {"on": is_ours(sl), "wrapped": is_ours(sl) and os.path.exists(os.path.join(d, SAVED)),
            "other": bool(sl) and not is_ours(sl), "error": None}


_cache: dict[str, tuple] = {}


def meter_state(d: str) -> str:
    """"on", "other" (a status line that is not chatdash's) or "off", cached on settings.json's mtime: the board asks
    for every seat on every refresh."""
    p = _settings(d)
    try:
        key = os.stat(p).st_mtime_ns
    except OSError:
        key = None
    hit = _cache.get(d)
    if hit and hit[0] == key:
        return hit[1]
    st = status(d)
    v = "on" if st["on"] else "other" if st["other"] else "off"
    _cache[d] = (key, v)
    return v


def turn_on(d: str) -> dict:
    if not os.path.isdir(d):
        return {"ok": False, "error": "no such account folder"}
    p = _settings(d)
    try:
        data = _load(p)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"settings.json could not be read, so it was left alone: {e}"[:300]}
    cur = data.get("statusLine")
    if is_ours(cur):
        if cur.get("command") != command():                  # chatdash moved (reinstall): point at this copy
            data["statusLine"] = dict(cur, command=command())
            _write(p, data)
        return {"ok": True, "on": True, "wrapped": os.path.exists(os.path.join(d, SAVED))}
    if cur:
        with open(os.path.join(d, SAVED), "w", encoding="utf-8") as fh:
            json.dump({"statusLine": cur}, fh, indent=2)
    new = {k: v for k, v in cur.items() if k not in ("type", "command")} if isinstance(cur, dict) else {}
    data["statusLine"] = {"type": "command", "command": command(), **new}
    _write(p, data)
    return {"ok": True, "on": True, "wrapped": bool(cur)}


def turn_off(d: str) -> dict:
    p = _settings(d)
    saved_path = os.path.join(d, SAVED)
    try:
        data = _load(p)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"settings.json could not be read, so it was left alone: {e}"[:300]}
    if not is_ours(data.get("statusLine")):
        return {"ok": True, "on": False, "note": "the status line is not chatdash's; left as it is"}
    try:
        with open(saved_path, encoding="utf-8") as fh:
            orig = json.load(fh).get("statusLine")
    except (OSError, ValueError):
        orig = None
    if orig:
        data["statusLine"] = orig
    else:
        data.pop("statusLine", None)
    _write(p, data)
    try:
        os.remove(saved_path)
    except OSError:
        pass
    return {"ok": True, "on": False, "restored": bool(orig)}


def saved_command(d: str) -> str | None:
    try:
        with open(os.path.join(d, SAVED), encoding="utf-8") as fh:
            sl = json.load(fh).get("statusLine")
    except (OSError, ValueError):
        return None
    return sl.get("command") if isinstance(sl, dict) and sl.get("type", "command") == "command" else None


def run_saved(d: str, raw: bytes, timeout: float = 5.0) -> bytes | None:
    """Run the owner's own status line with the same input; None when there is none."""
    cmd = saved_command(d)
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, input=raw, capture_output=True, timeout=timeout, cwd=os.getcwd())
        return p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return b""
