"""Graph extensions: files on demand, hot files, hotspots, search, spend flow, PR layer,
waiting chain, overlap detector, cost per outcome, repo grouping and replay file events.

Every view is computed from data already on disk: cp_touch / cp_touch_ev / cp_prlink (fileindex), limit units
per chat per day (the collector's cost_days), PR state from `gh` (the collector's 15-minute cache, fetched in
the background, so a PR shows "unknown" until it is read), receipts, and the live snapshot. Nothing here acts.
"""
from __future__ import annotations

import os
import posixpath
import time

from .. import _plat
from . import db, fileindex, resume

HOUR = 3600


def _meta(snap: dict) -> dict[str, dict]:
    out = {}
    for c in snap["chats"]:
        out[c["session_id"]] = {"session_id": c["session_id"], "key": c["key"], "name": c["name"], "seat": c["account"],
                                "work_item": c.get("ws"), "state": c["state"], "live": bool(c.get("live")),
                                "excluded": c["excluded"], "repo": repo_for(c),
                                "path": c.get("path"), "activity": c.get("activity")}
    return out


def repo_for(c: dict) -> str | None:
    """The repo a chat works in: its cwd with any worktree folded back into the main checkout."""
    cwd = c.get("cwd")
    if not cwd:
        return None
    cwd = fileindex.WT.sub("/", _plat.to_posix(cwd).rstrip("/") + "/").rstrip("/")
    return fileindex.repo_of(posixpath.join(cwd, "x"))


def _short(p: str) -> str:
    return p.replace(fileindex.HOME + "/", "~/")


def _sessions_of(snap: dict, session: str | None, work_item: str | None, repo: str | None) -> list[str] | None:
    repo = _plat.to_posix(os.path.expanduser(repo)) if repo else repo
    if session:
        return [session]
    if work_item:
        return [c["session_id"] for c in snap["chats"] if c.get("ws") == work_item]
    if repo:
        return [c["session_id"] for c in snap["chats"] if repo_for(c) == repo]
    return None


# ------------------------------------------------------------------ files, one level at a time
def files(snap: dict, prefix: str | None = None, session: str | None = None, work_item: str | None = None,
          repo: str | None = None, window: float | None = None, now: float | None = None) -> dict:
    fileindex.init()
    now = now or time.time()
    sids = _sessions_of(snap, session, work_item, repo)
    q = "SELECT norm, repo, session_id, kind, n, last_at FROM cp_touch WHERE 1=1"
    args: list = []
    if sids is not None:
        if not sids:
            return {"prefix": prefix, "items": []}
        q += f" AND session_id IN ({','.join('?' * len(sids))})"
        args += sids
    if prefix:
        prefix = _plat.to_posix(os.path.expanduser(prefix))
        q += " AND norm LIKE ?"
        args.append(prefix.rstrip("/") + "/%")
    if window:
        q += " AND last_at >= ?"
        args.append(now - window)
    rows = db.rows(q, tuple(args))
    groups: dict[str, dict] = {}
    for r in rows:
        if prefix:
            rest = r["norm"][len(prefix.rstrip("/")) + 1:]
            head, _, tail = rest.partition("/")
            key, is_dir = prefix.rstrip("/") + "/" + head, bool(tail)
        else:
            key, is_dir = r["repo"], True
        g = groups.setdefault(key, {"path": key, "label": _short(key) if not prefix else os.path.basename(key), "dir": is_dir,
                                    "files": set(), "w": 0, "r": 0, "sessions": set(), "last_at": 0})
        g["files"].add(r["norm"])
        g["w" if r["kind"] == "w" else "r"] += r["n"]
        g["sessions"].add(r["session_id"])
        g["last_at"] = max(g["last_at"], r["last_at"] or 0)
    items = sorted(groups.values(), key=lambda g: (-g["w"], -len(g["files"])))
    for g in items:
        g["files"], g["sessions"] = len(g["files"]), sorted(g["sessions"])
    return {"prefix": prefix, "items": items[:300], "total": len(items)}


