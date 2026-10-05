"""File index for the graph: which session wrote or read which file, and when, across every transcript.

Measured 2026-10-03: 4,998 transcripts, 6,020 distinct files written (6,201 more only read), median 3 per
writing session, 543 written by 2+ sessions; a full scan takes about 16 s. So the index is built once and
then only new transcript bytes are read (offset per file), and the graph never draws all files: it asks for
hot files, one folder level at a time, or a search.

Paths are normalised so one repo file is one node: a worktree copy (<repo>/.claude/worktrees/<name>/x)
becomes <repo>/x; scratch (/tmp, /private, /var/folders) and Claude config dirs (~/.claude*) are dropped.
Subagent transcripts count for their parent session. Tool uses only (Edit, Write, MultiEdit, NotebookEdit =
write; Read = read); a file touched through Bash is not seen (stated, not hidden).
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
import time

from . import db, sources

HOME = os.path.expanduser("~")
WRITE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ = {"Read"}
WT = re.compile(r"/\.claude/worktrees/[^/]+/")
PR = re.compile(rb"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
DROP = ("/tmp/", "/private/", "/var/folders/", "/dev/")
SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_fscan (path TEXT PRIMARY KEY, offset INTEGER, size INTEGER, session_id TEXT, seat TEXT);
CREATE TABLE IF NOT EXISTS cp_touch (norm TEXT, session_id TEXT, kind TEXT, seat TEXT, repo TEXT, n INTEGER,
    first_at REAL, last_at REAL, PRIMARY KEY (norm, session_id, kind));
CREATE INDEX IF NOT EXISTS cp_touch_at ON cp_touch(last_at);
CREATE TABLE IF NOT EXISTS cp_touch_ev (norm TEXT, session_id TEXT, at REAL);
CREATE INDEX IF NOT EXISTS cp_touch_ev_at ON cp_touch_ev(at);
CREATE TABLE IF NOT EXISTS cp_prlink (session_id TEXT, url TEXT, repo TEXT, num INTEGER, at REAL, PRIMARY KEY (session_id, url));
"""
_repo_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def init(path: str | None = None) -> None:
    c = db.connect(path)
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def normalize(p: str | None) -> str | None:
    if not p or not p.startswith("/"):
        return None
    if p.startswith(DROP):
        return None
    if re.match(re.escape(HOME) + r"/\.claude[^/]*/", p) and "/worktrees/" not in p:
        return None                                    # Claude config, memory, jobs scratch
    p = WT.sub("/", p)
    return os.path.normpath(p)


def repo_of(p: str) -> str:
    """Nearest parent with a .git (dir or file); else the first folder under the home directory."""
    d = os.path.dirname(p)
    seen = []
    while d and d != "/" and len(d) > len(HOME):
        if d in _repo_cache:
            r = _repo_cache[d]
            break
        seen.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            r = d
            break
        d = os.path.dirname(d)
    else:
        r = None
    if r is None:
        rel = os.path.relpath(p, HOME).split(os.sep)
        r = os.path.join(HOME, *rel[:2]) if p.startswith(HOME) and len(rel) > 2 else os.path.dirname(p)
    for s in seen:
        _repo_cache[s] = r
    return r


def _ts(s: str | None) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def transcripts() -> list[tuple[str, str, str]]:
    """(path, session_id, seat) for every main and subagent transcript on every seat."""
    out = []
    for cfg in sources.collector.config_dirs():
        seat = sources.collector.account_name(cfg)
        for f in glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl")):
            out.append((f, os.path.basename(f)[:-6], seat))
        for f in glob.glob(os.path.join(cfg, "projects", "*", "*", "subagents", "*.jsonl")):
            out.append((f, os.path.basename(os.path.dirname(os.path.dirname(f))), seat))
    return out


def scan(path: str | None = None, budget_s: float = 60.0) -> dict:
    """Read new bytes of every transcript; returns counts. Safe to call repeatedly."""
    init(path)
    t0 = time.time()
    known = {r["path"]: r for r in db.rows("SELECT path, offset, size FROM cp_fscan", (), path)}
    files = touches = 0
    con = db.connect(path)
    try:
        for f, sid, seat in transcripts():
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            k = known.get(f)
            off = k["offset"] if k and k["size"] <= size else 0
            if k and off >= size:
                continue
            agg: dict[tuple, list] = {}
            evs, prs = [], {}
            with open(f, "rb") as fh:
                fh.seek(off)
                data = fh.read()
            end = data.rfind(b"\n") + 1                    # never parse a half-written last line
            for raw in data[:end].split(b"\n"):
                if b"/pull/" in raw:
                    for m in PR.finditer(raw):
                        url = m.group(0).decode()
                        prs.setdefault(url, (m.group(1).decode(), int(m.group(2))))
                if b'"tool_use"' not in raw:
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                at = _ts(r.get("timestamp")) or time.time()
                for b in (r.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name")
                    kind = "w" if name in WRITE else "r" if name in READ else None
                    if not kind:
                        continue
                    inp = b.get("input") or {}
                    norm = normalize(inp.get("file_path") or inp.get("notebook_path"))
                    if not norm:
                        continue
                    a = agg.setdefault((norm, kind), [0, at, at])
                    a[0] += 1
                    a[1] = min(a[1], at)
                    a[2] = max(a[2], at)
                    if kind == "w":
                        evs.append((norm, sid, at))
            for (norm, kind), (n, fa, la) in agg.items():
                con.execute("INSERT INTO cp_touch(norm, session_id, kind, seat, repo, n, first_at, last_at) VALUES(?,?,?,?,?,?,?,?)"
                            " ON CONFLICT(norm, session_id, kind) DO UPDATE SET n=n+excluded.n,"
                            " first_at=MIN(first_at, excluded.first_at), last_at=MAX(last_at, excluded.last_at)",
                            (norm, sid, kind, seat, repo_of(norm), n, fa, la))
                touches += 1
            con.executemany("INSERT INTO cp_touch_ev(norm, session_id, at) VALUES(?,?,?)", evs)
            now = time.time()
            for url, (repo, num) in prs.items():
                con.execute("INSERT OR IGNORE INTO cp_prlink(session_id, url, repo, num, at) VALUES(?,?,?,?,?)", (sid, url, repo, num, now))
            con.execute("INSERT OR REPLACE INTO cp_fscan(path, offset, size, session_id, seat) VALUES(?,?,?,?,?)",
                        (f, off + end, size, sid, seat))
            files += 1
            if files % 200 == 0:
                con.commit()
            if time.time() - t0 > budget_s:
                break
        con.commit()
    finally:
        con.close()
    return {"files": files, "touches": touches, "secs": round(time.time() - t0, 2)}


class Indexer:
    """Background thread: one scan a minute (the first one indexes everything)."""
    def __init__(self, owner=lambda: True, every: float = 60.0):
        self.owner, self.every, self.last = owner, every, {}
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        time.sleep(5)
        while True:
            try:
                if self.owner():
                    with _lock:
                        self.last = dict(scan(), at=time.time())
            except Exception as e:
                self.last = {"error": f"{type(e).__name__}: {e}", "at": time.time()}
            time.sleep(self.every)


def status(path: str | None = None) -> dict:
    init(path)
    r = db.rows("SELECT COUNT(DISTINCT norm) AS files, COUNT(DISTINCT session_id) AS sessions, MAX(last_at) AS newest"
                " FROM cp_touch WHERE kind='w'", (), path)[0]
    return r
