"""Google Gemini CLI.

Read from `<root>/tmp/<project slug>/chats/session-*.jsonl` where root is `$GEMINI_CLI_HOME/.gemini` or `~/.gemini` (what Gemini CLI's
own source does: packages/core/src/services/chatRecordingService.ts and config/storage.ts). Each file is event-sourced JSONL: line 1 is
the session metadata (sessionId, startTime, lastUpdated, kind, directories, summary), then one message per line (id, timestamp, type
"user" | "gemini" | "info" | "error" | "warning", content), plus update lines: {"$set": {...}} (metadata), {"$patch": {...}}
(an edit to a message) and {"$rewindTo": "<message id>"} (the chat was rewound), which are replayed here. Sub-agent chats live in a
folder per parent session and are skipped.
Reply: `gemini --resume <id> -p <text>` run in the project folder (Gemini CLI resumes per project). Gemini CLI keeps no usage or
quota number on disk, so the board shows its limits as unknown.
Written from the source, not from a live install: the first real Gemini chats may show shapes this does not know yet."""
from __future__ import annotations

import glob
import json
import os
import time

from . import cached_version, clip, run_detached, which
from .custom import _epoch, _text

LABEL = "Gemini"
WORKING_S = 8                  # a chat file written this recently means Gemini is probably mid-turn
MAX_BYTES = 8_000_000


def roots() -> list[str]:
    env = os.environ.get("GEMINI_CLI_HOME")
    out = [os.path.join(os.path.expanduser(env) if env else os.path.expanduser("~"), ".gemini")]
    if env and os.path.isdir(os.path.join(os.path.expanduser(env), "tmp")):      # a home given as the .gemini folder itself
        out.append(os.path.expanduser(env))
    return out


def exe() -> str | None:
    return which("gemini", "DHI_ORBIT_GEMINI_BIN")


def _chat_files() -> list[str]:
    seen, out = set(), []
    for r in roots():
        for p in glob.glob(os.path.join(r, "tmp", "*", "chats", "session-*.json*")):      # direct children only: no sub-agent folders
            if p.endswith((".json", ".jsonl")) and p not in seen and os.path.isfile(p):
                seen.add(p)
                out.append(p)
    return out


def info() -> dict:
    e = exe()
    have = bool(_chat_files())
    return {"available": have, "version": cached_version(e) if e else None, "can_reply": bool(e),
            "detail": None if have else f"no Gemini CLI chats in {roots()[0]}"}