def hot(window: float = 24 * HOUR, now: float | None = None, limit: int = 60) -> list[dict]:
    """Files written by 2+ sessions inside the window."""
    fileindex.init()
    now = now or time.time()
    rows = db.rows("SELECT norm, repo, GROUP_CONCAT(session_id) AS sids, COUNT(DISTINCT session_id) AS ns, SUM(n) AS edits,"
                   " MAX(last_at) AS last_at FROM cp_touch WHERE kind='w' AND last_at >= ? GROUP BY norm HAVING ns >= 2"
                   " ORDER BY ns DESC, last_at DESC LIMIT ?", (now - window, limit))
    return [{"path": r["norm"], "label": os.path.basename(r["norm"]), "repo": r["repo"], "sessions": r["sids"].split(","),
             "n_sessions": r["ns"], "edits": r["edits"], "last_at": r["last_at"]} for r in rows]


def hotspots(window: float | None = 7 * 24 * HOUR, repo: str | None = None, now: float | None = None, limit: int = 2500) -> dict:
    fileindex.init()
    now = now or time.time()
    q = ("SELECT norm, repo, SUM(n) AS edits, COUNT(DISTINCT session_id) AS ns, MAX(last_at) AS last_at FROM cp_touch"
         " WHERE kind='w'")
    args: list = []
    if window:
        q += " AND last_at >= ?"
        args.append(now - window)
    if repo:
        q += " AND repo = ?"
        args.append(repo)
    q += " GROUP BY norm ORDER BY edits DESC LIMIT ?"
    args.append(limit)
    rows = db.rows(q, tuple(args))
    return {"files": [{"path": r["norm"], "repo": r["repo"], "edits": r["edits"], "sessions": r["ns"], "last_at": r["last_at"]} for r in rows],
            "home": fileindex.HOME}


def search(snap: dict, q: str, search_fn=None, limit: int = 40) -> dict:
    fileindex.init()
    q = (q or "").strip()
    if not q:
        return {"files": [], "sessions": [], "prs": []}
    meta = _meta(snap)
    frows = db.rows("SELECT norm, GROUP_CONCAT(DISTINCT session_id) AS sids, SUM(n) AS n, MAX(last_at) AS last_at,"
                    " GROUP_CONCAT(DISTINCT kind) AS kinds FROM cp_touch WHERE norm LIKE ? GROUP BY norm ORDER BY n DESC LIMIT ?",
                    (f"%{q}%", limit))
    files_ = [{"path": r["norm"], "label": _short(r["norm"]), "sessions": r["sids"].split(","), "edits": r["n"],
               "kinds": r["kinds"], "last_at": r["last_at"]} for r in frows]
    hits = []
    if search_fn:
        try:
            hits = search_fn(q)
        except Exception:
            hits = []
    by_key = {m["key"]: m for m in meta.values()}
    sess = {}
    for hgt in hits:
        m = by_key.get(hgt["key"])
        sid = m["session_id"] if m else hgt["key"].split(":", 1)[-1]
        sess.setdefault(sid, {"session_id": sid, "key": hgt["key"], "name": hgt["name"], "snippet": hgt.get("final") or hgt.get("prompt"), "ts": hgt.get("ts")})
    prs = []
    num = q.lstrip("#")
    if num.isdigit():
        prs = db.rows("SELECT session_id, url, repo, num FROM cp_prlink WHERE num=? ORDER BY at DESC LIMIT 40", (int(num),))
    elif "/pull/" in q:
        prs = db.rows("SELECT session_id, url, repo, num FROM cp_prlink WHERE url LIKE ? LIMIT 40", (f"%{q}%",))
    matched = set(sess)
    for f in files_:
        matched.update(f["sessions"])
    matched.update(p["session_id"] for p in prs)
    return {"files": files_, "sessions": list(sess.values()), "prs": prs, "matched_sessions": sorted(matched)}


def touches(since: float, until: float, limit: int = 3000) -> list[dict]:
    fileindex.init()
    return db.rows("SELECT norm, session_id, at FROM cp_touch_ev WHERE at >= ? AND at < ? ORDER BY at LIMIT ?", (since, until, limit))


