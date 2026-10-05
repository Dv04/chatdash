"""Contract v0 under /api/cp/. handle() is transport-free so both the 8788 dev server and (at S5)
chatdash's own server can mount it. Absence is never green: any unreadable source makes
health "unknown" and the affected objects carry state "unknown"."""
from __future__ import annotations

import json
import time

from .. import config
from . import db, limits, resume, sources

STALE_S = 60          # a snapshot older than this is not "ok"


def _seconds(now: float, since: float | None) -> int | None:
    return None if not since else max(0, int(now - since))


def _final(c: dict) -> dict:
    return {"final": (c.get("final") or "")[-1500:], "final_at": c.get("final_at"), "last_prompt": (c.get("last_prompt") or "")[-400:]}


DISMISS_SCHEMA = "CREATE TABLE IF NOT EXISTS cp_dismissed (item TEXT PRIMARY KEY, at REAL, title TEXT)"


def _dkey(item_id: str, since) -> str:
    return f"{item_id}@{round(float(since or 0))}"


def dismissed(item_id: str, since) -> bool:
    try:
        return bool(db.rows("SELECT 1 FROM cp_dismissed WHERE item=?", (_dkey(item_id, since),)))
    except Exception:
        return False


def dismiss(item_id: str, since, title: str = "") -> None:
    db.execute(DISMISS_SCHEMA)
    db.execute("INSERT OR REPLACE INTO cp_dismissed(item, at, title) VALUES(?,?,?)", (_dkey(item_id, since), time.time(), title[:200]))
    db.log_auto("dismiss", "on", None, None, "dismissed", f"{item_id}: {title}"[:300])