def _slugs() -> dict[str, str]:
    """slug -> project path, from <root>/projects.json ({"projects": {"/abs/path": "slug"}})."""
    out = {}
    for r in roots():
        try:
            with open(os.path.join(r, "projects.json"), encoding="utf-8-sig") as fh:
                for path, slug in ((json.load(fh) or {}).get("projects") or {}).items():
                    if isinstance(slug, str):
                        out[slug] = path
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _replay(path: str) -> tuple[dict, list[dict]]:
    """(metadata, messages in order) after applying $set, $patch and $rewindTo lines. A legacy .json file is the same data whole."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > MAX_BYTES and path.endswith(".jsonl"):          # a very long chat: its metadata line, then the newest part
                head = fh.readline()
                fh.seek(size - MAX_BYTES)
                fh.readline()                                         # drop a partial line
                raw = (head + fh.read()).decode("utf-8", "replace")
            else:
                raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return {}, []
    meta: dict = {}
    order: list[str] = []
    msgs: dict[str, dict] = {}

    def add(m: dict):
        mid = str(m.get("id") or f"_{len(order)}")
        if mid not in msgs:
            order.append(mid)
        msgs[mid] = {**msgs.get(mid, {}), **m}

    lines: list = []
    if path.endswith(".json"):
        try:
            doc = json.loads(raw)
        except ValueError:
            return {}, []
        if isinstance(doc, dict):
            meta = {k: v for k, v in doc.items() if k != "messages"}
            lines = [m for m in (doc.get("messages") or []) if isinstance(m, dict)]
    else:
        for ln in raw.splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    lines.append(json.loads(ln))
                except ValueError:
                    continue                                   # a half-written last line
    for d in lines:
        if not isinstance(d, dict):
            continue
        if "$set" in d and isinstance(d["$set"], dict):
            meta.update(d["$set"])
        elif "$rewindTo" in d:
            tgt = d["$rewindTo"]
            if tgt in msgs:
                keep = order[: order.index(tgt) + 1]
                order[:] = keep
                msgs = {k: msgs[k] for k in keep}
        elif "$patch" in d and isinstance(d["$patch"], dict):
            p = d["$patch"]
            for rid in p.get("removeIds") or []:
                if rid in msgs:
                    del msgs[rid]
                    order.remove(rid)
            mid = p.get("id")
            if mid in msgs:
                msgs[mid].update({k: v for k, v in p.items() if k in ("content", "toolCalls", "displayContent")})
            if isinstance(p.get("orderIds"), list):
                ordered = [i for i in p["orderIds"] if i in msgs]
                order[:] = ordered + [i for i in order if i not in ordered]
        elif d.get("sessionId") and "type" not in d:
            meta.update(d)                                     # the metadata line
        elif d.get("type"):
            add(d)
    out = []
    for mid in order:
        m = msgs[mid]
        kind = {"user": "user", "gemini": "assistant"}.get(m.get("type"))
        text = _text(m.get("displayContent") if m.get("type") == "user" and m.get("displayContent") else m.get("content")).strip()
        if kind and text:
            out.append({"role": kind, "text": text[:6000], "at": _epoch(m.get("timestamp")), "model": m.get("model")})
    return meta, out


_cache: dict = {}


def _chat(path: str) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    meta, msgs = _replay(path)
    if meta.get("kind") == "subagent":
        return None
    cid = str(meta.get("sessionId") or os.path.splitext(os.path.basename(path))[0])
    users = [m for m in msgs if m["role"] == "user"]
    assistant = [m for m in msgs if m["role"] == "assistant"]
    dirs = meta.get("directories")
    slug = os.path.basename(os.path.dirname(os.path.dirname(path)))
    cwd = (dirs[0] if isinstance(dirs, list) and dirs and isinstance(dirs[0], str) else None) or _slugs().get(slug)
    final = msgs[-1]["text"] if msgs and msgs[-1]["role"] == "assistant" else ""
    upd = max(st.st_mtime, _epoch(meta.get("lastUpdated")) or 0.0)
    name = (meta.get("summary") or (users[0]["text"] if users else None) or f"Gemini chat {cid[:6]}")
    chat = {"key": f"gemini:{cid}", "provider": "gemini", "id": cid, "name": clip(str(name).replace("\n", " "), 90), "cwd": cwd,
            "state": "working" if time.time() - st.st_mtime < WORKING_S else "idle", "error": None, "updated_at": upd,
            "created_at": _epoch(meta.get("startTime")) or st.st_ctime, "model": next((m["model"] for m in reversed(assistant) if m.get("model")), None),
            "last_prompt": clip(users[-1]["text"] if users else "", 500), "final": clip(final, 2000),
            "final_at": upd if final else None, "path": path}
    _cache[path] = ((st.st_mtime, st.st_size), chat)
    if len(_cache) > 600:
        _cache.clear()
    return chat


def chats(since: float, limit: int = 120) -> list[dict]:
    files = []
    for p in _chat_files():
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= since:
            files.append((mt, p))
    files.sort(reverse=True)
    out = [c for c in (_chat(p) for _, p in files[: limit * 2]) if c and c["updated_at"] >= since]
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out[:limit]


def find(cid: str) -> dict | None:
    for c in chats(0, 5000):
        if c["id"] == cid:
            return c
    return None


def turns(cid: str, limit: int = 80) -> list[dict]:
    c = find(cid)
    if not c:
        return []
    _, msgs = _replay(c["path"])
    return [{"role": m["role"], "text": m["text"], "at": m["at"] or c["updated_at"]} for m in msgs[-limit:]]


def reply(cid: str, text: str) -> dict:
    e = exe()
    if not e:
        return {"ok": False, "error": "the gemini command is not on PATH (set DHI_ORBIT_GEMINI_BIN)"}
    c = find(cid)
    if not c:
        return {"ok": False, "error": "no such Gemini chat (its retention period may have removed it)"}
    if c["state"] == "working":
        return {"ok": False, "route": "refused", "error": "Gemini is working on this chat right now. Reply when it is idle."}
    if not c.get("cwd") or not os.path.isdir(c["cwd"]):
        return {"ok": False, "error": "this chat's project folder is unknown or gone: Gemini CLI resumes a chat only from its own project folder"}
    r = run_detached([e, "--resume", cid, "-p", text], c["cwd"], None, f"gemini-{cid[:8]}")
    return {**r, "route": "gemini --resume -p"}


def limits() -> dict | None:
    return None                                   # no quota or rate-limit figure is stored locally


def terminal_command(cid: str) -> str | None:
    return f"gemini --resume {cid}"
