"""OpenAI Codex CLI (and the Codex app / VS Code extension: they share ~/.codex).

Read from: state_*.sqlite `threads` (id, cwd, title, updated_at, source) and thread_history_*.sqlite (`thread_turns` for
status, `thread_items` for the messages). Threads from before the paginated history (history_mode "legacy") keep their
messages in the rollout jsonl; those show their title and last message only.
Reply: `codex exec resume <id> <text>` in the thread's folder, detached, so a long turn does not hold the request.
A thread whose latest turn is still running is refused (two writers corrupt a session)."""
from __future__ import annotations

import glob
import json
import os
import re
import time

from . import cached_version, clip, ro_connect, run_detached, which

LABEL = "Codex"
STALE_RUNNING_S = 600          # a turn "inProgress" with no update for 10 minutes is a dead record, not work


def home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


def _newest(pattern: str) -> str | None:
    def num(p):
        m = re.search(r"_(\d+)\.sqlite$", p)
        return int(m.group(1)) if m else 0
    hits = sorted(glob.glob(os.path.join(home(), pattern)), key=num)
    return hits[-1] if hits else None


def exe() -> str | None:
    return which("codex", "DHI_ORBIT_CODEX_BIN")


def info() -> dict:
    e, st = exe(), _newest("state_*.sqlite")
    return {"available": bool(st), "version": cached_version(e) if e else None, "can_reply": bool(e),
            "detail": None if st else f"no Codex data in {home()}"}


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _source(raw: str | None) -> tuple[str, str | None]:
    """(source label, parent thread id) from threads.source: 'cli', 'vscode', 'exec' or a subagent JSON object."""
    if raw and raw.startswith("{"):
        try:
            j = json.loads(raw)
        except ValueError:
            return "other", None
        sub = j.get("subagent")
        if isinstance(sub, dict):
            return "subagent", (sub.get("thread_spawn") or {}).get("parent_thread_id")
        return "subagent" if sub else "other", None
    return raw or "other", None


def _text_of(item_json: str | None) -> str:
    try:
        j = json.loads(item_json or "{}")
    except ValueError:
        return ""
    if j.get("type") == "agentMessage":
        return j.get("text") or ""
    if j.get("type") == "userMessage":
        return "\n".join(c.get("text", "") for c in j.get("content") or [] if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _row_to_chat(r: dict, turn: dict | None, hist) -> dict:
    src, parent = _source(r.get("source"))
    cid = r["id"]
    upd = (r.get("updated_at_ms") or (r.get("updated_at") or 0) * 1000) / 1000
    state, err, final, prompt = "idle", None, "", ""
    if turn:
        status = turn["status"]
        if status == "inProgress":
            state = "working" if time.time() - upd < STALE_RUNNING_S else "stopped"
        elif status == "failed":
            state = "failed"
            try:
                err = (json.loads(turn["error_json"] or "{}") or {}).get("message")
            except ValueError:
                err = None
        elif status == "interrupted":
            state = "stopped"
        if hist is not None:
            for col, name in (("final_agent_item_id", "final"), ("first_user_item_id", "prompt")):
                iid = turn.get(col)
                if iid:
                    row = hist.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_id=?", (cid, iid)).fetchone()
                    if row:
                        if name == "final":
                            final = _text_of(row[0])
                        else:
                            prompt = _text_of(row[0])
            if not final:
                row = hist.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND item_type='agentMessage' "
                                   "ORDER BY rollout_ordinal DESC LIMIT 1", (cid,)).fetchone()
                final = _text_of(row[0]) if row else ""
    name = (r.get("name") or r.get("title") or r.get("first_user_message")
            or f"{os.path.basename((r.get('cwd') or '').rstrip('/\\')) or 'Codex'} thread {cid[:6]}")
    return {"key": f"codex:{cid}", "provider": "codex", "id": cid, "name": clip(name.replace("\n", " "), 90),
            "cwd": r.get("cwd"), "state": state, "error": err, "updated_at": upd,
            "created_at": (r.get("created_at_ms") or (r.get("created_at") or 0) * 1000) / 1000,
            "model": r.get("model"), "source": src, "parent": parent, "archived": bool(r.get("archived")),
            "last_prompt": clip(prompt or r.get("first_user_message"), 500), "final": clip(final, 2000),
            "final_at": upd if final else None, "agent": r.get("agent_nickname"), "history": r.get("history_mode") or "legacy"}