def answered_after(c: dict, since: float | None) -> bool:
    """A prompt reached the chat after `since` (the user typed it, or the board sent it). Keep-warm pings are
    not prompts (extract hides them); limit-resume's own line is not an answer."""
    pa = (c or {}).get("last_prompt_at")
    if not pa or not since:
        return False
    if (c.get("last_prompt") or "").strip() == resume.RESUME_TEXT:
        return False
    from datetime import datetime
    try:
        t = datetime.fromisoformat(pa.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return t > since + 1


def needs_you(snap: dict, now: float) -> list[dict]:
    """Everything waiting on the user, oldest first: open decisions, dialogs on screen, blocked jobs
    (Claude Code's own 'needs'), and limit-stalled chats whose reset has passed."""
    out, have = [], set()
    by_sid = {c["session_id"]: c for c in snap["chats"]}
    try:
        from . import decisions as _dec
        _dec.reconcile(by_sid, now)            # answered in the terminal -> closed here too
    except Exception:
        pass
    for d in db.rows("SELECT * FROM decisions WHERE state='open' OR (state='answered' AND delivered=0"
                     " AND answered_at > ?) ORDER BY asked_at", (now - 600,)):
        have.add(d["session_id"])
        parts = json.loads(d.get("parts") or "[]")
        out.append({"id": f"decision:{d['id']}", "kind": "decision", "decision_id": d["id"],
                    "session_id": d["session_id"], "seat": d["seat"], "work_item": d["work_item"],
                    "title": (parts[0]["header"] if parts and parts[0].get("header") else d["question"])[:140],
                    "text": d["question"], "parts": parts, "options": json.loads(d["options"] or "[]"),
                    "recommended": d["recommended"], "since": d["asked_at"], "seconds": _seconds(now, d["asked_at"]),
                    "risk": d["risk"], "risk_why": d.get("risk_why"), "on_timeout": d["on_timeout"],
                    "timeout_at": d.get("hold_until") if d.get("held") else None, "held": bool(d.get("held")),
                    "mode": d.get("mode"), "delivery": "pending" if d["state"] == "answered" else None,
                    "running": bool((by_sid.get(d["session_id"]) or {}).get("live")),
                    "screen": (by_sid.get(d["session_id"]) or {}).get("dialog"),
                    "excluded": sources.excluded(d.get("config"))})
    for c in snap["chats"]:
        if c["state"] == "needs_you" and c["session_id"] not in have and c["kind"] != "headless":
            have.add(c["session_id"])
            since = c.get("activity")
            out.append({"id": f"dialog:{c['key']}", "kind": "dialog", "session_id": c["session_id"],
                        "key": c["key"], "seat": c["account"], "work_item": c.get("ws"),
                        "title": c["name"], "text": c.get("waiting_for") or c.get("pending_tool") or "",
                        "since": since, "seconds": _seconds(now, since), "excluded": c["excluded"],
                        "screen": c.get("dialog")})
    for j in snap["jobs"]:
        if j["state"] != "blocked" or j["session_id"] in have:
            continue
        c = by_sid.get(j["session_id"]) or {}
        b = j.get("banner")
        # One rule for every question: it stays until there is evidence it was answered (a prompt typed
        # into that chat after it blocked) or the user dismisses it; whether the process still runs does not
        # decide it (answering a stopped chat resumes it). A limit stall is not a question: a dead one
        # stays only while limit-resume would still act on it (MAX_STALL_S).
        if b:
            if not c.get("live") and now - b["resets_at"] > resume.MAX_STALL_S:
                continue
        asked = j.get("blocked_since") or j["updated"]           # updatedAt moves when the daemon rewrites the job
        if not b:
            # The user's prompt answers the ask only until the chat speaks again: a reply after it, with the job still
            # blocked and the chat not working, is the chat asking again (from that reply's time). Without this a
            # chat that answered the user's side remark and re-asked was hidden for good (2026-10-04, 4 live chats).
            pa, fa = sources.iso_epoch(c.get("last_prompt_at")), sources.iso_epoch(c.get("final_at"))
            if pa and fa and pa > asked + 1 and fa > pa and c.get("state") != "working":
                asked = fa
            echo = (j.get("needs") or j.get("detail") or "").strip()
            if answered_after(c, asked) or dismissed(f"blocked:{j['seat']}:{j['id']}", asked) \
                    or (echo and echo == (c.get("last_prompt") or "").strip()):   # the "question" is the user's own reply
                continue
        if b:                         # a limit stall is limit-resume's job until the reset has passed
            if b["resets_at"] > now - 300:
                continue
            if dismissed(f"limit:{j['seat']}:{j['id']}", b["resets_at"]):
                continue
            have.add(j["session_id"])
            out.append({"id": f"limit:{j['seat']}:{j['id']}", "kind": "limit", "running": bool(c.get("live")), "session_id": j["session_id"],
                        "key": c.get("key") or f"{j['seat']}:{j['session_id']}", "job_id": j["id"],
                        "seat": j["seat"], "work_item": config.work_item_of(j["name"]),
                        "title": j["name"] or j["id"],
                        "text": f"Limit reset at {limits.fmt_clock(b['resets_at'])} passed; chat still stalled",
                        "since": b["resets_at"], "seconds": _seconds(now, b["resets_at"]),
                        "excluded": sources.excluded(j["config"])})
            continue
        have.add(j["session_id"])
        out.append({"id": f"blocked:{j['seat']}:{j['id']}", "kind": "blocked", "session_id": j["session_id"],
                    "running": bool(c.get("live")),
                    "key": c.get("key") or f"{j['seat']}:{j['session_id']}", "job_id": j["id"],
                    "seat": j["seat"], "work_item": config.work_item_of(j["name"]),
                    "title": j["name"] or j["id"], "text": j.get("needs") or j.get("detail") or "",
                    "suggested": j.get("suggested"), "since": asked,
                    "seconds": _seconds(now, asked), "excluded": sources.excluded(j["config"])})
    for c in snap["chats"]:
        b = c.get("banner")
        if b and b["resets_at"] <= now - 300 and c["session_id"] not in have \
                and (c.get("live") or now - b["resets_at"] <= resume.MAX_STALL_S) \
                and not dismissed(f"limit:{c['key']}", b["resets_at"]):
            out.append({"id": f"limit:{c['key']}", "kind": "limit", "session_id": c["session_id"],
                        "key": c["key"], "seat": c["account"], "work_item": c.get("ws"), "title": c["name"],
                        "text": f"Limit reset at {limits.fmt_clock(b['resets_at'])} passed; chat still stalled",
                        "since": b["resets_at"], "seconds": _seconds(now, b["resets_at"]),
                        "excluded": c["excluded"]})
    from . import shadow, receipts
    rmap = receipts.latest_map([x["session_id"] for x in out])
    for x in out:
        x["receipt"] = rmap.get(x["session_id"])
        x["gate_on"] = gate_state(x["session_id"], x.get("title") or "")
        c = by_sid.get(x["session_id"])
        x.update(_final(c) if c else {"final": "", "final_at": None, "last_prompt": ""})
        if x["kind"] in ("blocked", "dialog"):
            from . import drafts
            dr = drafts.latest_draft(x["session_id"])
            if dr and c and dr["basis"] == f"{c['session_id']}:{c.get('final_at')}":
                x["draft"] = {"id": dr["id"], "class": dr["class"], "text": dr["draft"].get("text", ""),
                              "pairs": [{"final": p["final"][-200:], "reply": p["reply"][:300]} for p in dr["draft"].get("pairs", [])][:4]}
            sug = shadow.latest_for(x["session_id"])
            if sug and c and sug["basis"] == f"{c['session_id']}:{c.get('final_at')}":
                x["suggested_parts"] = sug["parts"]
    out.sort(key=lambda x: x["since"] or now)
    return out


def gate_state(session_id: str, name: str) -> bool:
    """Is the evidence gate on for this chat: the per-chat switch if set, else on when the chat's work item
    is listed in config evidence_gate_work_items (empty by default, so off)."""
    try:
        r = db.rows("SELECT on_ FROM cp_gate WHERE session_id=?", (session_id,))
    except Exception:
        r = []
    if r:
        return bool(r[0]["on_"])
    wi = config.work_item_of(name)
    return bool(wi and wi in config.gate_work_items())


def proposals(now: float) -> list[dict]:
    """Apply / Skip items (state-doc updates, handoffs, rules). Kept out of
    needs_you so they never bury a real wait or move the waiting-longest headline."""
    out = []
    try:
        from . import work
        for p in work.proposals_open():
            out.append({"id": f"proposal:{p['id']}", "kind": "proposal", "proposal_id": p["id"], "proposal_kind": p["kind"],
                        "session_id": p.get("session_id"), "work_item": p.get("work_item"),
                        "title": {"state_doc": f"State of play update: {p.get('work_item')}", "handoff": f"Handoff: {p.get('detail') or p.get('work_item')}",
                                  "rule": "Proposed rule from your corrections"}.get(p["kind"], p["kind"]),
                        "text": p.get("why") or "", "target": p.get("target"), "since": p["created_at"],
                        "seconds": _seconds(now, p["created_at"])})
    except Exception:
        pass
    return out


def health(snap: dict, now: float) -> dict:
    reasons = [f"{k}: {v}" for k, v in (snap.get("errors") or {}).items()]
    if not snap.get("at"):
        reasons.append("no snapshot yet")
    elif now - snap["at"] > STALE_S:
        reasons.append(f"snapshot {int(now - snap['at'])} s old")
    unknown = [s["seat"] for s in snap["seats"] if s["state"] == "unknown"]
    if unknown:
        reasons.append("seat data unknown: " + ", ".join(unknown))
    return {"state": "ok" if not reasons else "unknown", "reasons": reasons}


def visible(c: dict) -> bool:
    """Chats the board shows: background and interactive (headless worker runs and closed chats only
    while they are working)."""
    return c["kind"] in ("bg", "interactive") or c["state"] in ("working", "needs_you")


def overview(snap: dict, now: float) -> dict:
    ny = needs_you(snap, now)
    chats = [c for c in snap["chats"] if visible(c)]
    counts = {"needs_you": len(ny), "decisions_open": sum(1 for x in ny if x["kind"] == "decision")}
    for st in ("working", "needs_you", "idle", "stopped"):
        counts["sessions_" + st] = sum(1 for c in chats if c["state"] == st)
    counts["sessions"] = len(chats)
    counts["limited"] = len({c["session_id"] for c in chats if c.get("banner")}
                            | {j["session_id"] for j in snap["jobs"] if j.get("banner") and j["state"] == "blocked"})
    counts["seats_blocked"] = sum(1 for s in snap["seats"] if s["state"] == "blocked")
    oldest = next((x for x in ny if x["seconds"] is not None), None)
    h = health(snap, now)
    props = proposals(now)
    counts["proposals"] = len(props)
    return {"generated_at": now, "snapshot_at": snap.get("at"), "health": h["state"], "proposals": props,
            "health_reasons": h["reasons"], "needs_you": ny,
            "longest_wait": ({"id": oldest["id"], "seconds": oldest["seconds"], "kind": oldest["kind"],
                              "title": oldest["title"]} if oldest else None),
            "counts": counts, "capacity": capacity(snap, now)}


def capacity(snap: dict, now: float) -> dict:
    return {"generated_at": now, "seats": snap["seats"]}


STATE_OF = {"working": "working", "needs_you": "needs_you", "idle": "idle", "stopped": "stopped",
            "failed": "failed"}


def _repo(c: dict) -> str | None:
    try:
        from . import graphx
        r = graphx.repo_for(c)
        return r.replace(graphx.fileindex.HOME + "/", "~/") if r else None
    except Exception:
        return None


def graph(snap: dict, now: float) -> dict:
    nodes, edges, wis = [], [], {}
    ny = {x["session_id"] for x in needs_you(snap, now)}
    from . import receipts
    rmap = receipts.latest_map([c["session_id"] for c in snap["chats"] if visible(c)])
    for s in snap["seats"]:
        nodes.append({"id": f"seat:{s['seat']}", "type": "seat", "label": s["label"], "state": s["state"],
                      "seat": s["seat"], "spend": s["five_hour"]["pct"], "parent": None,
                      "excluded": s["excluded"]})
    for c in snap["chats"]:
        if not visible(c):
            continue
        wi = c.get("ws")
        sid = f"session:{c['key']}"
        if wi:
            wis.setdefault(wi, []).append(c)
        rc = rmap.get(c["session_id"])
        nodes.append({"id": sid, "type": "session", "label": c["name"], "state": STATE_OF.get(c["state"], "unknown"),
                      "receipt": {"files": rc["diff"]["files"], "add": rc["diff"].get("add"), "del": rc["diff"].get("del"),
                                  "test": (rc["tests"] or {}).get("last_line"), "verified": rc["verified"],
                                  "prs": rc["prs"]} if rc else None,
                      "gate_on": gate_state(c["session_id"], c["name"]), "session_id": c["session_id"], "key": c["key"],
                      "seat": c["account"], "spend": c.get("units_today") or 0,
                      "parent": f"work_item:{wi}" if wi else None, "needs_you": c["session_id"] in ny,
                      "repo": _repo(c),
                      "limited": bool(c.get("banner")), "kind": c["kind"], "final": (c.get("final") or "")[-280:],
                      "activity": c.get("activity")})
        edges.append({"from": sid, "to": f"seat:{c['account']}", "kind": "runs_on"})
        if wi:
            edges.append({"from": sid, "to": f"work_item:{wi}", "kind": "belongs_to"})
    titles = {w["id"]: w["title"] for w in db.rows("SELECT id, title FROM work_items")}
    for wi, cs in sorted(wis.items()):
        state = ("needs_you" if any(c["session_id"] in ny for c in cs) else
                 "working" if any(c["state"] == "working" for c in cs) else "idle")
        nodes.append({"id": f"work_item:{wi}", "type": "work_item", "label": titles.get(wi) or wi, "state": state,
                      "seat": None, "spend": sum(c.get("units_today") or 0 for c in cs), "parent": None})
    for j in snap.get("fleet") or []:
        age = now - (j.get("at") or 0)
        if age > 24 * 3600:
            continue                   # an external job status has no liveness: old "working" rows are history
        jid = f"job:{j['id']}"
        seat = sources.collector.account_name(j["account"]) if j.get("account") else None
        state = j.get("status") or "unknown"
        if state == "working" and age > 6 * 3600:
            state = "unknown"
        nodes.append({"id": jid, "type": "job", "label": j.get("role") or j["id"], "state": state,
                      "seat": seat, "spend": 0, "parent": None})
        if seat:
            edges.append({"from": jid, "to": f"seat:{seat}", "kind": "runs_on"})
    # subagents and other workers spawned by a visible session: transcripts under <session>/subagents/
    import glob as _glob
    import os as _os
    for c in snap["chats"]:
        if not visible(c) or not c.get("path"):
            continue
        for p in _glob.glob(c["path"][:-6] + "/subagents/*.jsonl"):
            try:
                mt = _os.path.getmtime(p)
            except OSError:
                continue
            if now - mt > 1800:
                continue
            jid = f"job:sub:{_os.path.basename(p)[:-6]}"
            try:
                meta = json.load(open(p[:-6] + ".meta.json"))
            except (OSError, ValueError):
                meta = {}
            label = (meta.get("agentType") or "subagent") + (": " + meta["description"] if meta.get("description") else "")
            nodes.append({"id": jid, "type": "job", "label": label[:80],
                          "state": "working" if now - mt < 90 else "idle", "seat": c["account"], "spend": 0,
                          "parent": f"session:{c['key']}", "activity": mt})
            edges.append({"from": jid, "to": f"session:{c['key']}", "kind": "spawned_by"})
    try:
        from . import work
        keys = {c["session_id"]: c["key"] for c in snap["chats"]}
        for path, a, b in work.collisions(snap):
            if a in keys and b in keys:
                edges.append({"from": f"session:{keys[a]}", "to": f"session:{keys[b]}", "kind": "collides_with", "path": path})
    except Exception:
        pass
    ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["from"] in ids and e["to"] in ids]
    return {"generated_at": now, "nodes": nodes, "edges": edges}


