"""Receipts: what a turn actually did, computed from facts, never from the chat's prose . Sources: the turn's transcript records (tool calls and their results), git in the
session cwd, usage per message.id. Each fact carries its source label."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

from .. import extract              # dhi_orbit/extract.py (model weights)

from . import db

TAIL_BYTES = 8 * 1024 * 1024
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
CHECK_RE = re.compile(r"\b(pytest|py\.test|unittest|tox|nox|npm (run )?(test|build|lint)|pnpm (run )?(test|build|lint)|"
                      r"yarn (test|build|lint)|bun test|vitest|jest|mocha|node --test|go (test|build|vet)|cargo (test|build|check|clippy)|"
                      r"make( |$)|cmake --build|ctest|tsc|eslint|ruff|mypy|pyright|flake8|black --check|swift (test|build)|"
                      r"xcodebuild|gradle|mvn|dotnet (test|build)|playwright|wrangler deploy --dry-run|curl -[a-zA-Z]*f)", re.I)
REDIRECT_RE = re.compile(r"(?:^|[^>&0-9])>>?\s*([~\w./@%+-][^\s;|&<>]*)|\btee\s+(?:-a\s+)?([~\w./@%+-][^\s;|&<>]*)")
DISTINCT = {"pytest", "py.test", "unittest", "tox", "nox", "vitest", "jest", "mocha", "tsc", "eslint", "ruff", "mypy",
            "pyright", "flake8", "xcodebuild", "gradle", "mvn", "playwright", "ctest"}
NOT_VERIFIED_RE = re.compile(r"\b(not verified|unverified|did not (run|verify|test)|didn'?t (run|verify|test)|could not verify|"
                             r"couldn'?t verify|not tested|haven'?t tested|untested|no tests? (were )?run|not checked|"
                             r"no (additional |further |other )?(checks?|tests?|verification) (were |was )?(run|done|performed)|"
                             r"without (running )?(a |any )?(check|test|verification))\b", re.I)
PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
EXIT_RE = re.compile(r"(?:exit(?:ed with)? code|Exit code)\s*[:=]?\s*(\d+)", re.I)
NOISE = ("<task-notification", "<system-reminder", "<local-command", "<command-", "Caveat:", "[keepalive]",
         "<cross-session-message", "[Request interrupted")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                         if not isinstance(b, dict) or b.get("type") in ("text", None))
    return ""


def human_prompt(r: dict) -> bool:
    if r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain"):
        return False
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False
    t = _text(c).strip()
    return bool(t) and not t.startswith(NOISE)


def read_turn(path: str) -> tuple[list[dict], int, str | None]:
    """Records of the current turn (after the last human prompt), the turn number (human prompts seen in
    the tail, a lower bound for huge files) and the session's first timestamp."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            first = fh.readline()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return [], 0, None
    try:
        start_ts = json.loads(first).get("timestamp")
    except ValueError:
        start_ts = None
    recs = []
    for raw in data.split(b"\n"):
        if raw.strip():
            try:
                recs.append(json.loads(raw))
            except ValueError:
                continue
    if not start_ts:
        start_ts = next((r.get("timestamp") for r in recs if r.get("timestamp")), None)
    idx = [i for i, r in enumerate(recs) if human_prompt(r)]
    turn_recs = recs[idx[-1]:] if idx else recs
    return [r for r in turn_recs if not r.get("isSidechain")], len(idx), start_ts


def facts(turn: list[dict]) -> dict:
    """Tool calls of the turn -> files, checks (with verbatim last line), PRs, cost."""
    uses, results, usage, prs = {}, {}, {}, set()
    for r in turn:
        m = r.get("message") or {}
        if r.get("type") == "pr-link" and r.get("prUrl"):
            prs.add(r["prUrl"])
        c = m.get("content") if isinstance(m.get("content"), list) else []
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = (b.get("name"), b.get("input") or {})
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (_text(b.get("content")), bool(b.get("is_error")))
        if r.get("type") == "assistant" and m.get("usage") and m.get("id"):
            u = m["usage"]
            w = extract.weight(m.get("model"))
            usage[m["id"]] = w * ((u.get("cache_creation_input_tokens") or 0) + (u.get("input_tokens") or 0)
                                  + 5 * (u.get("output_tokens") or 0))
        for t in (_text(c) if r.get("type") in ("user", "assistant") else "", ):
            prs.update(PR_RE.findall(t))
    files, checks = {}, []
    for tid, (name, inp) in uses.items():
        if name in EDIT_TOOLS and inp.get("file_path"):
            files[inp["file_path"]] = "tool"
        elif name in ("Read", "Grep", "Glob") and tid in results:
            out, err = results[tid]
            lines = [re.sub(r"^\s*\d+[\t→]", "", l).strip() for l in out.splitlines() if l.strip()]
            what = inp.get("file_path") or inp.get("pattern") or ""
            checks.append({"cmd": f"{name} {what}"[:500], "last_line": lines[-1][:400] if lines else "",
                           "exit": 1 if err else None, "ran": True, "test": False})
        elif name == "Bash":
            cmd = inp.get("command") or ""
            for a, b in REDIRECT_RE.findall(cmd):
                f = a or b
                if f and not f.startswith("/dev/") and f not in ("&1", "&2"):
                    files.setdefault(f, "bash redirect")
            out, err = results.get(tid, ("", False))
            prs.update(PR_RE.findall(out))
            lines = [l for l in out.splitlines() if l.strip()]
            m = EXIT_RE.search(out)
            checks.append({"cmd": cmd[:500], "last_line": lines[-1][:400] if lines else "",
                           "exit": int(m.group(1)) if m else (1 if err else None), "ran": tid in results,
                           "test": bool(CHECK_RE.search(cmd)), "hit": (CHECK_RE.search(cmd) or [None])[0]})
    return {"files": files, "checks": checks, "prs": sorted(prs), "cost_units": round(sum(usage.values()))}