# ------------------------------------------------------------------ spend and outcomes
def _units(col, days: int) -> dict[str, float]:
    """path -> limit units over the last `days` UTC days (1 = today)."""
    out = {}
    if not col:
        return out
    try:
        for r in col.cost_days(max(1, days)):
            vals = list(r["by_day"].values())[-days:]
            out[r["path"]] = float(sum(vals))
    except Exception:
        pass
    return out


def spend(snap: dict, col, days: int = 1, group: str = "work_item", top: int = 24) -> dict:
    units = _units(col, days)
    rows = []
    for c in snap["chats"]:
        u = units.get(c.get("path"))
        if not u:
            continue
        g = c.get("ws") if group == "work_item" else (repo_for(c) and _short(repo_for(c)))
        rows.append({"session_id": c["session_id"], "key": c["key"], "name": c["name"], "seat": c["account"],
                     "group": g or ("no work item" if group == "work_item" else "unknown repo"), "units": u})
    rows.sort(key=lambda r: -r["units"])
    head, tail = rows[:top], rows[top:]
    if tail:
        for seat in sorted({r["seat"] for r in tail}):
            t = [r for r in tail if r["seat"] == seat]
            head.append({"session_id": None, "key": None, "name": f"{len(t)} other chats", "seat": seat, "group": "other",
                         "units": sum(r["units"] for r in t)})
    total = sum(r["units"] for r in rows)
    return {"days": days, "group": group, "total": total, "rows": head,
            "seats": [{"seat": s["seat"], "five": s["five_hour"].get("pct"), "seven": s["seven_day"].get("pct")} for s in snap.get("seats") or []]}


def prs(snap: dict, col, sessions: list[str] | None = None) -> list[dict]:
    fileindex.init()
    sids = sessions or [c["session_id"] for c in snap["chats"]]
    if not sids:
        return []
    rows = db.rows(f"SELECT session_id, url, repo, num FROM cp_prlink WHERE session_id IN ({','.join('?' * len(sids))})", tuple(sids))
    states = {}
    if col:
        try:
            states = col.pr_states(sorted({r["url"] for r in rows}))
        except Exception:
            states = {}
    # "missing" = gh says the PR does not exist (a placeholder or example link in a chat): not a node, not a row
    return [dict(r, state=states.get(r["url"]) or "unknown") for r in rows if states.get(r["url"]) != "missing"]


def outcomes(snap: dict, col, days: int = 7) -> dict:
    units = _units(col, days)
    meta = _meta(snap)
    pr = prs(snap, col)
    merged = {}
    for p in pr:
        if p["state"] == "merged":
            merged.setdefault(p["session_id"], set()).add(p["url"])
    since = time.time() - days * 86400
    ver = {r["session_id"]: r["n"] for r in db.rows(
        "SELECT session_id, COUNT(*) AS n FROM receipts WHERE verified=1 AND at >= ? GROUP BY session_id", (since,))}
    by_group: dict[tuple, dict] = {}
    for c in snap["chats"]:
        u = units.get(c.get("path")) or 0
        m = meta[c["session_id"]]
        for dim, key in (("work_item", m["work_item"] or "no work item"), ("seat", m["seat"]),
                         ("repo", _short(m["repo"]) if m["repo"] else "unknown repo")):
            g = by_group.setdefault((dim, key), {"dim": dim, "key": key, "units": 0.0, "merged": 0, "verified": 0, "sessions": 0})
            g["units"] += u
            g["merged"] += len(merged.get(c["session_id"], ()))
            g["verified"] += ver.get(c["session_id"], 0)
            g["sessions"] += 1 if u else 0
    out = []
    for g in by_group.values():
        if not g["units"] and not g["merged"]:
            continue
        g["per_merged"] = round(g["units"] / g["merged"]) if g["merged"] else None
        g["per_verified"] = round(g["units"] / g["verified"]) if g["verified"] else None
        g["units"] = round(g["units"])
        out.append(g)
    out.sort(key=lambda g: -g["units"])
    unknown = sum(1 for p in pr if p["state"] == "unknown")
    return {"days": days, "groups": out, "pr_states_pending": unknown,
            "note": "units = limit units from transcripts; merged = PRs linked in the chat that gh reports merged; "
                    "verified = turns whose receipt passed the evidence rule (receipts exist from 2026-10-02 on)"}