def decisions(state: str | None) -> dict:
    q, args = "SELECT * FROM decisions", ()
    if state:
        q, args = q + " WHERE state=?", (state,)
    out = []
    for d in db.rows(q + " ORDER BY asked_at DESC LIMIT 200", args):
        d["options"] = json.loads(d["options"] or "[]")
        out.append(d)
    return {"decisions": out}


def metrics() -> dict:
    from . import drafts
    try:
        corr = db.rows("SELECT kind, COUNT(*) AS n FROM cp_corrections GROUP BY kind")
    except Exception:
        corr = []
    live = {"drafts": drafts.precision(), "corrections_by_kind": {r["kind"]: r["n"] for r in corr},
            "auto_log": db.rows("SELECT action, decision, COUNT(*) AS n FROM auto_log GROUP BY action, decision")}
    rows = db.rows("SELECT name, at, value, detail FROM cp_metrics ORDER BY at")
    latest, trend = {}, {}
    for r in rows:
        latest[r["name"]] = {"at": r["at"], "value": r["value"], "detail": json.loads(r["detail"] or "{}")}
        trend.setdefault(r["name"], []).append([r["at"], r["value"]])
    return {"baselines": latest, "trend": trend, "live": live}


def workitems() -> dict:
    return {"work_items": db.rows("SELECT * FROM work_items ORDER BY id")}


