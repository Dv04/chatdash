"""Cursor CLI (`cursor-agent`).

Read from ~/.cursor/chats/<workspace hash>/<chat id>/: meta.json (title, cwd, createdAtMs, updatedAtMs, hasConversation) and
store.db (content-addressed blobs; meta.latestRootBlobId is a protobuf list of the message blob ids in order).
Reply: `cursor-agent -p --trust --resume <id> <text>` in the chat's folder, detached.
Whether a turn is running is not recorded anywhere: a chat written to in the last 20 s is shown as working, which is a
heuristic (there is no process-to-chat link), and a reply is not refused because of it."""
from __future__ import annotations

import glob
import json
import os
import re
import time

from . import cached_version, clip, ro_connect, run_detached, which

LABEL = "Cursor"
WORKING_S = 20
_PARSED: dict = {}          # store.db path -> (mtime, messages): parsing walks blobs, so do it once per change


def home() -> str:
    return os.path.expanduser(os.environ.get("DHI_ORBIT_CURSOR_HOME") or "~/.cursor/chats")


def exe() -> str | None:
    return which("cursor-agent", "DHI_ORBIT_CURSOR_BIN") or which("agent", "DHI_ORBIT_CURSOR_BIN")


def info() -> dict:
    e = exe()
    ok = os.path.isdir(home())
    return {"available": ok, "version": cached_version(e) if e else None, "can_reply": bool(e),
            "detail": None if ok else f"no Cursor chats in {home()}"}


def _meta_files() -> list[str]:
    return glob.glob(os.path.join(home(), "*", "*", "meta.json"))


def _read_json(p: str) -> dict:
    try:
        with open(p, encoding="utf-8") as fh:
            j = json.load(fh)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def _root_ids(blob: bytes) -> list[str]:
    """The message blob ids, in order, from the protobuf root blob: repeated field 1, 32 bytes each."""
    ids, i, n = [], 0, len(blob)
    while i < n:
        tag = blob[i]
        i += 1
        wt, fn = tag & 7, tag >> 3
        if wt == 2:
            ln, sh = 0, 0
            while i < n:
                x = blob[i]
                i += 1
                ln |= (x & 0x7F) << sh
                sh += 7
                if not x & 0x80:
                    break
            if fn == 1 and ln == 32 and i + ln <= n:
                ids.append(blob[i:i + ln].hex())
            i += ln
        elif wt == 0:
            while i < n and blob[i] & 0x80:
                i += 1
            i += 1
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            break
    return ids


def _text_parts(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))
    return ""


_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def messages(store: str) -> list[dict]:
    """[{role: user|assistant, text}] in order. A user turn is the text inside <user_query>; the context the CLI adds
    around it (<user_info>, rules) is dropped. Tool calls, results and reasoning are not messages."""
    try:
        mt = os.path.getmtime(store)
    except OSError:
        return []
    hit = _PARSED.get(store)
    if hit and hit[0] == mt:
        return hit[1]
    out: list[dict] = []
    try:
        db = ro_connect(store)
    except Exception:
        return []
    try:
        row = db.execute("SELECT value FROM meta LIMIT 1").fetchone()
        meta = json.loads(bytes.fromhex(row[0])) if row else {}
        root = db.execute("SELECT data FROM blobs WHERE id=?", (meta.get("latestRootBlobId", ""),)).fetchone()
        for bid in _root_ids(root[0]) if root else []:
            r = db.execute("SELECT data FROM blobs WHERE id=?", (bid,)).fetchone()
            if not r or r[0][:1] != b"{":
                continue
            try:
                j = json.loads(r[0])
            except ValueError:
                continue
            role, txt = j.get("role"), _text_parts(j.get("content"))
            if role == "user":
                m = _QUERY.search(txt)
                if m:
                    out.append({"role": "user", "text": m.group(1)})
            elif role == "assistant" and txt.strip():
                out.append({"role": "assistant", "text": txt.strip()})
    except Exception:
        pass
    finally:
        db.close()
    _PARSED[store] = (mt, out)
    if len(_PARSED) > 200:
        _PARSED.pop(next(iter(_PARSED)))
    return out


def _chat(meta_path: str, with_text: bool) -> dict | None:
    m = _read_json(meta_path)
    if not m.get("hasConversation", True):
        return None
    d = os.path.dirname(meta_path)
    cid = os.path.basename(d)
    upd = (m.get("updatedAtMs") or os.path.getmtime(meta_path) * 1000) / 1000
    final = prompt = ""
    if with_text:
        msgs = messages(os.path.join(d, "store.db"))
        final = next((x["text"] for x in reversed(msgs) if x["role"] == "assistant"), "")
        prompt = next((x["text"] for x in reversed(msgs) if x["role"] == "user"), "")
    return {"key": f"cursor:{cid}", "provider": "cursor", "id": cid, "name": clip((m.get("title") or cid[:8]).replace("\n", " "), 90),
            "cwd": m.get("cwd"), "state": "working" if time.time() - upd < WORKING_S else "idle", "error": None,
            "updated_at": upd, "created_at": (m.get("createdAtMs") or 0) / 1000, "model": None, "source": "cli",
            "parent": None, "archived": False, "last_prompt": clip(prompt, 500), "final": clip(final, 2000),
            "final_at": upd if final else None, "agent": None, "history": "blobs"}


def chats(since: float, limit: int = 120) -> list[dict]:
    files = []
    for p in _meta_files():
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= since:
            files.append((mt, p))
    files.sort(reverse=True)
    out = []
    for _, p in files[:limit * 2]:
        c = _chat(p, True)
        if c and c["updated_at"] >= since:
            out.append(c)
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return out[:limit]


def find(cid: str) -> dict | None:
    if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", cid or ""):
        return None
    for p in glob.glob(os.path.join(home(), "*", cid, "meta.json")):
        return _chat(p, True)
    return None


def turns(cid: str, limit: int = 80) -> list[dict]:
    if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", cid or ""):
        return []
    for p in glob.glob(os.path.join(home(), "*", cid, "store.db")):
        return [{"role": m["role"], "text": m["text"][:6000], "at": None, "phase": None} for m in messages(p)[-limit:]]
    return []


def reply(cid: str, text: str) -> dict:
    e = exe()
    if not e:
        return {"ok": False, "error": "cursor-agent is not on PATH (set DHI_ORBIT_CURSOR_BIN)"}
    c = find(cid)
    if not c:
        return {"ok": False, "error": "no such Cursor chat"}
    r = run_detached([e, "-p", "--trust", "--resume", cid, text], c.get("cwd") or "", None, f"cursor-{cid[:8]}", settle_s=15.0)
    return {**r, "route": "cursor-agent --resume"}


def terminal_command(cid: str) -> str | None:
    return f"cursor-agent --resume {cid}"
