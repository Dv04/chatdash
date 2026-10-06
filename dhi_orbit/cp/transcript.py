"""Full chat history for the v2 chat view: every turn of a transcript as display entries.

Entries, oldest first: user (typed by the user or sent by DHI Orbit), text (assistant prose), thinking, tool (one
per tool call, with its result folded in), notice (limit banners, API errors), summary (compaction). Meta
records and subagent sidechains are left out; subagents live in their own files. Parsed once per file
version (mtime, size) and cached, so paging through a long chat does not re-read it.
"""
from __future__ import annotations

import json
import os
import threading

MAX_TEXT = 20_000          # one entry's text; longer is cut with a marker (the transcript keeps it all)
MAX_TOOL_IN = 4_000
MAX_TOOL_OUT = 6_000
_cache: dict[str, tuple[tuple, list]] = {}
_lock = threading.Lock()


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n:,} more characters in the transcript]"


def _text_of(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) and x.get("type") == "text" else
                         "[image]" if isinstance(x, dict) and x.get("type") == "image" else ""
                         for x in c).strip()
    return ""


def parse(path: str) -> list[dict]:
    out: list[dict] = []
    tools: dict[str, dict] = {}
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for raw in fh:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if r.get("isSidechain"):
                continue
            t, ts = r.get("type"), r.get("timestamp")
            msg = r.get("message") or {}
            c = msg.get("content")
            if t == "user":
                if r.get("isCompactSummary"):
                    out.append({"kind": "summary", "ts": ts, "text": _cut(_text_of(c), MAX_TEXT)})
                    continue
                if r.get("isMeta"):
                    continue
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tl = tools.get(b.get("tool_use_id"))
                            if tl is not None:
                                tl["result"] = _cut(_text_of(b.get("content")), MAX_TOOL_OUT)
                                tl["error"] = bool(b.get("is_error"))
                                tl["result_ts"] = ts
                text = _text_of(c)
                if text and not text.startswith("<local-command") and not text.startswith("<command-"):
                    out.append({"kind": "user", "ts": ts, "text": _cut(text, MAX_TEXT)})
                elif text.startswith("<command-name>"):
                    out.append({"kind": "user", "ts": ts, "text": _cut(text, 600), "command": True})
            elif t == "assistant":
                if r.get("isApiErrorMessage"):
                    out.append({"kind": "notice", "ts": ts, "text": _cut(_text_of(c), 2000)})
                    continue
                for b in c if isinstance(c, list) else []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        out.append({"kind": "text", "ts": ts, "text": _cut(b["text"], MAX_TEXT)})
                    elif bt == "thinking":
                        th = b.get("thinking") or ""
                        out.append({"kind": "thinking", "ts": ts, "text": _cut(th, MAX_TEXT),
                                    "redacted": not th.strip()})
                    elif bt == "redacted_thinking":
                        out.append({"kind": "thinking", "ts": ts, "text": "", "redacted": True})
                    elif bt == "tool_use":
                        inp = b.get("input") or {}
                        e = {"kind": "tool", "ts": ts, "name": b.get("name"), "id": b.get("id"),
                             "summary": _summary(b.get("name"), inp),
                             "input": _cut(json.dumps(inp, indent=1, ensure_ascii=False), MAX_TOOL_IN),
                             "result": None, "error": False}
                        tools[b.get("id")] = e
                        out.append(e)
            elif t == "system" and r.get("subtype") in ("compact_boundary",):
                out.append({"kind": "notice", "ts": ts, "text": "Conversation compacted here"})
    for i, e in enumerate(out):
        e["i"] = i
    return out


def _summary(name: str | None, inp: dict) -> str:
    for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt", "skill"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:200]
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        return (qs[0].get("question", "") if qs else "")[:200]
    return ""


def entries(path: str) -> list[dict]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    sig = (st.st_mtime, st.st_size)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == sig:
            return hit[1]
    got = parse(path)
    with _lock:
        if len(_cache) > 24:
            _cache.pop(next(iter(_cache)))
        _cache[path] = (sig, got)
    return got


def page(path: str, before: int | None = None, limit: int = 200) -> dict:
    """The `limit` entries before index `before` (default: the end), oldest first."""
    es = entries(path)
    end = len(es) if before is None else max(0, min(before, len(es)))
    start = max(0, end - max(1, min(limit, 2000)))
    counts = {}
    for e in es:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"entries": es[start:end], "start": start, "end": end, "total": len(es), "counts": counts}