HHMM = __import__("re").compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate_setting(key: str, body: dict) -> str | None:
    if key == "pace":
        v = body.get("pct_per_day")
        if not isinstance(v, (int, float)) or not 5 <= v <= 30:
            return "pct_per_day must be 5 to 30"
        return None
    if key != "focus":
        return "unknown setting"
    if not isinstance(body.get("on"), bool):
        return "on must be true or false"
    m = body.get("min_age_min")
    if not isinstance(m, (int, float)) or not 0 <= m <= 1440:
        return "min_age_min must be 0 to 1440"
    w = body.get("windows")
    if not isinstance(w, list) or len(w) > 12 or not all(isinstance(x, str) and HHMM.match(x) for x in w):
        return "windows must be a list of HH:MM (local time)"
    return None


MODE_DOCS = {
    "limit_resume": ("Limit resume", "When a seat's 5-hour limit resets, type the resume line into chats that stalled on it."),
    "decision_hook": ("Question hold", "Hold a chat's AskUserQuestion for the board; low risk takes the recommended option at the end of the hold."),
    "stop_gate": ("Evidence gate", "Block a coding chat from ending a turn that changed files without a named check (max 3 times)."),
    "handoff": ("Handoff", "After you confirm a chat is done, write its note into the state doc and stop the session."),
    "shadow_drafts": ("Draft replies", "Suggest replies for prose questions, paid by the chat's own seat (capped $2 a day)."),
    "idle_compact": ("Idle compaction", "After 2 keep-warm pings with nothing real in between (about 2 hours idle), stop keeping the chat warm; if its context is 150k or more, compact it first while warm so a later resume is cheap. Automatic keep-warm then leaves it cold until you type into it."),
    "corrections": ("Daily corrections", "At the first check-in time (10:00 by default), turn repeated corrections into proposed rules."),
}


