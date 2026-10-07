"""Google Antigravity (the `agy` command line agent and the Antigravity desktop and IDE apps).

Listing comes from `<root>/conversation_summaries.db`, a plain SQLite file (schema read from a real agy 1.3.1 install): per conversation
its id, title, preview, last_modified_time, workspace_uris, status, not_fully_idle (a turn is running), killed, and parent_conversation_id
(sub-agent runs). The messages come from the plain-text transcript each conversation keeps: `<root>/brain/<conversation id>/.system_generated/logs/transcript_full.jsonl`
(or `transcript.jsonl`, which can be truncated) under `~/.gemini/antigravity-cli`, `~/.gemini/antigravity` and `~/.gemini/antigravity-ide`.
A line is {"step_index", "source": "USER_EXPLICIT" | "MODEL" | ..., "type": "USER_INPUT" | "PLANNER_RESPONSE" | ..., "created_at",
"content"}; the user's text arrives wrapped in <USER_REQUEST>...</USER_REQUEST>. Titles and folders come from `<root>/history.jsonl` when
it has them. The conversations themselves (conversations/*.db and *.pb) are NOT read: sources disagree on whether they are encrypted.
Reply: `agy -p <text> --conversation <id>` (print mode; the flag spelling comes from a third-party reference and the CHANGELOG of the
agy repository, so a failure is shown with agy's own message). The desktop and IDE apps have no documented way to be sent a prompt:
a chat that only lives there is shown but a reply may be refused by agy.
Checked against a real agy 1.3.1 install for its flags, folders and the summaries schema; the transcript line format comes from third-party
captures (no signed-in conversation was available to read). Anything this does not cover can be described in agents.json instead."""
from __future__ import annotations

import glob
import pathlib
import json
import os
import re
import sqlite3
import time

from . import cached_version, clip, run_detached, which
from .custom import _epoch

LABEL = "Antigravity"
WORKING_S = 8
ROOT_NAMES = ("antigravity-cli", "antigravity", "antigravity-ide")
USER_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def roots() -> list[str]:
    base = os.path.join(os.path.expanduser("~"), ".gemini")
    return [os.path.join(base, n) for n in ROOT_NAMES]


def exe() -> str | None:
    return which("agy", "DHI_ORBIT_AGY_BIN")


def _transcripts() -> list[tuple[str, str, str]]:
    """(conversation id, transcript path, root) for every conversation that has a transcript; the full one wins."""
    out = []
    for r in roots():
        for d in glob.glob(os.path.join(r, "brain", "*")):
            logs = os.path.join(d, ".system_generated", "logs")
            for name in ("transcript_full.jsonl", "transcript.jsonl"):
                p = os.path.join(logs, name)
                if os.path.isfile(p):
                    out.append((os.path.basename(d), p, r))
                    break
    return out


def info() -> dict:
    e = exe()
    have = bool(_transcripts()) or any(_summaries(r) for r in roots())
    return {"available": have, "version": cached_version(e) if e else None, "can_reply": bool(e),
            "detail": None if have else "no Antigravity conversations under ~/.gemini"}


FILE_URI_RE = re.compile(r"file://(/[^\s\",\]\[]*)")


