"""Usage limits for every account without setup: DHI Orbit's status line meter, per account.

Claude Code passes its status line command a JSON document on stdin that carries "rate_limits" (five_hour and
seven_day, each with used_percentage and resets_at). It does so for every chat on the account, background ones
included (measured 2026-10-05: a background session wrote 18 meter lines). DHI Orbit records those numbers from a
status line of its own, so limits show up without the owner writing a script.

turn_on(dir) sets the account's settings.json "statusLine" to `dhi-orbit-statusline`. If the account already has a
status line, that one is saved next to settings.json (dhi-orbit-statusline.json) and still runs: the meter records the
limits, then runs the saved command with the same input and prints its output, so the terminal looks the same.
turn_off(dir) puts the saved status line back exactly (or removes the key when there was none). A status line the
owner changed after DHI Orbit set its own is never touched.

A meter installed by chatdash (before the rename) is still recognised (LEGACY_MARK), its saved file
(chatdash-statusline.json) is still read and removed, and turn_on rewrites its command to the new one.

Claude Code only runs the status line while a chat is open, so a fresh account has no reading until its first
turn; the board says "waiting for the first chat" for that, not a guess.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from . import _plat, config

SAVED = "dhi-orbit-statusline.json"     # the owner's own status line, kept while DHI Orbit's is on
MARK = "dhi_orbit.statusline"           # in every command DHI Orbit writes; how its own status line is recognised
LEGACY_SAVED = "chatdash-statusline.json"   # SAVED as written by chatdash before the rename
LEGACY_MARK = "chatdash.statusline"         # MARK as written by chatdash before the rename


def _settings(d: str) -> str:
    return os.path.join(d, "settings.json")


def _saved(d: str) -> str:
    """Path of the saved status line: the new file, else the legacy one if only that exists, else the new name."""
    new, old = os.path.join(d, SAVED), os.path.join(d, LEGACY_SAVED)
    return old if os.path.exists(old) and not os.path.exists(new) else new


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
    tmp = path + ".dhi-orbit.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _plat.O_BIN, mode), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def command() -> str:
    """The status line command: this interpreter running dhi_orbit.statusline, with the data dir pinned so the
    meter lands where this DHI Orbit reads it."""
    if _plat.IS_WIN:
        # Claude Code runs a status line command through Git Bash when it is installed, else through PowerShell (docs:
        # Customize your status line > Windows configuration). They disagree about VAR=value prefixes, so the data dir is an
        # argument; forward slashes survive Git Bash (it eats backslashes). A PowerShell command that starts with a quoted
        # string is just a string, so there the interpreter is called with &; Git Bash would reject a leading &.
        q = lambda p: '"' + p.replace("\\", "/") + '"'
        exe = shutil.which("dhi-orbit-statusline")
        if exe:                                                         # the console script is a bare name in every shell
            return f"dhi-orbit-statusline --home {q(config.home())}"
        lead = "" if _has_git_bash() else "& "
        return f"{lead}{q(sys.executable)} -m {MARK} --home {q(config.home())}"
    return (f"DHI_ORBIT_HOME={shlex.quote(config.home())} "
            f"{shlex.quote(sys.executable)} -m {MARK}")


def _has_git_bash() -> bool:
    for p in (os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"), r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if p and os.path.isfile(p):
            return True
    return False


def is_ours(sl) -> bool:
    cmd = str(sl.get("command") or "") if isinstance(sl, dict) else ""
    return MARK in cmd or LEGACY_MARK in cmd or "dhi-orbit-statusline" in cmd


def missing_interpreter(cmd: str) -> str | None:
    """The absolute interpreter path a command starts with (after any leading VAR=value assignments) when that file
    is gone, e.g. a pipx venv deleted by an upgrade; None when it exists, is not an absolute path, or cannot be parsed."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    for w in words:
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w):
            continue
        return w if os.path.isabs(w) and not os.path.exists(w) else None
    return None


def status(d: str) -> dict:
    """{"on": bool, "wrapped": bool (the owner's own status line still runs), "other": bool (a status line that is
    not DHI Orbit's), "error": str|None}. A status line that is DHI Orbit's but points at an interpreter that no
    longer exists is not on: it adds "stale": True and names the missing path (repair() or turn_on fixes it)."""
    try:
        sl = _load(_settings(d)).get("statusLine")
    except (OSError, ValueError) as e:
        return {"on": False, "wrapped": False, "other": False, "error": f"settings.json unreadable: {e}"[:200]}
    ours = is_ours(sl)
    gone = missing_interpreter(sl["command"]) if ours and sl["command"] != command() else None
    if gone:
        return {"on": False, "wrapped": os.path.exists(_saved(d)), "other": False, "stale": True,
                "error": f"status line points at a missing interpreter: {gone}"[:200]}
    return {"on": ours, "wrapped": ours and os.path.exists(_saved(d)),
            "other": bool(sl) and not ours, "error": None}


def repair(d: str) -> bool:
    """Point a status line that is DHI Orbit's (new or legacy mark) at command() when its interpreter no longer exists
    (an uninstalled or upgraded copy). A working line of ours is left alone even when it differs, so two installs on
    one account never take turns rewriting it. The saved original is kept; a status line that is not ours, or
    settings that cannot be read, is never touched. True when the settings file was rewritten."""
    try:
        sl = _load(_settings(d)).get("statusLine")
    except (OSError, ValueError):
        return False
    if not is_ours(sl) or sl.get("command") == command() or not missing_interpreter(str(sl.get("command") or "")):
        return False
    try:
        return bool(turn_on(d).get("ok"))
    except OSError:
        return False


_cache: dict[str, tuple] = {}


def meter_state(d: str) -> str:
    """"on", "other" (a status line that is not DHI Orbit's) or "off", cached on settings.json's mtime: the board asks
    for every seat on every refresh. A cache miss is also where a status line of ours with an outdated command is
    repaired, so the settings file is rewritten only when the command differs, not on every refresh."""
    p = _settings(d)
    try:
        key = os.stat(p).st_mtime_ns
    except OSError:
        key = None
    hit = _cache.get(d)
    if hit and hit[0] == key:
        return hit[1]
    if repair(d):
        try:
            key = os.stat(p).st_mtime_ns
        except OSError:
            key = None
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
        if cur.get("command") != command():                  # moved (reinstall) or set by chatdash: point at this copy
            data["statusLine"] = dict(cur, command=command())
            _write(p, data)
        return {"ok": True, "on": True, "wrapped": os.path.exists(_saved(d))}
    if cur:
        with open(os.path.join(d, SAVED), "w", encoding="utf-8") as fh:
            json.dump({"statusLine": cur}, fh, indent=2)
    new = {k: v for k, v in cur.items() if k not in ("type", "command")} if isinstance(cur, dict) else {}
    data["statusLine"] = {"type": "command", "command": command(), **new}
    _write(p, data)
    return {"ok": True, "on": True, "wrapped": bool(cur)}


def turn_off(d: str) -> dict:
    p = _settings(d)
    saved_path = _saved(d)
    try:
        data = _load(p)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"settings.json could not be read, so it was left alone: {e}"[:300]}
    if not is_ours(data.get("statusLine")):
        return {"ok": True, "on": False, "note": "the status line is not DHI Orbit's; left as it is"}
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
        with open(_saved(d), encoding="utf-8") as fh:
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