def gx_get(parts: list, q1, snap: dict, now: float, ctx: dict) -> tuple[int, dict]:
    from . import fileindex, graphx
    what = parts[0] if parts else ""
    num = lambda k, d: float(q1(k)) if q1(k) not in (None, "") else d
    col = ctx.get("col") or getattr(getattr(ctx.get("sender"), "src", None), "col", None)
    if what == "files":
        return 200, graphx.files(snap, q1("prefix"), q1("session"), q1("work_item"), q1("repo"), num("window", None), now)
    if what == "hot":
        return 200, {"files": graphx.hot(num("window", 24 * 3600), now)}
    if what == "hotspots":
        return 200, graphx.hotspots(num("window", 7 * 86400) or None, q1("repo"), now)
    if what == "search":
        return 200, graphx.search(snap, q1("q") or "", ctx.get("search"))
    if what == "touches":
        return 200, {"touches": graphx.touches(num("since", now - 3600), num("until", now))}
    if what == "spend":
        return 200, graphx.spend(snap, col, int(num("days", 1)), q1("group") or "work_item")
    if what == "prs":
        return 200, {"prs": graphx.prs(snap, col)}
    if what == "outcomes":
        return 200, graphx.outcomes(snap, col, int(num("days", 7)))
    if what == "waiting":
        return 200, graphx.waiting(snap, needs_you(snap, now), (ctx.get("queue") or {}), now)
    if what == "overlaps":
        return 200, {"overlaps": graphx.overlaps(snap, num("window", 24 * 3600), now)}
    if what == "index":
        return 200, {"status": fileindex.status(), "last_scan": getattr(ctx.get("indexer"), "last", None)}
    return 404, {"error": "unknown graph view"}


def settings_view(ctx: dict | None = None) -> dict:
    out = db.settings()
    try:
        cfg = json.load(open(db.config_path()))
    except (OSError, ValueError):
        cfg = {}
    modes = []
    for k, (label, doc) in MODE_DOCS.items():
        last = db.rows("SELECT at, decision, reason, session_id FROM auto_log WHERE action=? ORDER BY at DESC LIMIT 5",
                       (k if k != "shadow_drafts" else "shadow_call",))
        modes.append({"key": k, "label": label, "doc": doc, "mode": db.mode(k), "raw": cfg.get(k), "recent": last})
    out["modes"] = modes
    try:
        out["resume_prefs"] = db.rows("SELECT session_id, key, name, pref, set_at FROM cp_resume_pref ORDER BY set_at DESC")
    except Exception:
        out["resume_prefs"] = []
    out["decision_hold_s"] = cfg.get("decision_hold_s")
    kw = (ctx or {}).get("kw")
    out["keepwarm"] = {"auto": kw.auto_on() if kw else None,
                       "chats": [dict(r, name=((ctx.get("chat_row") or (lambda k: None))(r["key"]) or {}).get("name"))
                                 for r in kw.active_rows()] if kw else [],
                       "available": bool(kw)}
    return out


def put_modes(body: dict) -> str | None:
    """Write auto-action modes into cp/config.json (read on every use, so no restart). Atomic replace."""
    if not body or not isinstance(body, dict):
        return "send {name: off | dry-run | on}"
    for k, v in body.items():
        if k not in MODE_DOCS:
            return f"unknown mode {k}"
        if v not in db.MODES:
            return f"{k}: mode must be off, dry-run or on"
    return put_config(body, log_as="mode_change")


def put_config(body: dict, log_as: str = "config_change") -> str | None:
    import os
    import tempfile
    path = db.config_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    before = {k: cfg.get(k) for k in body}
    cfg.update(body)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".config.")
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    for k, v in body.items():
        db.log_auto(log_as, "on", None, None, f"{k}: {before[k]} -> {v}", "settings page")
    return None


