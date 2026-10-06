"""Where DHI Orbit keeps its data, and the settings a user can change.

Data directory: $DHI_ORBIT_HOME, or ~/.config/dhi-orbit. It holds the access token (mode 600), the sqlite
database, the settings file (config.json) and the hooks' logs. Nothing is ever written next to the code.
Upgrading from chatdash: the old $CHATDASH_* variables are still read (the DHI_ORBIT_* name wins), and an
existing ~/.config/chatdash (or chatdash.db inside the data dir) keeps being used until a new one exists.

config.json (every key optional; a missing or unreadable file means the defaults below):

    public_url                 "" (default). Host name the dashboard is reachable at through a tunnel you set up.
    seat_labels                {} account name -> display label. Default: derived from the config dir name.
    read_only_accounts         [] account names that are shown but never acted on (no replies, no spawns).
    work_item_pattern          "" (default: no work items). A regex; a chat whose name matches it belongs to
                               that work item, e.g. "PROJ-\\d+".
    evidence_gate_work_items   [] work item ids whose chats get the evidence gate switched on by default.
    default_cwd                "" (default: your home directory) folder for chats started from the board.
    launchd_prefix             "" (com.dhi.orbit; com.chatdash on an old chatdash data dir) label prefix of the launchd jobs the Schedules panel manages.
    timezone                   "" (default: this computer's time zone) IANA name used to show clock times.
    meter_log                  "" (default: <data dir>/meter.log) file your status line appends rate limits to.
    plugins                    {} optional helpers, see plugin().

plus the auto-action modes ("limit_resume", "decision_hook", ... each off / dry-run / on, default dry-run).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from datetime import datetime, tzinfo

DEFAULTS = {
    "public_url": "",
    "seat_labels": {},
    "read_only_accounts": [],
    "work_item_pattern": "",
    "evidence_gate_work_items": [],
    "default_cwd": "",
    "launchd_prefix": "",          # empty: com.dhi.orbit, or com.chatdash while the old data dir is in use
    "timezone": "",
    "meter_log": "",
    "plugins": {},
}

_CACHE: dict = {}


def env(name: str, default: str | None = None, environ=None) -> str | None:
    """$DHI_ORBIT_<name>, else the legacy $CHATDASH_<name>, else default (empty counts as unset)."""
    e = os.environ if environ is None else environ
    return e.get(f"DHI_ORBIT_{name}") or e.get(f"CHATDASH_{name}") or default


def home() -> str:
    h = env("HOME")
    if not h:
        h = "~/.config/dhi-orbit"
        if not os.path.exists(os.path.expanduser(h)) and os.path.exists(os.path.expanduser("~/.config/chatdash")):
            h = "~/.config/chatdash"      # a chatdash install that has not moved yet
    return os.path.abspath(os.path.expanduser(h))


def ensure_home() -> str:
    """Create the data directory on first use (mode 700) and return it."""
    h = home()
    os.makedirs(h, mode=0o700, exist_ok=True)
    return h


def token_path() -> str:
    return os.path.join(home(), ".token")


def db_path() -> str:
    if os.environ.get("CP_DB"):
        return os.environ["CP_DB"]
    new, old = os.path.join(home(), "orbit.db"), os.path.join(home(), "chatdash.db")
    return old if os.path.exists(old) and not os.path.exists(new) else new


def public_path() -> str:
    return os.path.join(home(), "public.json")


def run_dir() -> str:
    return os.path.join(home(), "run")


def logs_dir() -> str:
    return os.path.join(home(), "logs")


def work_dir() -> str:
    return os.path.join(home(), "work")


def path() -> str:
    return env("CONFIG") or os.environ.get("CP_CONFIG") or os.path.join(home(), "config.json")


def load(p: str | None = None) -> dict:
    """The parsed settings file ({} when absent or unreadable), cached until the file changes."""
    p = p or path()
    try:
        stt = os.stat(p)
        mt = (stt.st_mtime_ns, stt.st_size)
    except OSError:
        return {}
    hit = _CACHE.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    _CACHE[p] = (mt, data)
    return data


def get(key: str, default=None):
    v = load().get(key)
    if v is None:
        return DEFAULTS.get(key) if default is None else default
    return v


# ------------------------------------------------------------------ accounts
def account_label(account: str) -> str:
    labels = get("seat_labels")
    if isinstance(labels, dict) and labels.get(account):
        return str(labels[account])
    return account.replace("-", " ").replace("_", " ").capitalize()


def read_only_accounts() -> set[str]:
    v = get("read_only_accounts")
    return {str(a) for a in v} if isinstance(v, (list, tuple)) else set()


def account_of_config(cfg: str | None) -> str | None:
    """Account name of a config dir: ~/.claude is "main", ~/.claude-<name> is "<name>"."""
    if not cfg:
        return None
    b = os.path.basename(os.path.normpath(os.path.expanduser(cfg)))
    return "main" if b == ".claude" else b.replace(".claude-", "", 1)


def is_read_only(account: str | None = None, cfg: str | None = None) -> bool:
    """True for an account listed in read_only_accounts, or when there is no config dir to act on."""
    acct = account or account_of_config(cfg)
    return not acct or acct in read_only_accounts()


# ------------------------------------------------------------------ work items
def work_item_of(name: str | None) -> str | None:
    pat = get("work_item_pattern")
    if not pat or not name:
        return None
    try:
        m = re.search(pat, name)
    except re.error:
        return None
    return m.group(0) if m else None


def gate_work_items() -> set[str]:
    v = get("evidence_gate_work_items")
    return {str(x) for x in v} if isinstance(v, (list, tuple)) else set()


def default_cwd() -> str:
    c = get("default_cwd")
    return os.path.expanduser(c) if c else os.path.expanduser("~")


def launchd_prefix() -> str:
    # an install still on the old chatdash data dir keeps managing its com.chatdash jobs
    dflt = "com.chatdash" if os.path.basename(home()) == "chatdash" else "com.dhi.orbit"
    p = str(get("launchd_prefix") or dflt)
    return p if re.fullmatch(r"[A-Za-z0-9][\w.-]*", p) else dflt


def claude_bin() -> str:
    """The `claude` binary: $CLAUDE_BIN, else the first one on PATH, else ~/.local/bin/claude."""
    env = os.environ.get("CLAUDE_BIN")
    if env:
        return env
    import shutil
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def public_url() -> str:
    return str(get("public_url") or "")


# ------------------------------------------------------------------ time
def tz() -> tzinfo:
    name = get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


# ------------------------------------------------------------------ optional parts
def meter_log_path() -> str:
    env = os.environ.get("CP_METER_LOG")
    if env:
        return env
    p = get("meter_log")
    return os.path.expanduser(p) if p else os.path.join(home(), "meter.log")


def plugin(name: str):
    """An optional helper module named in config.json, or None when it is not configured or not importable.

        "plugins": {"usage_live": {"path": "/some/dir", "module": "usage_live"}}

    A feature that needs a plugin is simply unavailable without it; nothing is guessed or faked."""
    spec = (get("plugins") or {}).get(name) if isinstance(get("plugins"), dict) else None
    if not isinstance(spec, dict) or not spec.get("path"):
        return None
    d = os.path.expanduser(str(spec["path"]))
    if not os.path.isdir(d):
        return None
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        return importlib.import_module(str(spec.get("module") or name))
    except Exception:
        return None
