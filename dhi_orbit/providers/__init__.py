"""Other AI coding tools on the board: OpenAI Codex CLI and Cursor CLI, next to Claude Code.

They are deliberately a separate list, not mixed into the Claude chats: every automation in DHI Orbit (limit resume,
idle compaction, keep-warm, handoff) drives the `claude` binary by session id and must never be pointed at another
tool's session. Here a chat can be read, and answered by hand; nothing acts on it by itself.

A provider module offers: LABEL, info() -> {available, version, detail}, chats(since, limit) -> [chat],
turns(id, limit) -> [turn], reply(id, text) -> {ok, ...}. A chat is a dict with the same core fields as a Claude chat
(key, name, cwd, state, final, last_prompt) plus provider, id, updated_at, created_at, model and source.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

from .. import _plat, config

_CACHE: dict = {}
_LOCK = threading.Lock()
TTL_S = 4.0


def _modules() -> dict:
    from . import codex, cursor
    return {"codex": codex, "cursor": cursor}


def providers() -> dict:
    return _modules()


def overview(window_s: float = 7 * 86400, limit: int = 120) -> list[dict]:
    """Every provider with its recent chats, newest first. Cached for a few seconds: the page polls this."""
    hit = _CACHE.get(("ov", window_s, limit))
    if hit and time.time() - hit[0] < TTL_S:
        return hit[1]
    since = time.time() - window_s
    out = []
    for pid, mod in _modules().items():
        try:
            info = mod.info()
            chats = mod.chats(since, limit) if info.get("available") else []
            err = None
        except Exception as e:                 # one tool's odd data must never blank the other
            info, chats, err = {"available": False, "version": None, "detail": None}, [], f"{type(e).__name__}: {e}"[:200]
        out.append({"id": pid, "label": mod.LABEL, **info, "error": err, "chats": chats})
    with _LOCK:
        _CACHE[("ov", window_s, limit)] = (time.time(), out)
    return out


def chat(provider: str, cid: str, limit: int = 80) -> dict | None:
    mod = _modules().get(provider)
    if not mod:
        return None
    for p in overview(30 * 86400, 400):
        if p["id"] == provider:
            c = next((c for c in p["chats"] if c["id"] == cid), None)
            break
    else:
        c = None
    if c is None:
        c = mod.find(cid)
    if c is None:
        return None
    return {"chat": c, "turns": mod.turns(cid, limit)}


def reply(provider: str, cid: str, text: str) -> dict:
    mod = _modules().get(provider)
    if not mod:
        return {"ok": False, "error": f"unknown provider {provider!r}"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    if len(text) > 20000:
        return {"ok": False, "error": "message too long"}
    _CACHE.clear()
    return mod.reply(cid, text)


def enabled() -> dict:
    off = config.get("providers_off")
    off = {str(x) for x in off} if isinstance(off, (list, tuple)) else set()
    return {pid: mod for pid, mod in _modules().items() if pid not in off}


def rows(window_s: float) -> list[dict]:
    """Every enabled provider's chats in the window as board rows. A provider that is not installed or whose data cannot be
    read contributes nothing (and never raises): the Claude Code chats must always show."""
    from . import board as _rows
    out = []
    en = enabled()
    for p in overview(window_s, 200):
        if p["id"] not in en or not p.get("available"):
            continue
        for c in p["chats"]:
            out.append(_rows.row(c, p["label"], config.work_item_of))
    return out


def seats(rows_: list[dict]) -> list[dict]:
    """A pseudo-seat for each provider that has rows, in the shape of a Claude Code seat."""
    from . import board as _rows
    have = {}
    for r in rows_:
        if r.get("provider"):                 # a Claude Code row has no provider
            have.setdefault(r["provider"], r["provider_label"])
    out = []
    for pid, label in have.items():
        mod = _modules().get(pid)
        try:
            lim = mod.limits() if mod and hasattr(mod, "limits") else None
        except Exception:                       # an unreadable usage reading is "unknown", never an error on the board
            lim = None
        s = _rows.seat(pid, label, [], lim)
        if s["resume_at"]:
            from ..cp import limits as _limits
            s["resume_at_ct"] = _limits.fmt_clock(s["resume_at"])
        out.append(s)
    return out


def _turn_pairs(msgs: list[dict], fallback_ts: str | None) -> list[dict]:
    """Messages -> the {prompt, pts, final, fts, texts} turns the search index and digest read."""
    from . import board as _rows
    turns: list[dict] = []
    for m in msgs:
        ts = _rows.iso(m.get("at")) or fallback_ts
        if m["role"] == "user":
            turns.append({"prompt": m["text"], "pts": ts, "final": "", "fts": None, "texts": []})
        else:
            if not turns:
                turns.append({"prompt": "", "pts": None, "final": "", "fts": None, "texts": []})
            turns[-1]["texts"].append(m["text"])
            turns[-1]["final"] = m["text"]
            turns[-1]["fts"] = ts
    return turns


def _messages(path: str, limit: int = 400) -> list[dict]:
    from . import board as _rows
    prov, cid = _rows.split_path(path)
    mod = _modules().get(prov)
    return mod.turns(cid, limit) if mod else []


def turns_for_path(path: str) -> list[dict]:
    return _turn_pairs(_messages(path), None)


def entries_for_path(path: str) -> list[dict]:
    """The chat view's entries (see cp/transcript.py) for a provider chat: user and assistant messages, in order."""
    from . import board as _rows
    out = []
    for i, m in enumerate(_messages(path, 600)):
        out.append({"kind": "user" if m["role"] == "user" else "text", "ts": _rows.iso(m.get("at")), "text": m["text"], "i": i})
    return out