def _session_row(c: dict, now: float, prefs: dict | None = None) -> dict:
    if prefs is None:
        prefs = resume.prefs()
    gm = db.mode("limit_resume")
    pref = prefs.get(c["session_id"])
    can = c["kind"] == "bg" and bool(c.get("job_id")) and not c["excluded"]
    return {"resume": {"pref": pref, "global": gm, "effective": resume.effective(gm, pref) if can else "off",
                       "available": can, "why": None if can else ("read-only seat" if c["excluded"] else
                                                                  "only background chats with a job can be typed into")},"key": c["key"], "session_id": c["session_id"], "seat": c["account"], "name": c["name"],
            "state": c["state"], "live": bool(c.get("live")), "kind": c["kind"], "work_item": c.get("ws"),
            "activity": c.get("activity"), "warmth": c.get("warmth"), "cache_age_min": c.get("cache_age_min"),
            "kw": c.get("kw"), "excluded": c["excluded"], "job_id": c.get("job_id"),
            "limited": bool(c.get("banner")), "final": (c.get("final") or "")[-300:], "final_at": c.get("final_at"),
            "units_today": c.get("units_today")}


def sessions_list(snap: dict, now: float) -> list[dict]:
    pf = resume.prefs()
    rows = [_session_row(c, now, pf) for c in snap["chats"] if c["kind"] != "headless"]
    rank = {"needs_you": 0, "working": 1, "idle": 2, "stopped": 3}
    return sorted(rows, key=lambda r: (rank.get(r["state"], 4), -(r["activity"] or 0)))


NOT_YET = {"receipts": "A4", "suggestions": "A7", "sessions": "A5", "workitems_state": "A5",
           "decision_answer": "A3"}


# Every open page polls overview (5 s) and graph (10 s); several pages poll at once. Measured: graph p50 197 ms, overview+graph together up to 1.9 s, all recomputed per request from the same snapshot.
# A short cache serves repeats instantly; any POST clears it so an action shows on the next read.
CACHE_TTL = {"overview": 1.5, "graph": 3.0}
_CACHE: dict[tuple, tuple[float, dict]] = {}