# ------------------------------------------------------------------ waiting chain and overlaps
def waiting(snap: dict, needs: list[dict], queue: dict | None, now: float) -> dict:
    meta = _meta(snap)
    chains = []
    dev = [{"session_id": x["session_id"], "title": x["title"], "kind": x["kind"], "seconds": x.get("seconds"),
            "seat": x.get("seat"), "running": x.get("running")} for x in needs]
    dev.sort(key=lambda x: -(x["seconds"] or 0))
    if dev:
        chains.append({"waits_on": "you", "label": "Waiting on your answer", "items": dev})
    for s in snap.get("seats") or []:
        # the board's rule (api.needs_you): a chat whose process is gone and whose reset passed over MAX_STALL_S ago is
        # not waiting on anything any more; it used to sit here for days as "reset passed"
        stalled = [c for c in snap["chats"] if c["config"] == s["config"] and c.get("banner")
                   and (c.get("live") or now - c["banner"]["resets_at"] <= resume.MAX_STALL_S)]
        if not stalled:
            continue
        b = max(c["banner"]["resets_at"] for c in stalled)
        items = [{"session_id": c["session_id"], "title": c["name"], "kind": "limit", "seat": c["account"],
                  "seconds": max(0, int(now - c["banner"]["shown_at"])) if c["banner"].get("shown_at") else None,
                  "resets_at": c["banner"]["resets_at"], "passed": c["banner"]["resets_at"] <= now} for c in stalled]
        chains.append({"waits_on": f"seat:{s['seat']}", "label": f"Waiting on {s['seat']}'s limit reset", "resets_at": b, "items": items})
    if queue:
        items = []
        for sid, q in queue.items():
            m = meta.get(sid) or {}
            items.append({"session_id": sid, "title": m.get("name") or sid[:8], "kind": "queued reply",
                          "seconds": int(now - q.get("at", now)), "text": q.get("text", "")[:200]})
        if items:
            chains.append({"waits_on": "turn", "label": "Replies queued until the chat finishes its turn", "items": items})
    longest = max((i for ch in chains for i in ch["items"] if i.get("seconds") is not None), key=lambda i: i["seconds"], default=None)
    return {"chains": chains, "longest": longest}


def overlaps(snap: dict, window: float = 24 * HOUR, now: float | None = None) -> list[dict]:
    fileindex.init()
    now = now or time.time()
    meta = _meta(snap)
    rows = db.rows("SELECT norm, session_id, last_at FROM cp_touch WHERE kind='w' AND last_at >= ?", (now - window,))
    by_file: dict[str, dict] = {}
    for r in rows:
        by_file.setdefault(r["norm"], {})[r["session_id"]] = r["last_at"]
    pairs: dict[tuple, dict] = {}
    for f, ss in by_file.items():
        if len(ss) < 2:
            continue
        sids = sorted(ss)
        for i in range(len(sids)):
            for j in range(i + 1, len(sids)):
                p = pairs.setdefault((sids[i], sids[j]), {"files": [], "last_at": 0})
                p["files"].append(f)
                p["last_at"] = max(p["last_at"], ss[sids[i]], ss[sids[j]])
    out = []
    for (a, b), p in pairs.items():
        ma, mb = meta.get(a), meta.get(b)
        if not ma or not mb:
            continue                               # only chats still in view
        both_live = ma["live"] and mb["live"]
        out.append({"a": ma, "b": mb, "shared": len(p["files"]), "files": [_short(f) for f in p["files"][:12]],
                    "last_at": p["last_at"], "both_running": both_live,
                    "risk": "high" if both_live and (ma["state"] == "working" or mb["state"] == "working") else
                            "med" if both_live else "low"})
    rank = {"high": 0, "med": 1, "low": 2}
    out.sort(key=lambda o: (rank[o["risk"]], -o["shared"], -o["last_at"]))
    return out
