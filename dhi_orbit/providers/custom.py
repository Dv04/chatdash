"""Any other local terminal agent, described in `<data dir>/agents.json` instead of code.

A file is enough to put an agent's chats on the board, next to Claude Code, Codex and Cursor:

    {"agents": [
      {"id": "myagent", "label": "My agent",
       "files": {"glob": "~/.myagent/sessions/*.jsonl", "format": "jsonl", "role": "role", "text": "content", "time": "ts"},
       "reply": ["myagent", "--resume", "{id}", "-p", "{text}"]},
      {"id": "bridge", "label": "Bridge",
       "list": ["my-bridge", "list"], "turns": ["my-bridge", "turns", "{id}"], "reply": ["my-bridge", "send", "{id}", "{text}"]}
    ]}

Two ways to describe an agent:
  files    the agent keeps one file per chat: a glob, the format (jsonl: one message per line; json: a list, or an object that
           holds one under `messages`), and the field names of role, text and optionally time and cwd (dotted: "message.content").
  scripts  `list` prints a JSON list of {id, title, cwd, updated_at, state, last_prompt, final, model}; `turns` prints a JSON list
           of {role, text, at} for one chat. Any language, any agent: the script does the reading.
`reply` is a command: argv items are never run through a shell, "{id}" "{text}" "{cwd}" are replaced inside an item, the text is
one argument. Without `reply` the agent is read-only. `terminal` (optional) is the command that resumes a chat in a terminal.
A problem in the file never stops the board: it is reported (see problems()) and the rest of the file still loads."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

from . import _plat, clip, config, run_detached, which

RESERVED = {"claude", "codex", "cursor", "gemini", "antigravity", "provider", "main"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,23}$")
MAX_BYTES = 8_000_000            # a chat file larger than this is read from its tail
WORKING_S = 8                    # a file written this recently means the agent is probably mid-turn
CMD_TIMEOUT_S = 20
USER_ROLES = ("user", "human", "you", "prompt")
ASSISTANT_ROLES = ("assistant", "model", "ai", "agent", "bot", "gemini")

_cache: dict = {"sig": None, "agents": [], "problems": []}
_parsed: dict = {}              # path -> (mtime, size, parsed chat)


def config_path() -> str:
    return os.path.join(config.home(), "agents.json")


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _dig(rec, path: str | None):
    """rec["a"]["b"] for "a.b"; None when any step is missing."""
    cur = rec
    for part in (path or "").split("."):
        if part == "":
            return None
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return _text(v.get("text") if "text" in v else v.get("content"))
    if isinstance(v, list):
        return "\n".join(t for t in (_text(x) for x in v) if t)
    return ""


def _epoch(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return float(v) / (1000.0 if float(v) > 1e11 else 1.0)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _sub(argv: list[str], **kw) -> list[str]:
    out = []
    for a in argv:
        for k, v in kw.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(_expand(a) if a.startswith("~") else a)
    return out


class Agent:
    """One described agent; has the same surface as the codex and cursor modules (LABEL, info, chats, find, turns, reply)."""

    def __init__(self, spec: dict):
        self.id = spec["id"]
        self.LABEL = spec.get("label") or spec["id"]
        self.spec = spec
        self.files = spec.get("files")

    # -------------------------------------------------------------- info
    def _exe(self, key: str) -> str | None:
        argv = self.spec.get(key)
        return which(_expand(argv[0]), "DHI_ORBIT_NO_SUCH_VAR") if argv else None

    def info(self) -> dict:
        if self.files:
            pats = self._globs()
            ok = any(_glob(p) for p in pats)
            detail = None if ok else f"no files match {pats[0] if pats else '(no glob)'}"
        else:
            ok = bool(self._exe("list"))
            detail = None if ok else f"command not found: {(self.spec.get('list') or ['?'])[0]}"
        return {"available": ok, "version": None, "can_reply": bool(self.spec.get("reply") and self._exe("reply")),
                "detail": detail}

    # -------------------------------------------------------------- files mode
    def _globs(self) -> list[str]:
        g = (self.files or {}).get("glob")
        return [_expand(x) for x in (g if isinstance(g, list) else [g])] if g else []

    def _files(self) -> list[str]:
        seen, out = set(), []
        for pat in self._globs():
            for p in _glob(pat):
                if p not in seen and os.path.isfile(p):
                    seen.add(p)
                    out.append(p)
        return out

    def _read_records(self, path: str) -> list:
        fmt = (self.files or {}).get("format", "jsonl")
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > MAX_BYTES:
                    fh.seek(size - MAX_BYTES)
                    fh.readline()                          # drop the partial first line
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return []
        if fmt == "json":
            try:
                doc = json.loads(raw)
            except ValueError:
                return []
            key = (self.files or {}).get("messages")
            doc = _dig(doc, key) if key else doc
            if isinstance(doc, dict):
                doc = doc.get("messages")
            return doc if isinstance(doc, list) else []
        recs = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
        return recs

    def _messages(self, recs: list) -> list[dict]:
        f = self.files or {}
        users = {x.lower() for x in f.get("user_roles", USER_ROLES)}
        assts = {x.lower() for x in f.get("assistant_roles", ASSISTANT_ROLES)}
        out = []
        for r in recs:
            role = str(_dig(r, f.get("role", "role")) or "").lower()
            kind = "user" if role in users else "assistant" if role in assts else None
            text = _text(_dig(r, f.get("text", "content"))).strip()
            if not kind or not text:
                continue
            out.append({"role": kind, "text": text[:6000], "at": _epoch(_dig(r, f.get("time"))) if f.get("time") else None,
                        "cwd": _dig(r, f.get("cwd")) if f.get("cwd") else None})
        return out

    def _parse(self, path: str) -> dict | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        hit = _parsed.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        msgs = self._messages(self._read_records(path))
        f = self.files or {}
        cid = os.path.splitext(os.path.basename(path))[0]
        users = [m for m in msgs if m["role"] == "user"]
        last_user = users[-1] if users else None
        final = ""
        if msgs and msgs[-1]["role"] == "assistant":
            final = msgs[-1]["text"]
        cwd = next((m["cwd"] for m in msgs if m.get("cwd")), None) or f.get("cwd_default")
        name = (users[0]["text"] if users else cid).replace("\n", " ")
        times = [m["at"] for m in msgs if m.get("at")]
        chat = {"key": f"{self.id}:{cid}", "provider": self.id, "id": cid, "name": clip(name, 90), "cwd": cwd,
                "state": "working" if time.time() - st.st_mtime < WORKING_S else "idle", "error": None,
                "updated_at": max([st.st_mtime] + times[-1:]), "created_at": (min(times) if times else st.st_mtime),
                "model": None, "last_prompt": clip(last_user["text"] if last_user else "", 500), "final": clip(final, 2000),
                "final_at": st.st_mtime if final else None, "path": path, "messages": len(msgs)}
        _parsed[path] = (st.st_mtime, st.st_size, chat)
        if len(_parsed) > 600:
            _parsed.clear()
        return chat

    # -------------------------------------------------------------- scripts mode
    def _run(self, key: str, **kw):
        argv = self.spec.get(key)
        if not argv:
            return None, f"no `{key}` command in agents.json"
        argv = _sub(argv, **kw)
        exe = which(argv[0], "DHI_ORBIT_NO_SUCH_VAR")
        if not exe:
            return None, f"command not found: {argv[0]}"
        try:
            p = subprocess.run([exe] + argv[1:], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=CMD_TIMEOUT_S, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000 if _plat.IS_WIN else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"{os.path.basename(argv[0])}: {type(e).__name__}"
        if p.returncode != 0:
            return None, f"{os.path.basename(argv[0])} exited {p.returncode}: {clip((p.stderr or p.stdout), 200)}"
        try:
            return json.loads(p.stdout), None
        except ValueError:
            return None, f"{os.path.basename(argv[0])} did not print JSON"

    def _chat_from_script(self, r: dict) -> dict | None:
        if not isinstance(r, dict) or not r.get("id"):
            return None
        cid = str(r["id"])
        upd = _epoch(r.get("updated_at")) or 0.0
        state = r.get("state") if r.get("state") in ("idle", "working", "stopped", "failed", "needs_you") else "idle"
        return {"key": f"{self.id}:{cid}", "provider": self.id, "id": cid, "name": clip(str(r.get("title") or cid).replace("\n", " "), 90),
                "cwd": r.get("cwd"), "state": state, "error": r.get("error"), "updated_at": upd,
                "created_at": _epoch(r.get("created_at")) or upd, "model": r.get("model"),
                "last_prompt": clip(str(r.get("last_prompt") or ""), 500), "final": clip(str(r.get("final") or ""), 2000),
                "final_at": upd if r.get("final") else None}

    # -------------------------------------------------------------- the module surface
    def chats(self, since: float, limit: int = 120) -> list[dict]:
        if self.files:
            paths = []
            for p in self._files():
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt >= since:
                    paths.append((mt, p))
            paths.sort(reverse=True)
            out = [c for c in (self._parse(p) for _, p in paths[:limit]) if c]
        else:
            data, err = self._run("list", limit=limit)
            if err:
                raise RuntimeError(err)
            out = [c for c in (self._chat_from_script(r) for r in (data if isinstance(data, list) else [])) if c]
            out = [c for c in out if c["updated_at"] >= since]
        out.sort(key=lambda c: c["updated_at"], reverse=True)
        return out[:limit]

    def find(self, cid: str) -> dict | None:
        return next((c for c in self.chats(0, 5000) if c["id"] == cid), None)

    def turns(self, cid: str, limit: int = 80) -> list[dict]:
        if self.files:
            c = self.find(cid)
            if not c:
                return []
            msgs = self._messages(self._read_records(c["path"]))[-limit:]
            return [{"role": m["role"], "text": m["text"], "at": m["at"] or c["updated_at"]} for m in msgs]
        data, err = self._run("turns", id=cid, limit=limit)
        if err or not isinstance(data, list):
            return []
        out = []
        for m in data[-limit:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("text") or "").strip():
                out.append({"role": m["role"], "text": str(m["text"])[:6000], "at": _epoch(m.get("at")) or 0.0})
        return out

    def reply(self, cid: str, text: str) -> dict:
        argv = self.spec.get("reply")
        if not argv:
            return {"ok": False, "error": f"{self.LABEL} is read-only here: there is no `reply` command in agents.json"}
        c = self.find(cid)
        if not c:
            return {"ok": False, "error": f"no such {self.LABEL} chat"}
        if c["state"] == "working":
            return {"ok": False, "route": "refused", "error": f"{self.LABEL} is working on this chat right now. Reply when it is idle."}
        argv = _sub(argv, id=cid, text=text, cwd=c.get("cwd") or "")
        exe = which(argv[0], "DHI_ORBIT_NO_SUCH_VAR")
        if not exe:
            return {"ok": False, "error": f"command not found: {argv[0]}"}
        r = run_detached([exe] + argv[1:], c.get("cwd") or "", None, f"{self.id}-{cid[:8]}")
        return {**r, "route": os.path.basename(argv[0])}

    def limits(self) -> dict | None:
        return None

    def terminal_command(self, cid: str) -> str | None:
        argv = self.spec.get("terminal")
        if not argv:
            return None
        import shlex
        return " ".join(shlex.quote(a) for a in _sub(argv, id=cid))


def _glob(pattern: str) -> list[str]:
    import glob
    return glob.glob(pattern, recursive=True)


def _validate(spec, seen: set[str]) -> tuple[Agent | None, str | None]:
    if not isinstance(spec, dict):
        return None, "an entry is not an object"
    aid = spec.get("id")
    if not isinstance(aid, str) or not ID_RE.match(aid):
        return None, f"id {aid!r} must be 2 to 24 lowercase letters, digits, - or _"
    if aid in RESERVED or aid in seen:
        return None, f"id {aid!r} is reserved or used twice"
    f = spec.get("files")
    if f is not None and (not isinstance(f, dict) or not f.get("glob")):
        return None, f"{aid}: `files` needs a `glob`"
    if f is None and not (spec.get("list") and spec.get("turns")):
        return None, f"{aid}: give either `files`, or both `list` and `turns`"
    for k in ("list", "turns", "reply", "terminal"):
        v = spec.get(k)
        if v is not None and not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            return None, f"{aid}: `{k}` must be a list of strings (a command and its arguments, no shell)"
    if f is not None and f.get("format", "jsonl") not in ("jsonl", "json"):
        return None, f"{aid}: files.format must be jsonl or json"
    return Agent(spec), None


def load() -> tuple[list[Agent], list[str]]:
    """The agents described in agents.json and the problems found in it. Cached until the file changes."""
    p = config_path()
    try:
        st = os.stat(p)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return [], []
    if _cache["sig"] == sig:
        return _cache["agents"], _cache["problems"]
    agents, problems, seen = [], [], set()
    try:
        with open(p, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        specs = doc.get("agents") if isinstance(doc, dict) else doc
        if not isinstance(specs, list):
            raise ValueError("expected {\"agents\": [...]}")
    except (OSError, ValueError) as e:
        _cache.update(sig=sig, agents=[], problems=[f"agents.json is not valid: {e}"])
        return [], _cache["problems"]
    for spec in specs:
        a, err = _validate(spec, seen)
        if err:
            problems.append(err)
        else:
            seen.add(a.id)
            agents.append(a)
    _cache.update(sig=sig, agents=agents, problems=problems)
    return agents, problems
