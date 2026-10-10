"""Markdown documents of one chat, for the chat view's Docs reader: the files a chat writes, or points you to, read
right beside it instead of "the full write-up is in docs/X.md".

list_docs: the chat's .md files, its subagents included, newest first, in two groups:
  made: written or edited with a file tool (Write, Edit, MultiEdit) or created by a shell command (> file.md, >> , tee,
        cp/mv to it, -o file.md);
  mentioned: named in Claude's replies or your prompts and present on disk.
A file a script writes some other way is not seen.
read_doc: one .md file's text. Allowed only when the file is one this chat made or named, its path appears in the chat's
transcript (a path the chat view makes clickable), or a document of the chat links to it (one hop); always a .md under
the home directory, never under ~/.secrets or ~/.ssh, at most MAX_BYTES. Relative paths resolve against the chat's
working directory. Each transcript is read incrementally (new bytes only), like fileindex. POSIX and Windows paths.
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading

HOME = os.path.expanduser("~")
WRITE = {"Write", "Edit", "MultiEdit"}
WT = re.compile(r"[\\/]\.claude[\\/]worktrees[\\/][^\\/]+[\\/]")
MAX_BYTES = 2_000_000
DENY = (os.path.join(HOME, ".secrets"), os.path.join(HOME, ".ssh"))
# Shell targets that create or overwrite a .md file (a Windows path keeps its drive and backslashes).
SHELL_MD = re.compile(r"""(?:>>?|\btee(?:\s+-a)?|\b(?:cp|mv)(?:\s+-\w+)*\s+\S+|\s-o)\s*['"]?([~\w./\\:@+-]+\.md)\b(?![\w/\\-])""")
# A .md path in prose: absolute (POSIX or C:\...), ~/, or relative with a folder or in backticks (a bare word like
# README.md alone is too vague).
TEXT_MD = re.compile(r"(?<![\w./\\~@+:-])((?:~[/\\]|/|[A-Za-z]:[/\\])[\w./\\@+-]+\.md|[\w.@+-]+(?:[/\\][\w.@+-]+)+\.md|(?<=`)[\w.@+-]+\.md(?=`))(?![\w/\\-])")
# Every whole .md path token in the raw transcript lines ("notes.md" does not vouch for "s.md"). Kept instead of the raw
# bytes: transcripts reach 100+ MB. A path at the start of a line of text follows an escaped \n or \t.
MENTION = re.compile(rb"(?:(?<=\\[nt])|(?<![\w./~@+\\-]))([\w./~@+-]+\.md)(?![\w/-])")
_cache: dict[str, dict] = {}          # per transcript file: bytes read so far and what they held
_lock = threading.Lock()
_plock = threading.Lock()              # one parse at a time: two refreshes reading the same new bytes would count writes twice


def _files(path: str) -> list[tuple[str, bool]]:
    """The main transcript and its subagent transcripts: (path, is_subagent)."""
    out = [(path, False)]
    base = path[:-6] if path.endswith(".jsonl") else path
    out += [(f, True) for f in sorted(glob.glob(os.path.join(base, "subagents", "*.jsonl")))]
    return out


def _read_new(f: str, sub: bool) -> dict:
    """This file's state, after parsing only the bytes added since the last call. A live chat's transcript grows every few
    seconds, so a whole-file cache would miss on every refresh and re-read the whole file."""
    try:
        size = os.path.getsize(f)
    except OSError:
        return _empty()
    with _lock:
        st = _cache.get(f)
    if st is None or size < st["off"]:                         # new, or rewritten shorter: start over
        st = _empty()
    if size > st["off"]:
        with open(f, "rb") as fh:
            fh.seek(st["off"])
            data = fh.read(size - st["off"])
        end = data.rfind(b"\n") + 1                          # never parse a half-written last line
        for raw in data[:end].split(b"\n"):
            if not sub and st["cwd"] is None and b'"cwd"' in raw[:4000]:
                try:
                    st["cwd"] = json.loads(raw).get("cwd") or None
                except ValueError:
                    pass
            if b".md" not in raw:
                continue
            st["said"].update(m.decode("utf-8", "replace") for m in MENTION.findall(raw))
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("isMeta") or r.get("type") not in ("assistant", "user"):
                continue
            at, rcwd = r.get("timestamp"), r.get("cwd") or st["cwd"]
            c = (r.get("message") or {}).get("content")
            blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") in WRITE:
                        _made(st, _abs(inp.get("file_path"), rcwd), "writes" if b["name"] == "Write" else "edits", at, sub)
                    elif b.get("name") == "Bash" and isinstance(inp.get("command"), str):
                        for t in SHELL_MD.findall(inp["command"]):
                            _made(st, _abs(t, rcwd), "shell", at, sub)
                elif b.get("type") == "text" and isinstance(b.get("text"), str) and ".md" in b["text"]:
                    if r["type"] == "user" and b["text"].lstrip().startswith("<"):
                        continue                                   # task notifications and command echoes, not the user's words
                    for t in TEXT_MD.findall(b["text"]):
                        p = _abs(t, rcwd)
                        if p:
                            st["ment"][p] = max(st["ment"].get(p) or "", at or "") or None
        st["off"] += end
    with _lock:
        if f not in _cache and len(_cache) > 2000:
            _cache.pop(next(iter(_cache)))
        _cache[f] = st
    return st


def _empty() -> dict:
    return {"off": 0, "docs": {}, "ment": {}, "said": set(), "cwd": None}


def _abs(p, cwd: str | None) -> str | None:
    if not isinstance(p, str) or not p.lower().endswith(".md"):
        return None
    p = p.strip("'\"")
    if p.startswith(("~/", "~\\")):
        p = os.path.join(HOME, p[2:])
    elif not _isabs(p):
        if not cwd:
            return None
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def _isabs(p: str) -> bool:
    return p.startswith("/") or os.path.isabs(p) or bool(re.match(r"[A-Za-z]:[\\/]", p))


def _under(p: str, base: str) -> bool:
    p, base = os.path.normcase(p), os.path.normcase(base)
    return p.startswith(base.rstrip("\\/") + os.sep)


def _made(st: dict, p: str | None, how: str, at, sub: bool) -> None:
    if not p:
        return
    d = st["docs"].setdefault(p, {"path": p, "writes": 0, "edits": 0, "shell": 0, "first_at": at, "last_at": at, "by_subagent": sub})
    d[how] += 1
    d["last_at"] = max(d["last_at"] or "", at or "") or None


def _get(path: str) -> dict:
    """The chat's docs, mentions and cwd across its main and subagent transcripts."""
    docs: dict[str, dict] = {}
    ment: dict[str, str | None] = {}
    said: set[str] = set()
    cwd = None
    for f, sub in _files(path):
        with _plock:
            st = _read_new(f, sub)
        said |= st["said"]
        for p, at in st["ment"].items():
            ment[p] = max(ment.get(p) or "", at or "") or None
        cwd = cwd or (None if sub else st["cwd"])
        for p, d in st["docs"].items():
            m = docs.get(p)
            if m is None:
                docs[p] = dict(d)
                continue
            m["writes"] += d["writes"]
            m["edits"] += d["edits"]
            m["shell"] += d["shell"]
            m["first_at"] = min(m["first_at"] or "~", d["first_at"] or "~") if (m["first_at"] or d["first_at"]) else None
            m["last_at"] = max(m["last_at"] or "", d["last_at"] or "") or None
            m["by_subagent"] = m["by_subagent"] and d["by_subagent"]
    return {"docs": docs, "ment": ment, "cwd": cwd, "said": said}


def _where(p: str) -> str | None:
    """The file on disk now: the path itself, or for a removed worktree copy the same file in the main checkout."""
    if os.path.isfile(p):
        return p
    q = os.path.normpath(WT.sub("/", p))
    return q if q != p and os.path.isfile(q) else None


def list_docs(path: str) -> dict:
    g = _get(path)
    out = []
    rows = [dict(d, kind="made") for d in g["docs"].values()]
    rows += [{"path": p, "kind": "mentioned", "writes": 0, "edits": 0, "shell": 0, "first_at": at, "last_at": at, "by_subagent": False}
             for p, at in g["ment"].items() if p not in g["docs"]]
    for d in rows:
        if _allowed(d["path"]):                                  # ~/.secrets, outside home: never listed, never served
            continue
        real = _where(d["path"])
        if d["kind"] == "mentioned" and not real:                # a name in prose that is not a file: not a document
            continue
        st = os.stat(real) if real else None
        out.append(dict(d, exists=bool(real), at_path=real, size=st.st_size if st else None,
                        mtime=st.st_mtime if st else None, name=os.path.basename(d["path"]),
                        folder=_short(os.path.dirname(d["path"]))))
    out.sort(key=lambda d: (d["kind"] == "made", d["last_at"] or ""), reverse=True)
    return {"docs": out, "cwd": g["cwd"]}


def _short(p: str) -> str:
    return "~" + p[len(HOME):] if p == HOME or _under(p, HOME) else p


def _allowed(real: str) -> str | None:
    if not real.lower().endswith(".md"):
        return "only .md files open here"
    if not _under(real, HOME):
        return "only files under the home directory open here"
    if any(_under(real, d) for d in DENY):
        return "that folder is never served"
    return None


def _linked(g: dict, p: str, frm: str | None) -> bool:
    """A link inside a document this chat made or mentioned (docs/A.md links ./B.md): one hop, never a chain."""
    if not frm:
        return False
    frm = os.path.normpath(frm)
    if frm not in g["docs"] and frm not in g["ment"]:
        return False
    real = _where(frm)
    if not real or _allowed(os.path.realpath(real)):
        return False
    try:
        text = open(real, "rb").read(MAX_BYTES).decode("utf-8", "replace")
    except OSError:
        return False
    base = os.path.dirname(frm)
    for m in re.finditer(r"\]\(\s*<?([^)\s>#]+\.md)(?:#[^)\s]*)?>?(?:\s+\"[^\"]*\")?\s*\)", text):
        t = m.group(1)
        q = os.path.normpath(os.path.join(HOME, t[2:]) if t.startswith("~/") else t if _isabs(t) else os.path.join(base, t))
        if q == p:
            return True
    return False


def read_doc(path: str, want: str, frm: str | None = None) -> tuple[int, dict]:
    """(status, body) for the .md file `want` (absolute, ~/, relative to the chat's cwd, or with `frm` relative to that
    document) as seen by this chat."""
    want = (want or "").strip()
    want = re.sub(r":\d+(?::\d+)?$", "", want)                 # file.md:12 from a "path:line" reference
    if not want:
        return 400, {"error": "path required"}
    g = _get(path)
    if want.startswith(("~/", "~\\")):
        p = os.path.join(HOME, want[2:])
    elif _isabs(want):
        p = want
    elif frm:
        p = os.path.join(os.path.dirname(os.path.normpath(frm)), want)
    elif g["cwd"]:
        p = os.path.join(g["cwd"], want)
    else:
        return 404, {"error": "relative path and this chat has no working directory on record"}
    p = os.path.normpath(p)
    if not frm and not want.startswith(("~/", "~\\")) and not _isabs(want) and not _where(p):
        # `notes.md` or `docs/notes.md` named in a message: the newest file this chat wrote that ends with it
        tail = os.sep + os.path.normpath(want.lstrip("./\\"))
        hits = sorted([(d["last_at"] or "", k) for k, d in g["docs"].items() if k.endswith(tail)] +
                      [(at or "", k) for k, at in g["ment"].items() if k.endswith(tail) and _where(k)])
        if hits:
            p = hits[-1][1]
    real = _where(p)
    if not real:
        return 404, {"error": "file not found (moved, deleted, or its worktree was removed)", "path": p}
    real = os.path.realpath(real)
    why = _allowed(real)
    if why:
        return 403, {"error": why}
    if p not in g["docs"] and p not in g["ment"] and want not in g["said"] and p not in g["said"] and not _linked(g, p, frm):
        return 403, {"error": "this chat neither wrote nor mentioned that file"}
    st = os.stat(real)
    if st.st_size > MAX_BYTES:
        return 413, {"error": f"file is {st.st_size:,} bytes; the reader opens up to {MAX_BYTES:,}"}
    with open(real, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    d = g["docs"].get(p)
    return 200, {"path": p, "at_path": real, "name": os.path.basename(p), "folder": _short(os.path.dirname(p)),
                 "size": st.st_size, "mtime": st.st_mtime, "text": text, "written_here": bool(d),
                 "writes": d["writes"] if d else 0, "edits": d["edits"] if d else 0, "shell": d["shell"] if d else 0}
