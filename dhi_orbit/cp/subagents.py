"""Which subagents of a chat are really running (graph, 2026-10-06).

The graph used to call every subagent transcript touched in the last 30 minutes a live node, so finished and failed
workers lingered as grey squares for half an hour. Ground truth, measured on 6,230 notifications in two days of
transcripts (5,576 completed, 523 failed, 127 killed, 4 stopped; every one terminal): when a background subagent
ends, its parent transcript gets a `<task-notification>` naming `<tool-use-id>` (the id in the subagent's
.meta.json) and a terminal `<status>`. Second signal: the subagent's own last record is an assistant message that
ended its turn (stop_reason end_turn / stop_sequence / max_tokens) with no tool call pending.

Not knowable from disk: a subagent that went silent without either signal. It is shown as "unknown" (amber,
dashed) once it has been quiet for QUIET_S, and dropped after DROP_S, never as working.
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading

NOTE = re.compile(rb"<tool-use-id>(toolu_\w+)</tool-use-id>.{0,400}?<status>(\w+)</status>", re.S)
TERMINAL = {"completed", "failed", "killed", "stopped"}
ENDED_STOP = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
WORKING_S = 120        # transcript written this recently: working
INFLIGHT_S = 1200      # a tool call or model turn pending: still working for this long without a write
DROP_S = 1800          # older than this with no end signal: not shown
TAIL = 262144
CHUNK = 8 << 20
OVERLAP = 1024

_lock = threading.Lock()
_seen: dict[str, dict] = {}     # parent transcript -> {"off": int, "ended": {tool_use_id: status}}


def ended_in_parent(parent_path: str) -> dict[str, str]:
    """tool_use_id -> terminal status for every subagent notification in the parent transcript (incremental)."""
    with _lock:
        st = _seen.setdefault(parent_path, {"off": 0, "ended": {}})
        try:
            size = os.path.getsize(parent_path)
        except OSError:
            return dict(st["ended"])
        if size < st["off"]:                       # truncated or replaced: start over
            st["off"], st["ended"] = 0, {}
        if size > st["off"]:
            try:
                with open(parent_path, "rb") as fh:
                    pos = max(0, st["off"] - OVERLAP)      # a notification can straddle the previous read's end
                    while pos < size:
                        fh.seek(pos)
                        data = fh.read(CHUNK)
                        for m in NOTE.finditer(data):
                            if m.group(2).decode() in TERMINAL:
                                st["ended"][m.group(1).decode()] = m.group(2).decode()
                        pos += max(1, len(data) - OVERLAP) if pos + len(data) < size else len(data)
            except OSError:
                return dict(st["ended"])
            st["off"] = size
        return dict(st["ended"])


def last_record(path: str) -> dict | None:
    """The last assistant or user record of a subagent transcript (tail read)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL))
            data = fh.read()
    except OSError:
        return None
    last = None
    for raw in data.split(b"\n"):
        if b'"type":"assistant"' not in raw and b'"type":"user"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("type") in ("assistant", "user"):
            last = r
    return last


def _summary(rec: dict | None) -> tuple[bool, str | None]:
    """(finished its turn, name of the tool call in flight)."""
    if not rec:
        return False, None
    msg = rec.get("message") or {}
    blocks = msg.get("content")
    blocks = blocks if isinstance(blocks, list) else []
    tools = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    if rec.get("type") == "assistant":
        if not tools and msg.get("stop_reason") in ENDED_STOP:
            return True, None
        return False, (tools[-1].get("name") if tools else None)
    return False, None                              # a tool_result came back: the model is thinking


def live(chats_by_path: dict[str, dict], now: float) -> list[dict]:
    """Subagents of the given chats that are still running, newest first.

    chats_by_path: transcript path -> chat dict (needs "live", "key", "account"). A chat whose process is gone has
    no running subagents, so its workers are dropped whatever their files say."""
    out = []
    if len(_seen) > 200:                              # parents of long-gone chats: the scan cache must not grow for ever
        with _lock:
            for k in [k for k in _seen if k not in chats_by_path]:
                del _seen[k]
    for parent, c in chats_by_path.items():
        if not c.get("live"):
            continue
        base = parent[:-6]
        paths = glob.glob(base + "/subagents/*.jsonl")
        recent = []
        for p in paths:
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if now - mt <= DROP_S:
                recent.append((p, mt))
        if not recent:
            continue
        ended = ended_in_parent(parent)
        for p, mt in recent:
            try:
                meta = json.load(open(p[:-6] + ".meta.json", encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if ended.get(meta.get("toolUseId")):
                continue
            done, tool = _summary(last_record(p))
            if done:
                continue
            age = now - mt
            if age <= WORKING_S or (age <= INFLIGHT_S and tool):
                state = "working"
            else:
                state = "unknown"
            out.append({"id": os.path.basename(p)[:-6], "agent_type": meta.get("agentType") or "subagent",
                        "description": meta.get("description") or "", "state": state, "activity": mt, "tool": tool,
                        "chat": c})
    out.sort(key=lambda s: -s["activity"])
    return out