def _git(cwd: str, *args, timeout: float = 5) -> str | None:
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def git_facts(cwd: str | None, since_iso: str | None) -> dict | None:
    """Uncommitted diff plus commits since the session started, in the session cwd. None outside a repo."""
    if not cwd or not os.path.isdir(cwd) or _git(cwd, "rev-parse", "--is-inside-work-tree") is None:
        return None
    add = dele = 0
    files = set()
    for line in (_git(cwd, "diff", "--numstat", "HEAD") or "").splitlines():
        p = line.split("\t")
        if len(p) == 3:
            add += int(p[0]) if p[0].isdigit() else 0
            dele += int(p[1]) if p[1].isdigit() else 0
            files.add(p[2])
    commits = []
    if since_iso:
        log = _git(cwd, "log", f"--since={since_iso}", "--numstat", "--format=@@%h %s") or ""
        for line in log.splitlines():
            if line.startswith("@@"):
                commits.append(line[2:][:120])
            else:
                p = line.split("\t")
                if len(p) == 3:
                    add += int(p[0]) if p[0].isdigit() else 0
                    dele += int(p[1]) if p[1].isdigit() else 0
                    files.add(p[2])
    root = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    return {"files": sorted(files), "add": add, "del": dele, "commits": commits, "root": root}


def build(path: str, cwd: str | None, last_message: str | None = None) -> dict:
    turn, n, start = read_turn(path)
    f = facts(turn)
    g = git_facts(cwd, start)
    files = dict(f["files"])
    for x in (g or {}).get("files", []):
        files.setdefault(os.path.join(g["root"], x) if g and g.get("root") else x, "git")
    check = next((c for c in reversed(f["checks"]) if c["ran"] and c["test"]), None)
    return {
        "turn": n, "at": time.time(), "cwd": cwd,
        "diff": {"files": len(files), "add": g["add"] if g else None, "del": g["del"] if g else None,
                 "source": "git + tool calls" if g else "tool calls (cwd is not a git repo)",
                 "commits": (g or {}).get("commits", [])},
        "tests": {"cmd": check["cmd"], "last_line": check["last_line"], "exit": check["exit"]} if check else None,
        "checks": f["checks"], "prs": f["prs"], "files": [{"path": p, "source": s} for p, s in sorted(files.items())][:200],
        "cost_units": f["cost_units"], "last_message": (last_message or "")[-2000:],
    }


def verdict(rc: dict, last_message: str) -> tuple[bool, str]:
    """The evidence rule, any one passes:
    a check ran this turn and the final names it or quotes its output; no files changed; explicit not-verified."""
    msg = last_message or ""
    if rc["diff"]["files"] == 0:
        return True, "no files changed this turn"
    if NOT_VERIFIED_RE.search(msg):
        return True, "the final message says plainly what is not verified"
    low = msg.lower()
    for c in rc["checks"]:
        if not c["ran"]:
            continue
        cmd = " ".join(c["cmd"].split())
        line = (c["last_line"] or "").strip()
        if len(cmd) >= 4 and cmd.lower() in " ".join(low.split()):
            return True, f"the final names the command it ran: {cmd[:80]}"
        if len(line) >= 3 and line in msg:
            return True, f"the final quotes the output of: {cmd[:80]}"
        if c["test"]:
            m = CHECK_RE.search(c["cmd"])
            if m:
                phrase = m.group(0).strip().lower()
                if " " not in phrase and phrase not in DISTINCT:     # "make" -> "make build": a bare common word proves nothing
                    phrase = " ".join(c["cmd"][m.start():].split()[:2]).lower()
            else:       # the runner name sat past the 500 characters kept of the command: it crashed every Stop hook (23 tracebacks)
                phrase = (c.get("hit") or " ".join(c["cmd"].split()[:2])).strip().lower()
            if (phrase in DISTINCT and re.search(rf"\b{re.escape(phrase)}\b", low)) or (" " in phrase and phrase in low):
                return True, f"check named in the final: {cmd[:80]}"
    if any(c["ran"] for c in rc["checks"]):
        return False, "commands ran but the final message neither names one nor quotes its output"
    return False, "files changed and no check ran this turn"


def store(session_id: str, config: str, rc: dict, verified: bool, path: str | None = None) -> None:
    db.execute("INSERT OR REPLACE INTO receipts(session_id, turn, at, config, cwd, diff, tests, prs, files, cost_units, verified)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (session_id, rc["turn"], rc["at"], config, rc["cwd"], json.dumps(rc["diff"]), json.dumps(rc["tests"]),
                json.dumps(rc["prs"]), json.dumps(rc["files"]), rc["cost_units"], 1 if verified else 0), path)


def latest(session_id: str, limit: int = 20, path: str | None = None) -> list[dict]:
    out = []
    for r in db.rows("SELECT * FROM receipts WHERE session_id=? ORDER BY at DESC LIMIT ?", (session_id, limit), path):
        for k in ("diff", "tests", "prs", "files"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        r["verified"] = bool(r["verified"])
        out.append(r)
    return out


def latest_map(session_ids, path: str | None = None) -> dict:
    out = {}
    for sid in set(session_ids):
        r = latest(sid, 1, path)
        if r:
            out[sid] = r[0]
    return out


def iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"