def handle(method: str, path: str, query: dict, body: dict, src: "sources.Sources", ctx: dict | None = None) -> tuple[int, dict]:
    """path is the part after /api/cp/ (no leading slash). ctx: {"sender": send.Sender, "dialog": fn(chat, n, label, text="")}
    for the user's own actions; without ctx the API is read-only (actions return 501)."""
    ctx = ctx or {}
    now = time.time()
    snap = src.get()
    ck = (id(src), path, snap.get("at"))     # a new snapshot (a chat changed) is never served from the cache
    if method != "GET":
        _CACHE.clear()                        # any action may change what the board shows: next read recomputes
    elif path in CACHE_TTL and not query:
        hit = _CACHE.get(ck)
        if hit and now - hit[0] < CACHE_TTL[path]:
            return 200, hit[1]
        if len(_CACHE) > 16:
            _CACHE.clear()
    parts = [p for p in path.split("/") if p]
    head = parts[0] if parts else ""
    q1 = lambda k: (query.get(k) or [None])[0]
    if method == "GET":
        if head == "overview":
            out = overview(snap, now)
            _CACHE[ck] = (now, out)
            return 200, out
        if head == "capacity":
            return 200, capacity(snap, now)
        if head == "graph" and len(parts) == 1:
            out = graph(snap, now)
            _CACHE[ck] = (now, out)
            return 200, out
        if head == "graph" and len(parts) == 2 and parts[1] == "history":
            hours = min(48.0, float(q1("hours") or 24))
            rows = db.rows("SELECT at, frame FROM cp_graph_history WHERE at > ? ORDER BY at", (now - hours * 3600,))
            return 200, {"frames": [{"at": r["at"], "nodes": json.loads(r["frame"])} for r in rows]}
        if head == "decisions" and len(parts) == 1:
            return 200, decisions(q1("state"))
        if head == "metrics":
            return 200, metrics()
        if head == "workitems" and len(parts) == 1:
            from . import work
            return 200, {"work_items": work.summary(snap)}
        if head == "workitems" and len(parts) == 2:
            from . import work
            d = work.detail(parts[1], snap)
            return (200, d) if d else (404, {"error": "no such work item"})
        if head == "workitems" and len(parts) == 3 and parts[2] == "brief":
            from . import work
            return 200, {"brief": work.brief(parts[1]), "home_seat": work.home_seat(parts[1], snap)}
        if head == "proposals" and len(parts) == 1:
            from . import work
            return 200, {"proposals": work.proposals_open()}
        if head == "proposals" and len(parts) == 2:
            from . import work
            p = work.proposal(int(parts[1])) if parts[1].isdigit() else None
            if not p:
                return 404, {"error": "no such proposal"}
            return 200, {k: v for k, v in p.items() if k not in ("old",)}
        if head == "gx":
            return gx_get(parts[1:], q1, snap, now, dict(ctx, col=ctx.get("col") or getattr(src, "col", None)))
        if head == "settings":
            return 200, settings_view(ctx)
        if head == "sessions" and len(parts) == 1:
            return 200, {"sessions": sessions_list(snap, now)}
        if head == "sessions" and len(parts) == 3 and parts[2] == "transcript":
            from . import transcript
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1] or c["key"] == parts[1]), None)
            if not c:
                return 404, {"error": "no such session in the last few days"}
            before = q1("before")
            pg = transcript.page(c["path"], int(before) if before not in (None, "") else None, int(q1("limit") or 200))
            return 200, {"session": _session_row(c, now), **pg}
        if head == "suggestions":
            from . import shadow
            sid = q1("session_id")
            if sid:
                return 200, {"suggestion": shadow.latest_for(sid)}
            return 200, {"suggestions": db.rows("SELECT id, session_id, at, class, basis, used FROM suggestions ORDER BY at DESC LIMIT 100")}
        if head == "receipts":
            from . import receipts
            sid = q1("session_id")
            if not sid:
                return 400, {"error": "session_id required"}
            return 200, {"receipts": receipts.latest(sid, int(q1("limit") or 20))}
    if method == "POST":
        if head == "decisions" and len(parts) == 3 and parts[2] == "answer":
            if not ctx.get("dialog"):
                return 501, {"error": "read-only server"}
            from . import decisions as dec
            d = dec.get(parts[1])
            chat = next((c for c in snap["chats"] if d and c["session_id"] == d["session_id"]), None)
            if d and sources.excluded(d.get("config")):
                return 403, {"error": "this account is read-only here"}
            return dec.answer(parts[1], body, chat, ctx["dialog"])
        if head == "sessions" and len(parts) == 3 and parts[2] == "dialog":
            # Pick one of the options the chat's own dialog shows (read from its screen, never generated);
            # n = 0 is Esc. answer_dialog re-reads the screen and refuses if the label no longer matches.
            if not ctx.get("dialog"):
                return 501, {"error": "read-only server"}
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
            if not c:
                return 404, {"error": "no such session in the last 24 h"}
            if c["excluded"]:
                return 403, {"error": "this account is read-only here"}
            try:
                n = int(body.get("n"))
            except (TypeError, ValueError):
                return 400, {"error": "n required"}
            res = ctx["dialog"](c, n, body.get("label") or "", body.get("text") or "")
            db.log_auto("dialog", "manual", c["session_id"], c["account"], "done" if res.get("ok") else "failed",
                        res.get("error") or "", {"result": res})
            return (200 if res.get("ok") else 409), res
        if head == "sessions" and len(parts) == 3 and parts[2] in ("stop", "terminal"):
            fn = ctx.get(parts[2])
            if not fn:
                return 501, {"error": "read-only server"}
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
            if not c:
                return 404, {"error": "no such session in the last 24 h"}
            if c["excluded"]:
                return 403, {"error": "this account is read-only here"}
            res = fn(c)
            db.log_auto(parts[2], "manual", c["session_id"], c["account"], "done" if res.get("ok") else "failed",
                        res.get("error") or "", {"result": res})
            return (200 if res.get("ok") else 409), res
        if head == "needs" and len(parts) == 2 and parts[1] == "dismiss":
            iid, since = body.get("id") or "", body.get("since")
            if iid.startswith("decision:"):
                n = db.execute("UPDATE decisions SET state='dismissed', answered_at=?, answered_by='user', delivery='dismissed on the board'"
                               " WHERE id=? AND state='open'", (now, iid.split(":", 1)[1]))
                return (200, {"ok": True}) if n else (409, {"error": "already closed"})
            if not iid.startswith(("blocked:", "limit:", "dialog:")):
                return 400, {"error": "unknown item"}
            dismiss(iid, since, body.get("title") or "")
            return 200, {"ok": True}
        if head == "sessions" and len(parts) == 3 and parts[2] == "resume_pref":
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
            if not c:
                return 404, {"error": "no such chat"}
            if c["excluded"]:
                return 403, {"error": "this account is read-only here"}
            pref = body.get("pref")
            if pref not in ("on", "off", "default"):
                return 400, {"error": "pref must be on, off or default"}
            resume.set_pref(c, None if pref == "default" else pref)
            return 200, {"ok": True, "session": _session_row(c, now)}
        if head == "sessions" and len(parts) == 3 and parts[2] == "keepwarm":
            kw, row = ctx.get("kw"), (ctx.get("chat_row") or (lambda k: None))
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
            chat = row(c["key"]) if c else None
            if not kw or not chat:
                return (501, {"error": "keep-warm runs only in the main server"}) if not kw else (404, {"error": "no such chat"})
            if c["excluded"]:
                return 403, {"error": "this account is read-only here"}
            act = body.get("action")
            if act == "on":
                hours = float(body.get("hours") or 12)
                if not 0.5 <= hours <= 48:
                    return 400, {"error": "hours must be 0.5 to 48"}
                return 200, kw.enable(chat, hours)
            if act == "off":
                kw.disable(chat["key"])
                return 200, {"ok": True}
            if act == "now":
                from ..keepwarm import PING_TEXT
                st = next((x for x in snap.get("seats") or [] if x.get("config") == c["config"]), {})
                f5, f7 = (st.get("five_hour") or {}).get("pct") or 0, (st.get("seven_day") or {}).get("pct") or 0
                if f5 >= 100 or f7 >= 100:
                    return 409, {"error": f"{c['account']} is at its {'5h' if f5 >= 100 else '7d'} limit: a ping cannot run, so it would warm nothing"}
                return 200, ctx["reply"](chat, PING_TEXT)
            return 400, {"error": "action must be on, off or now"}
        if head == "sessions" and len(parts) == 3 and parts[2] == "reply":
            if not ctx.get("sender"):
                return 501, {"error": "read-only server"}
            return ctx["sender"].reply(parts[1], body.get("text", ""), body.get("key"))
        if head == "sessions" and len(parts) == 1:
            from . import work
            if not ctx.get("sender"):
                return 501, {"error": "read-only server"}
            return work.spawn(body, snap)
        if head == "sessions" and len(parts) == 3 and parts[2] == "done":
            from . import work
            c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
            if not c:
                return 404, {"error": "no such session in the last 24 h"}
            if c["excluded"]:
                return 403, {"error": "this account is read-only here"}
            if not c.get("ws"):
                return 409, {"error": "this chat has no work item, so there is no state doc to hand off to; stop it instead"}
            p = work.propose_handoff(c, "You marked it done")
            return (200, {"ok": True, "proposal": p["id"]}) if p else (409, {"error": "a handoff is already proposed or done for this chat"})
        if head == "intent":
            from . import intent
            text = (body.get("text") or "").strip()
            if not text or len(text) > 500:
                return 400, {"error": "text required (max 500 chars)"}
            ictx, rev = intent.context_from(snap, db.rows("SELECT id, title FROM work_items"))
            r = intent.route(text, ictx, use_model=bool(body.get("model", True)))
            for k in ("chat", "target", "work_item"):
                if r.get(k) in rev:
                    r[k + "_ref"] = rev[r[k]]
            return 200, r
        if head == "suggestions" and len(parts) == 3 and parts[2] == "used":
            if not parts[1].isdigit():
                return 404, {"error": "no such suggestion"}
            n = db.execute("UPDATE suggestions SET used=2 WHERE id=?", (int(parts[1]),))
            return (200, {"ok": True}) if n else (404, {"error": "no such suggestion"})
        if head == "proposals" and len(parts) == 3 and parts[2] in ("apply", "skip"):
            from . import work
            if not parts[1].isdigit():
                return 404, {"error": "no such proposal"}
            return work.decide(int(parts[1]), parts[2], snap, ctx.get("reply"), ctx.get("stop"))
        if head == "workitems" and len(parts) == 3 and parts[2] == "regenerate":
            from . import work
            p = work.propose_state(parts[1], snap, force=True)
            return 200, {"ok": True, "proposal": p["id"] if p else None, "note": None if p else "nothing changed"}
    if method == "PUT" and head == "settings" and len(parts) == 2 and parts[1] == "modes":
        err = put_modes(body)
        return (400, {"error": err}) if err else (200, settings_view(ctx))
    if method == "PUT" and head == "settings" and len(parts) == 2 and parts[1] == "keepwarm_auto":
        if not ctx.get("kw"):
            return 501, {"error": "keep-warm runs only in the main server"}
        if not isinstance(body.get("on"), bool):
            return 400, {"error": "on must be true or false"}
        ctx["kw"].set_auto(body["on"])
        db.log_auto("keepwarm_auto", "on", None, None, "set " + ("on" if body["on"] else "off"), "settings page")
        return 200, settings_view(ctx)
    if method == "PUT" and head == "settings" and len(parts) == 2:
        err = validate_setting(parts[1], body)
        if err:
            return 400, {"error": err}
        db.put_setting(parts[1], body)
        return 200, db.settings()
    if method == "PUT" and head == "gate" and len(parts) == 2:
        if not isinstance(body.get("on"), bool):
            return 400, {"error": "on must be true or false"}
        c = next((c for c in snap["chats"] if c["session_id"] == parts[1]), None)
        if c and c["excluded"]:
            return 403, {"error": "this account is read-only here"}
        from .hooks import stop_hook
        conn = db.connect()
        conn.executescript(stop_hook.GATE_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO cp_gate(session_id, on_, set_by, at) VALUES(?,?,?,?)",
                     (parts[1], 1 if body["on"] else 0, "user", now))
        conn.commit()
        conn.close()
        return 200, {"ok": True, "session_id": parts[1], "on": body["on"]}
    if method == "PUT" and head == "workitems" and len(parts) == 3 and parts[2] == "state":
        from . import work
        if not db.rows("SELECT id FROM work_items WHERE id=?", (parts[1],)):
            return 404, {"error": "no such work item"}
        text = body.get("text")
        if not isinstance(text, str) or len(text) > 200_000:
            return 400, {"error": "text required (max 200k chars)"}
        work.write_state(parts[1], text, "edited by the user on the board")
        return 200, {"ok": True}
    return 404, {"error": "not found"}
