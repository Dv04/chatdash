"""Decision inbox: AskUserQuestion calls from background chats become decision rows on the board.

Lifecycle (design rules):
  hook (mode on) writes the row with held=1 and polls it until hold_until.
    The user answers while held   -> state answered; the hook returns allow + updatedInput {questions, answers}.
    hold ends, low risk and every part has a recommended option -> the recommended answers are taken.
    hold ends otherwise      -> held=0 and the chat shows its own dialog; the card stays answerable and
                                an answer is typed into that dialog (single-question dialogs only).
  mode dry-run: the row is written with held=0 (no hold); the dialog shows at once; the card answers it.
The held=1 -> 0 flip is one conditional UPDATE, so an answer arriving at the same instant is either
taken by the hook or routed to the dialog, never lost.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime

from . import db, risk

REC = "(recommended)"
MARGIN_S = 30          # hold ends this long before the hook's own timeout


def parts_from_input(tool_input: dict) -> list[dict]:
    out = []
    for q in (tool_input or {}).get("questions") or []:
        opts = [{"id": str(i + 1), "label": o.get("label", ""), "desc": o.get("description", "")}
                for i, o in enumerate(q.get("options") or [])]
        rec = next((o["id"] for o in opts if o["label"].strip().lower().endswith(REC)), None)
        out.append({"question": q.get("question", ""), "header": q.get("header", ""),
                    "multi": bool(q.get("multiSelect")), "options": opts, "recommended": rec})
    return out


def decision_id(session_id: str, tool_use_id: str | None, parts: list[dict]) -> str:
    tail = tool_use_id or hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    return f"{(session_id or 'nosess')[:8]}-{tail[-10:]}"


def create(payload: dict, *, mode: str, hold_s: float, seat: str, config: str, job_id: str | None,
           work_item: str | None = None, now: float | None = None, path: str | None = None) -> dict:
    now = now or time.time()
    parts = parts_from_input(payload.get("tool_input") or {})
    lvl, why = risk.of_parts(parts)
    default_ok = lvl == "low" and parts and all(p["recommended"] for p in parts)
    held = mode == "on"
    row = {
        "id": decision_id(payload.get("session_id"), payload.get("tool_use_id"), parts),
        "session_id": payload.get("session_id"), "config": config, "seat": seat, "work_item": work_item,
        "source": "hook", "asked_at": now,
        "question": " / ".join(p["question"] for p in parts)[:2000],
        "options": json.dumps(parts[0]["options"] if parts else []),
        "recommended": parts[0]["recommended"] if parts else None,
        "risk": lvl, "risk_why": why, "on_timeout": "default" if default_ok else "dialog",
        "timeout_at": now + hold_s if held else now, "state": "open",
        "parts": json.dumps(parts), "tool_use_id": payload.get("tool_use_id"), "held": 1 if held else 0,
        "hold_until": now + hold_s if held else now, "job_id": job_id, "mode": mode,
        "evidence": json.dumps({"transcript": payload.get("transcript_path"), "cwd": payload.get("cwd")}),
    }
    cols = ", ".join(row)
    db.execute(f"INSERT OR REPLACE INTO decisions({cols}) VALUES({', '.join('?' * len(row))})",
               tuple(row.values()), path)
    return row


def get(did: str, path: str | None = None) -> dict | None:
    r = db.rows("SELECT * FROM decisions WHERE id=?", (did,), path)
    if not r:
        return None
    d = r[0]
    d["parts"] = json.loads(d.get("parts") or "[]")
    d["options"] = json.loads(d.get("options") or "[]")
    return d


def answers_for(parts: list[dict], body: dict) -> tuple[dict | None, str | None]:
    """-> ({question text: answer}, None) or (None, error). Every part must be answered (Rule:
    nothing is sent until all parts have an answer). body: {option_id | text} for one part, or
    {answers: {"<part index>": option_id | [ids] | {"text": ...}}} for several."""
    if not parts:
        return None, "decision has no questions"
    per = body.get("answers")
    if per is None:
        if len(parts) != 1:
            return None, f"this decision has {len(parts)} parts; answer all of them"
        per = {"0": {"text": body["text"]} if body.get("text") else body.get("option_id")}
    out = {}
    for i, p in enumerate(parts):
        a = per.get(str(i), per.get(i))
        if a is None or a == "" or a == []:
            return None, f"part {i + 1} is not answered"
        if isinstance(a, dict):
            text = (a.get("text") or "").strip()
            if not text:
                return None, f"part {i + 1} is empty"
            out[p["question"]] = text
            continue
        ids = a if isinstance(a, list) else [a]
        if len(ids) > 1 and not p["multi"]:
            return None, f"part {i + 1} takes one option"
        labels = []
        for oid in ids:
            o = next((o for o in p["options"] if o["id"] == str(oid)), None)
            if not o:
                return None, f"part {i + 1}: no option {oid}"
            labels.append(o["label"])
        out[p["question"]] = ", ".join(labels)
    return out, None


def recommended_answers(parts: list[dict]) -> dict | None:
    if not parts or not all(p["recommended"] for p in parts):
        return None
    return {p["question"]: next(o["label"] for o in p["options"] if o["id"] == p["recommended"]) for p in parts}


def take_if_held(did: str, answers: dict, by: str = "user", path: str | None = None) -> bool:
    """Atomic: record the answer only while the hook still holds the call."""
    return db.execute("UPDATE decisions SET state='answered', answer=?, answered_at=?, answered_by=?"
                      " WHERE id=? AND state='open' AND held=1",
                      (json.dumps(answers), time.time(), by, did), path) == 1


def release(did: str, path: str | None = None) -> bool:
    """Hook side at the end of the hold: True if it released (nobody answered), False if an answer won."""
    return db.execute("UPDATE decisions SET held=0 WHERE id=? AND state='open' AND held=1", (did,), path) == 1


def mark_delivered(did: str, how: str, path: str | None = None) -> None:
    db.execute("UPDATE decisions SET delivered=1, delivery=? WHERE id=?", (how, did), path)


def answer(did: str, body: dict, chat: dict | None, dialog_fn, path: str | None = None) -> tuple[int, dict]:
    """Board side. dialog_fn(chat, n, label) types option n into the chat's on-screen dialog."""
    d = get(did, path)
    if not d:
        return 404, {"error": "no such decision"}
    if d["state"] != "open":
        return 409, {"error": f"already {d['state']}", "answered_by": d.get("answered_by")}
    answers, err = answers_for(d["parts"], body)
    if err:
        return 400, {"error": err}
    if d["held"] and take_if_held(did, answers, path=path):
        return 200, {"ok": True, "route": "hook", "confirmed": False,
                     "note": "the chat's hook picks it up within a second; the card shows delivered when it has"}
    # not held (dry-run, or the hold already ended): the chat is showing its own dialog
    if len(d["parts"]) != 1 or d["parts"][0]["multi"]:
        return 409, {"error": "the hold ended and this question has several parts or multi-select; answer it in the chat (Terminal)"}
    if chat is None:
        return 409, {"error": "the asking chat is not visible right now"}
    label = answers[d["parts"][0]["question"]]
    opt = next((o for o in d["parts"][0]["options"] if o["label"] == label), None)
    if not opt:
        return 409, {"error": "free-text answers cannot be typed into the dialog from here; use Terminal"}
    res = dialog_fn(chat, int(opt["id"]), label)
    if not res.get("ok"):
        return 409, {"error": res.get("error") or "dialog did not take the answer", "route": "dialog"}
    db.execute("UPDATE decisions SET state='answered', answer=?, answered_at=?, answered_by='user', delivered=1,"
               " delivery='dialog' WHERE id=? AND state='open'", (json.dumps(answers), time.time(), did), path)
    return 200, {"ok": True, "route": "dialog", "confirmed": True}


