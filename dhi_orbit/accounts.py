"""Claude accounts: list, connect (sign in), disconnect, reconnect and delete, from the board.

An account is a Claude Code config dir: ~/.claude ("main") or ~/.claude-<name>. Signing in runs the official
`claude auth login` for that dir in a pseudo-terminal. Claude Code opens this computer's browser at claude.com
and finishes by itself when the owner approves there; it also prints a fallback link whose page shows a code, and
the board can type that code into the same prompt. DHI Orbit never sees, stores or sends a password or token:
the credentials are written by Claude Code into its own config dir, exactly as when you sign in in a terminal.

Usage limits: connecting an account with no status line of its own turns on DHI Orbit's status line meter
(usage_meter.py), so its 5-hour and 7-day limits appear after its first chat; any account can turn it on or off
from Settings. Disconnect only hides an account from the board (its sign-in and chats are untouched) and puts its
own status line back; Reconnect turns the meter on again if it was on. Delete signs the account
out with `claude auth logout` and moves its config dir to the Trash (never for ~/.claude, which is only signed
out), and refuses while one of its chats is running.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time

from . import config, usage_meter

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
URL_RE = re.compile(r"https://[^\s\x07\x1b\]]+")
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]")
MARK = ".dhi-orbit-account"         # written into dirs DHI Orbit created
LEGACY_MARK = ".chatdash-account"   # the same, written by chatdash before the rename
LOGIN_TIMEOUT_S = 60 * 60          # a person may take a while to come back with the code

_logins: dict[str, "Login"] = {}
_lock = threading.Lock()


def root() -> str:
    """Where account dirs live: $DHI_ORBIT_ACCOUNTS_ROOT (tests) or the home directory."""
    return os.path.abspath(os.path.expanduser(config.env("ACCOUNTS_ROOT", "~")))


def created_here(d: str) -> bool:
    return any(os.path.exists(os.path.join(d, m)) for m in (MARK, LEGACY_MARK))


def dir_of(name: str) -> str:
    return os.path.join(root(), ".claude" if name == "main" else f".claude-{name}")


def valid_name(name: str) -> bool:
    return name == "main" or bool(NAME_RE.match(name or ""))


def hidden() -> set[str]:
    v = config.get("hidden_accounts")
    return {str(a) for a in v} if isinstance(v, (list, tuple)) else set()


def discovered() -> list[str]:
    """Every account dir on disk (hidden ones included): ~/.claude and ~/.claude-<name> with projects/ and sessions/."""
    out = []
    r = root()
    try:
        names = sorted(os.listdir(r))
    except OSError:
        return out
    for b in names:
        if b != ".claude" and not b.startswith(".claude-"):
            continue
        d = os.path.join(r, b)
        if os.path.isdir(os.path.join(d, "projects")) and os.path.isdir(os.path.join(d, "sessions")):
            out.append(d)
    return out


def account_name(d: str) -> str:
    b = os.path.basename(d)
    return "main" if b == ".claude" else b[len(".claude-"):]


def _env(d: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE"))}
    env.update(TERM="xterm-256color", COLUMNS="200", LINES="50")
    if os.path.basename(d) == ".claude" and os.path.normpath(root()) == os.path.normpath(os.path.expanduser("~")):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = d
    return env


def auth_status(d: str) -> dict:
    """`claude auth status --json` for a dir: {"signed_in", "method", "plan", "email", "org"} or an error."""
    try:
        p = subprocess.run([config.claude_bin(), "auth", "status", "--json"], env=_env(d), capture_output=True,
                           text=True, timeout=20, stdin=subprocess.DEVNULL)
        j = json.loads(p.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"signed_in": None, "error": f"{type(e).__name__}: {e}"[:200]}
    return {"signed_in": bool(j.get("loggedIn")), "method": j.get("authMethod"), "plan": j.get("subscriptionType"),
            "email": j.get("email"), "org": j.get("orgName")}


def _live(d: str) -> int:
    """Running Claude Code processes of this account (sessions/<pid>.json whose pid is alive)."""
    n = 0
    for f in os.listdir(os.path.join(d, "sessions")) if os.path.isdir(os.path.join(d, "sessions")) else []:
        if f.endswith(".json") and f.split(".")[0].isdigit():
            try:
                os.kill(int(f.split(".")[0]), 0)
                n += 1
            except (OSError, ValueError):
                pass
    return n


def listing(with_status: bool = True) -> list[dict]:
    hid = hidden()
    out = []
    for d in discovered():
        name = account_name(d)
        row = {"name": name, "label": config.account_label(name), "dir": d, "hidden": name in hid,
               "created_here": created_here(d), "read_only": config.is_read_only(name),
               "running": _live(d), "login": _logins[name].view() if name in _logins else None,
               "usage_meter": usage_meter.status(d)}
        if with_status:
            row.update(auth_status(d))
        out.append(row)
    return out


# ------------------------------------------------------------------ settings writes
def _save(key: str, value) -> None:
    p = config.path()
    config.ensure_home()
    data = dict(config.load(p))
    data[key] = value
    tmp = p + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _paused() -> set[str]:
    v = config.get("meter_paused_accounts")
    return {str(a) for a in v} if isinstance(v, (list, tuple)) else set()


def disconnect(name: str) -> dict:
    if not any(account_name(d) == name for d in discovered()):
        return {"ok": False, "error": "no such account"}
    _save("hidden_accounts", sorted(hidden() | {name}))
    if usage_meter.status(dir_of(name))["on"] and usage_meter.turn_off(dir_of(name)).get("ok"):
        _save("meter_paused_accounts", sorted(_paused() | {name}))     # Reconnect turns it back on
    return {"ok": True, "hidden": True}


def reconnect(name: str) -> dict:
    _save("hidden_accounts", sorted(hidden() - {name}))
    if name in _paused():
        usage_meter.turn_on(dir_of(name))
        _save("meter_paused_accounts", sorted(_paused() - {name}))
    return {"ok": True, "hidden": False}


def usage(name: str, on: bool) -> dict:
    """Turn the status line meter on or off for one account (Settings > Accounts)."""
    d = dir_of(name)
    if not os.path.isdir(d):
        return {"ok": False, "error": "no such account"}
    _save("meter_paused_accounts", sorted(_paused() - {name}))
    return usage_meter.turn_on(d) if on else usage_meter.turn_off(d)


def delete(name: str, confirm: str) -> dict:
    """Sign out and move the account dir to the Trash. Needs confirm == name; never removes ~/.claude."""
    if confirm != name:
        return {"ok": False, "error": "type the account name to confirm"}
    d = dir_of(name)
    if not os.path.isdir(d):
        return {"ok": False, "error": "no such account"}
    if _live(d):
        return {"ok": False, "error": "a chat on this account is still running; stop it first"}
    cancel(name)
    try:
        subprocess.run([config.claude_bin(), "auth", "logout"], env=_env(d), capture_output=True, text=True,
                       timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    st = auth_status(d)
    if st.get("signed_in"):
        return {"ok": False, "error": "sign-out did not take effect; nothing was moved"}
    if name == "main":
        usage_meter.turn_off(d)                    # ~/.claude stays: put its own status line back
        _save("hidden_accounts", sorted(hidden() | {name}))
        return {"ok": True, "signed_out": True, "moved_to": None,
                "note": "~/.claude is your default Claude Code folder: signed out and hidden, not moved"}
    trash = os.path.join(os.path.expanduser(config.env("TRASH", "~/.Trash")),
                         f"{os.path.basename(d)}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(trash), exist_ok=True)
    shutil.move(d, trash)
    _save("hidden_accounts", sorted(hidden() - {name}))
    return {"ok": True, "signed_out": True, "moved_to": trash}


# ------------------------------------------------------------------ sign in
class Login:
    """One `claude auth login` running in a pseudo-terminal for one account dir."""

    def __init__(self, name: str, d: str, console: bool = False):
        self.name, self.dir, self.started = name, d, time.time()
        self.state, self.error, self.urls, self.prompt, self.tail = "starting", None, [], False, ""
        self.buf, self.code_mark, self.notice = "", None, None
        self.result: dict | None = None
        args = [config.claude_bin(), "auth", "login"] + (["--console"] if console else [])
        # A pty pair and subprocess, not pty.fork(): forking a multi-threaded server can deadlock the child.
        self.fd, slave = os.openpty()
        try:
            self.proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=_env(d), cwd=d,
                                         start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self.pid = self.proc.pid
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        buf = ""
        while True:
            if time.time() - self.started > LOGIN_TIMEOUT_S:
                self._stop("failed", "sign-in timed out after 60 minutes: start it again")
                break
            try:
                r, _, _ = select.select([self.fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf = buf + ANSI_RE.sub("", chunk.decode(errors="replace"))
                if len(buf) > 40000:                       # keep the mark of the last code in step with the trim
                    cut = len(buf) - 20000
                    buf = buf[cut:]
                    if self.code_mark is not None:
                        self.code_mark = max(0, self.code_mark - cut)
                self.buf = buf
                if self.code_mark is not None:          # what Claude Code said after the last code, e.g. "Invalid code"
                    m = re.search(r"(invalid[^\n>]*|error[^\n>]*|expired[^\n>]*)", buf[self.code_mark:], re.I)
                    self.notice = m.group(1).strip()[:200] if m else None
                for u in URL_RE.findall(buf):
                    u = u.rstrip(".,)")
                    if u not in self.urls:
                        self.urls.append(u)
                self.prompt = "paste code" in buf.lower()
                if self.state == "starting" and (self.urls or self.prompt):
                    self.state = "waiting"
                self.tail = buf[-600:]
            if self.proc.poll() is not None:
                self.pid = 0
                break
        if self.state in ("starting", "waiting"):
            st = auth_status(self.dir)
            self.result = st
            self.state = "done" if st.get("signed_in") else "failed"
            if self.state == "failed" and not self.error:
                self.error = "sign-in did not complete" + (f": {self.tail.strip()[-200:]}" if self.tail.strip() else "")
        try:
            os.close(self.fd)
        except OSError:
            pass

    def code(self, text: str) -> dict:
        text = (text or "").strip()
        if self.state != "waiting":
            return {"ok": False, "error": f"sign-in is {self.state}, not waiting for a code"}
        if not text or len(text) > 2000 or any(c in text for c in "\r\n\x1b"):
            return {"ok": False, "error": "that does not look like a sign-in code"}
        self.code_mark, self.notice = len(self.buf), None
        try:
            os.write(self.fd, text.encode() + b"\r")            # typed into Claude Code's own prompt, never stored
        except OSError as e:
            return {"ok": False, "error": f"the sign-in prompt is gone ({e.strerror})"}
        return {"ok": True}

    def _stop(self, state: str, error: str | None = None) -> None:
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        self.state, self.error = state, error

    def view(self) -> dict:
        # The fallback link (code shown on claude.com) is the one to open from another device; the localhost one only
        # works in this computer's browser, which Claude Code opens by itself.
        manual = next((u for u in self.urls if "localhost" not in u and "127.0.0.1" not in u), None)
        return {"state": self.state, "error": self.error, "notice": self.notice, "link": manual,
                "wants_code": self.prompt and self.state == "waiting",
                "age_s": round(time.time() - self.started), "result": self.result}


def start_login(name: str, console: bool = False, restart: bool = False) -> dict:
    """Create the account dir if needed and start signing it in."""
    if not valid_name(name):
        return {"ok": False, "error": "name: lowercase letters, digits and dashes, up to 24 characters"}
    d = dir_of(name)
    with _lock:
        cur = _logins.get(name)
        if cur and cur.state in ("starting", "waiting"):
            if not restart:
                return {"ok": True, "login": cur.view(), "already": True}
            cur._stop("cancelled")                     # start over: a fresh sign-in link and prompt
        fresh = not os.path.exists(d)
        os.makedirs(os.path.join(d, "projects"), exist_ok=True)
        os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
        if fresh:
            open(os.path.join(d, MARK), "w").close()
        if name in hidden():
            reconnect(name)
        st = usage_meter.status(d)
        if not st["on"] and not st["other"] and not st["error"]:
            usage_meter.turn_on(d)                 # nothing of the owner's to keep: limits show after the first chat
        try:
            _logins[name] = Login(name, d, console)
        except OSError as e:
            return {"ok": False, "error": f"could not start claude ({e.strerror})"}
    return {"ok": True, "login": _logins[name].view(), "created": fresh}


def login_view(name: str) -> dict:
    lg = _logins.get(name)
    return {"ok": True, "login": lg.view() if lg else None}


def submit_code(name: str, code: str) -> dict:
    lg = _logins.get(name)
    return lg.code(code) if lg else {"ok": False, "error": "no sign-in in progress for this account"}


def cancel(name: str) -> dict:
    lg = _logins.get(name)
    if lg and lg.state in ("starting", "waiting"):
        lg._stop("cancelled")
    return {"ok": True}
