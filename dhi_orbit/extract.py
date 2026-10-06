"""Incremental transcript reader: per turn, your prompt and the chat's FINAL message.

A Claude Code transcript ($CFG/projects/<cwd>/<sessionId>.jsonl) writes one API
response as several lines sharing one message.id (one content block per line, output
tokens growing). So text is joined per message.id, and a turn's final message is the
last assistant message in that turn that has any text. Every text message of the turn is
also kept, in order, in turn["texts"] (final is its last entry); tool calls, tool output
and thinking never show.

Reads only the bytes appended since the last call (offset + carry for a partial line),
so a 70 MB transcript is parsed once and then costs almost nothing. Zero model tokens.
"""
from __future__ import annotations

import json
import os
import time

MODEL_WEIGHT = {"opus": 1.0, "sonnet": 0.72, "haiku": 0.5}
NOISE_PREFIXES = ("<task-notification", "<system-reminder", "<local-command", "<command-",
                  "Caveat:", "<cross-session-message", "[Request interrupted")
MAX_TURNS_KEPT = 400
KEEPALIVE_PREFIX = "[keepalive]"      # keepwarm.PING_TEXT starts with this


def weight(model: str | None) -> float:
    m = (model or "").lower()
    for k, v in MODEL_WEIGHT.items():
        if k in m:
            return v
    return 1.0


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_human(r: dict) -> str | None:
    """The prompt text if this user record is something the human typed, else None."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
        return None
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                         for b in content):
        return None
    origin = (r.get("origin") or {}).get("kind")
    text = _text_of(content).strip()
    if not text:
        return None
    if origin is not None and origin != "human":
        return None
    if origin is None and text.startswith(NOISE_PREFIXES):
        return None
    return text


class Transcript:
    """State for one transcript file, advanced by update()."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.carry = b""
        self.ino = None
        self.turns: list[dict] = []        # {prompt, pts, final, fts, texts}
        self._final_id = None
        self._texts: dict[str, list[str]] = {}
        self.title = {"custom": None, "name": None, "ai": None}
        self.prs: dict[str, dict] = {}
        self.model = None
        self.cwd = None
        self.session_id = None
        self.entrypoint = None
        self.last_ts = None                # last assistant record time (ISO)
        self.last_user_ts = None
        self.ctx_tokens = 0                # context size at the last step
        self.pending_tool = None           # name of a tool_use with no result yet
        self._ka = False                   # inside a keep-alive ping turn: hide it from turns and final
        self.write_ttl = None              # "1h" / "5m": TTL label of the last call that wrote to the cache
        self._tool_open: dict[str, str] = {}
        self._usage: dict[str, tuple] = {}  # message.id -> (day, units, write, read)
        self.version = 0                   # bumps when turns/final change

    # ---------------------------------------------------------------- reading
    def update(self) -> bool:
        """Read new bytes. Returns True if anything visible changed."""
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if self.ino is not None and (st.st_ino != self.ino or st.st_size < self.offset):
            self.__init__(self.path)       # rewritten or rotated: start over
        self.ino = st.st_ino
        if st.st_size == self.offset:
            return False
        before = self.version
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        self.offset += len(data)
        data = self.carry + data
        lines = data.split(b"\n")
        self.carry = lines.pop()           # partial last line (or b"")
        for raw in lines:
            if not raw.strip():
                continue
            try:
                self._feed(json.loads(raw))
            except (ValueError, AttributeError):
                continue
        return self.version != before

    def _feed(self, r: dict) -> None:
        t = r.get("type")
        if t == "ai-title":
            self.title["ai"] = self.title["ai"] or r.get("aiTitle")
        elif t == "custom-title":
            self.title["custom"] = r.get("customTitle"); self.version += 1
        elif t == "agent-name":
            self.title["name"] = r.get("agentName")
        elif t == "pr-link" and r.get("prUrl"):
            self.prs[r["prUrl"]] = {"n": r.get("prNumber"), "url": r["prUrl"],
                                   "repo": r.get("prRepository")}
        if r.get("cwd"):
            self.cwd = r["cwd"]
        if r.get("sessionId") and not self.session_id:
            self.session_id = r["sessionId"]
        if r.get("entrypoint"):
            self.entrypoint = r["entrypoint"]
        if t == "user":
            prompt = _is_human(r)
            if prompt is not None:
                self._ka = prompt.startswith(KEEPALIVE_PREFIX)
            if prompt is not None and self._ka:
                pass                       # a ping is not a turn: its "ok" must not replace the real final
            elif prompt is not None:
                self.turns.append({"prompt": prompt, "pts": r.get("timestamp"),
                                   "final": "", "fts": None, "texts": []})
                if len(self.turns) > MAX_TURNS_KEPT:
                    self.turns = self.turns[-MAX_TURNS_KEPT:]
                self._final_id = None
                self.last_user_ts = r.get("timestamp")
                self.version += 1
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self._tool_open.pop(b.get("tool_use_id"), None)
                self.pending_tool = next(reversed(list(self._tool_open.values())), None) if self._tool_open else None
        elif t == "assistant":
            m = r.get("message") or {}
            mid = m.get("id") or r.get("uuid")
            self.model = m.get("model") or self.model
            self.last_ts = r.get("timestamp") or self.last_ts
            u = m.get("usage")
            if u and mid:
                cc = u.get("cache_creation") or {}
                if cc.get("ephemeral_1h_input_tokens"):
                    self.write_ttl = "1h"
                elif cc.get("ephemeral_5m_input_tokens"):
                    self.write_ttl = "5m"
                day = (r.get("timestamp") or "")[:10]
                w = weight(m.get("model"))
                units = w * (u.get("cache_creation_input_tokens", 0) + u.get("input_tokens", 0)
                             + 5 * u.get("output_tokens", 0))
                self._usage[mid] = (day, units, u.get("cache_creation_input_tokens", 0),
                                    u.get("cache_read_input_tokens", 0))
                self.ctx_tokens = (u.get("cache_creation_input_tokens", 0)
                                   + u.get("cache_read_input_tokens", 0) + u.get("input_tokens", 0))
            content = m.get("content") if isinstance(m.get("content"), list) else []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("id"):
                    inp = b.get("input") or {}
                    what = (inp.get("command") or inp.get("file_path") or inp.get("url")
                            or inp.get("description") or inp.get("pattern") or "")
                    self._tool_open[b["id"]] = f"{b.get('name')}: {str(what)[:400]}" if what else b.get("name")
                    self.pending_tool = self._tool_open[b["id"]]
                elif b.get("type") == "text" and b.get("text", "").strip() and not self._ka:
                    parts = self._texts.setdefault(mid, [])
                    parts.append(b["text"])
                    if not self.turns:          # a chat that began without a human prompt
                        self.turns.append({"prompt": "", "pts": None, "final": "", "fts": None, "texts": []})
                    joined = "\n\n".join(parts)
                    texts = self.turns[-1]["texts"]
                    if self._final_id == mid and texts:
                        texts[-1] = joined
                    else:
                        texts.append(joined)
                    self.turns[-1]["final"] = joined
                    self.turns[-1]["fts"] = r.get("timestamp")
                    if self._final_id != mid:
                        # a newer message took over: drop the older one's buffer
                        if self._final_id:
                            self._texts.pop(self._final_id, None)
                        self._final_id = mid
                    self.version += 1

    # ---------------------------------------------------------------- views
    def label(self) -> str | None:
        return self.title["custom"] or self.title["name"] or self.title["ai"]

    def last_turn(self) -> dict:
        return self.turns[-1] if self.turns else {"prompt": "", "final": "", "pts": None, "fts": None, "texts": []}

    def units_on(self, day: str) -> float:
        return round(sum(u[1] for u in self._usage.values() if u[0] == day))

    def cold_recaches(self, day: str | None = None) -> int:
        """Steps that wrote a large context with little read from cache."""
        return sum(1 for d, _, w, rd in self._usage.values()
                   if (day is None or d == day) and w > 30000 and w > rd)

    def idle_minutes(self) -> float | None:
        ts = self.last_ts or self.last_user_ts
        if not ts:
            return None
        t = iso_epoch(ts)
        return None if t is None else max(0.0, (time.time() - t) / 60.0)


def iso_epoch(ts: str | None) -> float | None:
    """'2026-09-26T01:27:27.203Z' (UTC) -> epoch seconds."""
    if not ts:
        return None
    import calendar
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