# ------------------------------------------------------------------ answered elsewhere
# A dialog answered in the terminal (dry-run, or after a hold fell back to the dialog) never passes
# through the board. The transcript is the authority: a tool_result for the decision's tool_use_id
# means the question is closed. Read back from the transcript, not inferred from activity.
_scanned: dict[str, tuple[float, int]] = {}      # decision id -> (mtime, size) of the last scan
_last_run = [-1e9]
EXPIRE_DEAD_S = 3600                             # open, no answer, session gone this long -> expired


def _find_result(tpath: str, tool_use_id: str) -> dict | None:
    needle = tool_use_id.encode()            # spacing-independent prefilter; the parse below decides
    try:
        with open(tpath, "rb") as fh:
            for raw in fh:
                if needle not in raw or b'"tool_result"' not in raw:
                    continue
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                for b in (r.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id:
                        tur = r.get("toolUseResult")
                        ans = tur.get("answers") if isinstance(tur, dict) else None
                        c = b.get("content")
                        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                        return {"ts": r.get("timestamp"), "answers": ans, "text": text[:2000], "error": bool(b.get("is_error"))}
    except OSError:
        return None
    return None


def reconcile(chats_by_sid: dict, now: float | None = None, path: str | None = None, every: float = 5.0) -> int:
    """Close open decisions whose answer is already in the transcript. Returns how many closed."""
    now = now or time.time()
    tick = time.monotonic()
    if every and tick - _last_run[0] < every:
        return 0
    _last_run[0] = tick
    closed = 0
    for d in db.rows("SELECT id, session_id, tool_use_id, evidence, asked_at FROM decisions WHERE state='open'", (), path):
        c = chats_by_sid.get(d["session_id"]) or {}
        try:
            tpath = json.loads(d.get("evidence") or "{}").get("transcript") or c.get("path")
        except ValueError:
            tpath = c.get("path")
        res = None
        if tpath and d.get("tool_use_id"):
            try:
                st = os.stat(tpath)
                sig = (st.st_mtime, st.st_size)
            except OSError:
                sig = None
            if sig and _scanned.get(d["id"]) != sig:
                _scanned[d["id"]] = sig
                res = _find_result(tpath, d["tool_use_id"])
        if res:
            how = "declined in the terminal" if res["error"] else "answered in the terminal"
            ans = res["answers"] or {"_": res["text"]}
            at = _epoch(res["ts"]) or now
            closed += db.execute("UPDATE decisions SET state='answered', answer=?, answered_at=?, answered_by='terminal',"
                                 " delivered=1, delivery=? WHERE id=? AND state='open'",
                                 (json.dumps(ans), at, how, d["id"]), path)
            continue
        # No answer in the transcript: the question stays open even if the chat stopped (rule,
        # 2026-10-03: unanswered stays until answered or dismissed).
    return closed


def _epoch(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None
    except ValueError:
        return None