def log_dir() -> str:
    d = os.path.join(config.home(), "logs")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def run_detached(argv: list[str], cwd: str, env: dict | None, tag: str, settle_s: float = 6.0) -> dict:
    """Start a command that outlives this request (a turn can take minutes) with its output in the logs folder, then
    wait `settle_s` for an early failure. Returns {ok, started|finished, log, tail}. A command that is still running after
    settle_s is reported as started: the board shows the chat working while the tool writes its turn."""
    log = os.path.join(log_dir(), f"{tag}-{int(time.time())}.log")
    kw: dict = {"cwd": cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~"), "env": env,
                "stdin": subprocess.DEVNULL}
    if _plat.IS_WIN:
        kw["creationflags"] = 0x00000200 | 0x08000000          # new process group, no console window
    else:
        kw["start_new_session"] = True
    try:
        with open(log, "wb") as fh:
            p = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, **kw)
    except OSError as e:
        return {"ok": False, "error": f"could not start {os.path.basename(argv[0])}: {e.strerror or e}"}
    end = time.time() + settle_s
    while time.time() < end and p.poll() is None:
        time.sleep(0.1)
    tail = ""
    try:
        with open(log, "rb") as fh:
            fh.seek(max(0, os.path.getsize(log) - 1500))
            tail = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", fh.read().decode("utf-8", "replace")).strip()
    except OSError:
        pass
    rc = p.poll()
    if rc is None:
        return {"ok": True, "started": True, "pid": p.pid, "log": log}
    if rc == 0:
        return {"ok": True, "finished": True, "log": log, "tail": tail[-600:]}
    return {"ok": False, "error": f"{os.path.basename(argv[0])} exited with code {rc}: {tail[-400:]}", "log": log}


def which(name: str, env_var: str) -> str | None:
    import shutil
    return os.environ.get(env_var) or shutil.which(name)


def version_of(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=15, stdin=subprocess.DEVNULL)
        return (p.stdout or p.stderr).strip().splitlines()[0][:60] if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


_VER: dict = {}


def cached_version(exe: str) -> str | None:
    """`--version` costs a process start: ask once per binary path."""
    if exe not in _VER:
        _VER[exe] = version_of(exe)
    return _VER[exe]


def ro_connect(path: str):
    import sqlite3
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def clip(s, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "..."