def _summaries(root: str) -> list[dict]:
    """Rows of conversation_summaries.db that are real chats (not sub-agent runs), read-only; [] when absent or unreadable."""
    p = os.path.join(root, "conversation_summaries.db")
    if not os.path.isfile(p):
        return []
    try:
        con = sqlite3.connect(pathlib.Path(p).absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    out = []
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(conversation_summaries)")}
        want = [c for c in ("conversation_id", "title", "preview", "step_count", "last_modified_time", "workspace_uris", "status",
                            "not_fully_idle", "killed", "parent_conversation_id", "nesting_depth", "last_user_input_time") if c in cols]
        if "conversation_id" not in want:
            return []
        for row in con.execute(f"SELECT {','.join(want)} FROM conversation_summaries"):
            d = dict(zip(want, row))
            if d.get("parent_conversation_id") or (d.get("nesting_depth") or 0) > 0:
                continue
            out.append(d)
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return out


def _cwd_of(uris) -> str | None:
    m = FILE_URI_RE.search(str(uris or ""))
    if not m:
        return None
    from urllib.parse import unquote
    path = unquote(m.group(1))
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path        # file:///C:/x on Windows


def _history(root: str) -> dict[str, dict]:
    """conversation id -> {title, cwd} from history.jsonl; field names are tried loosely because the format is not documented."""
    out: dict[str, dict] = {}
    try:
        with open(os.path.join(root, "history.jsonl"), encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                cid = next((d[k] for k in ("conversationId", "conversation_id", "cascadeId", "sessionId", "id") if isinstance(d.get(k), str)), None)
                if cid:
                    out[cid] = {"title": next((d[k] for k in ("title", "summary", "display", "prompt") if isinstance(d.get(k), str)), None),
                                "cwd": next((d[k] for k in ("workspace", "cwd", "project") if isinstance(d.get(k), str)), None)}
    except OSError:
        pass
    return out


def _messages(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        content = d.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if d.get("source") == "USER_EXPLICIT" and d.get("type") == "USER_INPUT":
            m = USER_RE.search(content)
            out.append({"role": "user", "text": (m.group(1) if m else content).strip()[:6000], "at": _epoch(d.get("created_at"))})
        elif d.get("source") == "MODEL" and d.get("type") == "PLANNER_RESPONSE":
            out.append({"role": "assistant", "text": content.strip()[:6000], "at": _epoch(d.get("created_at"))})
    return out


_cache: dict = {}


def _chat(cid: str, path: str, root: str, hist: dict) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        msgs = hit[1]
    else:
        msgs = _messages(path)
        _cache[path] = ((st.st_mtime, st.st_size), msgs)
        if len(_cache) > 400:
            _cache.clear()
    users = [m for m in msgs if m["role"] == "user"]
    h = hist.get(cid) or {}
    final = msgs[-1]["text"] if msgs and msgs[-1]["role"] == "assistant" else ""
    times = [m["at"] for m in msgs if m["at"]]
    upd = max([st.st_mtime] + times[-1:])
    name = h.get("title") or (users[0]["text"] if users else f"Antigravity chat {cid[:6]}")
    return {"key": f"antigravity:{cid}", "provider": "antigravity", "id": cid, "name": clip(str(name).replace("\n", " "), 90),
            "cwd": h.get("cwd"), "state": "working" if time.time() - st.st_mtime < WORKING_S else "idle", "error": None,
            "updated_at": upd, "created_at": min(times) if times else st.st_ctime, "model": None,
            "last_prompt": clip(users[-1]["text"] if users else "", 500), "final": clip(final, 2000),
            "final_at": upd if final else None, "path": path}


def chats(since: float, limit: int = 120) -> list[dict]:
    items: dict[str, dict] = {}
    for r in roots():
        for d in _summaries(r):
            cid = str(d["conversation_id"])
            upd = _epoch(d.get("last_modified_time")) or 0.0
            idle = not d.get("not_fully_idle")
            status = str(d.get("status") or "")
            state = ("stopped" if d.get("killed") else "working" if not idle else
                     "failed" if re.search(r"error|fail", status, re.I) else "idle")
            items[cid] = {"key": f"antigravity:{cid}", "provider": "antigravity", "id": cid,
                          "name": clip(str(d.get("title") or d.get("preview") or f"Antigravity chat {cid[:6]}").replace("\n", " "), 90),
                          "cwd": _cwd_of(d.get("workspace_uris")), "state": state, "error": None, "updated_at": upd,
                          "created_at": upd, "model": None, "last_prompt": clip(str(d.get("preview") or ""), 500), "final": "",
                          "final_at": None, "path": None, "root": r}
    hists: dict[str, dict] = {}
    for cid, p, r in _transcripts():
        hists.setdefault(r, _history(r))
        c = _chat(cid, p, r, hists[r])
        if not c:
            continue
        if cid in items:                                        # the summary wins for title, folder and state; the transcript adds the text
            base = items[cid]
            items[cid] = {**c, "name": base["name"] or c["name"], "cwd": base["cwd"] or c["cwd"], "state": base["state"],
                          "updated_at": max(base["updated_at"], c["updated_at"])}
        else:
            items[cid] = c
    out = [c for c in items.values() if c["updated_at"] >= since]
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out[:limit]


def find(cid: str) -> dict | None:
    for c in chats(0, 5000):
        if c["id"] == cid:
            return c
    return None


def turns(cid: str, limit: int = 80) -> list[dict]:
    c = find(cid)
    if not c or not c.get("path"):
        return []                                   # listed from the summaries database only: there is no transcript to read
    return [{"role": m["role"], "text": m["text"], "at": m["at"] or c["updated_at"]} for m in _messages(c["path"])[-limit:]]


def reply(cid: str, text: str) -> dict:
    e = exe()
    if not e:
        return {"ok": False, "error": "the agy command is not on PATH (set DHI_ORBIT_AGY_BIN)"}
    c = find(cid)
    if not c:
        return {"ok": False, "error": "no such Antigravity chat"}
    if c["state"] == "working":
        return {"ok": False, "route": "refused", "error": "Antigravity is working on this chat right now. Reply when it is idle."}
    r = run_detached([e, "-p", text, "--conversation", cid], c.get("cwd") or "", None, f"agy-{cid[:8]}")
    return {**r, "route": "agy -p --conversation"}


def limits() -> dict | None:
    return None                                   # `agy /usage` fetches quota live; nothing is stored locally


def terminal_command(cid: str) -> str | None:
    return f"agy --conversation {cid}"