def chats(since: float, limit: int = 120) -> list[dict]:
    st = _newest("state_*.sqlite")
    if not st:
        return []
    hp = _newest("thread_history_*.sqlite")
    db = ro_connect(st)
    hist = ro_connect(hp) if hp else None
    try:
        cols = _cols(db, "threads")
        want = [c for c in ("id", "cwd", "title", "name", "updated_at", "updated_at_ms", "created_at", "created_at_ms", "source",
                            "model", "archived", "first_user_message", "agent_nickname", "history_mode") if c in cols]
        cur = db.execute(f"SELECT {','.join(want)} FROM threads WHERE updated_at >= ? "
                         + ("AND archived=0 " if "archived" in cols else "")
                         + "AND source NOT LIKE '{%' "              # sub-agent threads: rows of one run, not chats
                         + "ORDER BY updated_at DESC LIMIT ?", (int(since), limit))
        rows = [dict(zip(want, r)) for r in cur.fetchall()]
        turns: dict[str, dict] = {}
        if hist is not None and rows:
            ids = [r["id"] for r in rows]
            q = ",".join("?" * len(ids))
            for r in hist.execute(
                    f"SELECT t.thread_id,t.status,t.error_json,t.final_agent_item_id,t.first_user_item_id FROM thread_turns t "
                    f"JOIN (SELECT thread_id, MAX(rollout_ordinal) m FROM thread_turns WHERE thread_id IN ({q}) GROUP BY 1) x "
                    f"ON x.thread_id=t.thread_id AND x.m=t.rollout_ordinal", ids):
                turns[r[0]] = {"status": r[1], "error_json": r[2], "final_agent_item_id": r[3], "first_user_item_id": r[4]}
        out = [_row_to_chat(r, turns.get(r["id"]), hist) for r in rows]
    finally:
        db.close()
        if hist is not None:
            hist.close()
    return out


def find(cid: str) -> dict | None:
    for c in chats(0, 5000):
        if c["id"] == cid:
            return c
    return None


def turns(cid: str, limit: int = 80) -> list[dict]:
    hp = _newest("thread_history_*.sqlite")
    if not hp:
        return []
    hist = ro_connect(hp)
    try:
        rows = hist.execute("SELECT item_type,item_json,created_at_ms FROM thread_items WHERE thread_id=? "
                            "AND item_type IN ('userMessage','agentMessage') ORDER BY rollout_ordinal DESC LIMIT ?",
                            (cid, limit)).fetchall()
    finally:
        hist.close()
    out = []
    for typ, js, ms in reversed(rows):
        text = _text_of(js).strip()
        if not text:
            continue
        try:
            phase = json.loads(js).get("phase")
        except ValueError:
            phase = None
        out.append({"role": "user" if typ == "userMessage" else "assistant", "text": text[:6000], "at": (ms or 0) / 1000,
                    "phase": phase})
    return out


def reply(cid: str, text: str) -> dict:
    e = exe()
    if not e:
        return {"ok": False, "error": "the codex command is not on PATH (set DHI_ORBIT_CODEX_BIN)"}
    c = find(cid)
    if not c:
        return {"ok": False, "error": "no such Codex thread (it may be archived)"}
    if c["state"] == "working":
        return {"ok": False, "route": "refused", "error": "Codex is working on this thread right now. Reply when it is idle."}
    r = run_detached([e, "exec", "resume", "--skip-git-repo-check", cid, text], c.get("cwd") or "", None, f"codex-{cid[:8]}")
    return {**r, "route": "codex exec resume"}


def limits() -> dict | None:
    """The account's usage limits as Codex last reported them: every turn writes a token_count event with `rate_limits`
    (primary = the 5 h window, secondary = the 7 d window: used_percent, window_minutes, resets_at) into its rollout
    jsonl. The newest such event across the latest rollouts is the reading. None when no event has them (never used,
    or an API-key login, which has no plan limits)."""
    files = glob.glob(os.path.join(home(), "sessions", "*", "*", "*", "rollout-*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in files[:8]:
        try:
            with open(path, "rb") as fh:
                size = os.path.getsize(path)
                fh.seek(max(0, size - 600_000))
                tail = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            if '"rate_limits"' not in line or '"primary"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rl = (ev.get("payload") or {}).get("rate_limits") or {}
            pri, sec = rl.get("primary"), rl.get("secondary")
            if not isinstance(pri, dict) and not isinstance(sec, dict):
                continue
            at = _iso_epoch(ev.get("timestamp"))
            def one(w):
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    return None, None
                return float(w["used_percent"]), w.get("resets_at")
            fp, fr = one(pri)
            sp, sr = one(sec)
            return {"five": fp, "five_resets": fr, "seven": sp, "seven_resets": sr, "at": at,
                    "plan": rl.get("plan_type")}
    return None


def _iso_epoch(ts) -> float | None:
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
