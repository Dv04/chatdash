"""Provider chats as ordinary board rows, so Codex and Cursor chats sit in the same lists, graph and chat view as Claude Code chats.

A row has every field a Claude Code row has (the rest of DHI Orbit reads them all), with the Claude-only ones neutral: no job, no
process, no prompt cache, no token units. `provider` says whose chat it is. `path` is a pseudo path ("codex:<id>") that the
transcript and turn readers recognise; nothing ever opens it as a file."""
from __future__ import annotations

import time

PREFIX = "provider:"            # the pseudo config dir of a provider's rows: "provider:codex"


def iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z" if t else None


def is_path(path: str | None) -> bool:
    return bool(path) and path.split(":", 1)[0] in ("codex", "cursor") and ":" in path and "/" not in path.split(":", 1)[0]


def split_path(path: str) -> tuple[str, str]:
    prov, _, cid = path.partition(":")
    return prov, cid


def row(c: dict, label: str, work_item_of=lambda n: None) -> dict:
    now = time.time()
    upd = c["updated_at"]
    return {
        "key": c["key"], "account": c["provider"], "config": PREFIX + c["provider"], "session_id": c["id"],
        "job_id": None, "pid": None, "live": False, "kind": "interactive", "state": c["state"], "name": c["name"],
        "waiting_for": None, "warmth": None, "cache_age_min": None, "cools_in_min": 0,
        "cwd": c.get("cwd"), "path": f"{c['provider']}:{c['id']}", "model": c.get("model"), "ttl": None,
        "last_prompt": c.get("last_prompt") or "", "last_prompt_at": iso(c.get("created_at")),
        "final": c.get("final") or "", "final_at": iso(c.get("final_at")),
        "turns": 0, "ctx_tokens": 0, "idle_min": round(max(0.0, now - upd) / 60.0, 1),
        "units_today": 0, "cold_today": 0, "units_7d": 0, "ws": work_item_of(c["name"]),
        "pending_tool": None, "prs": [], "activity": upd, "version": int(upd * 1000),
        "provider": c["provider"], "provider_label": label, "error": c.get("error"),
    }


NEAR_PCT = 80           # the same line Claude seats use (cp/sources.NEAR_PCT; a test keeps them equal)


def _window(pct, resets_at, now: float) -> dict:
    """One limit window, as cp/sources.window: a reading whose reset time has passed no longer says anything about now."""
    if pct is None:
        return {"pct": None, "resets_at": resets_at, "since_reset": False}
    if resets_at and resets_at <= now:
        return {"pct": None, "resets_at": None, "since_reset": True}
    return {"pct": round(pct), "resets_at": resets_at, "since_reset": False}


def _state(five: dict, seven: dict) -> str:
    known = [w["pct"] for w in (five, seven) if w["pct"] is not None]
    if any(p >= 100 for p in known):
        return "blocked"
    if not known and not (five["since_reset"] or seven["since_reset"]):
        return "unknown"
    if seven["pct"] is None and not seven["since_reset"]:
        return "unknown"
    if any(p >= NEAR_PCT for p in known):
        return "near"
    return "ok"


def seat(pid: str, label: str, chats: list[dict], lim: dict | None = None, now: float | None = None) -> dict:
    """The pseudo-seat a provider appears as in the capacity list, the sky and the graph. `lim` is the provider's own
    usage reading ({five, five_resets, seven, seven_resets, at}); without one the limits read as unknown, never as 0."""
    now = now or time.time()
    lim = lim or {}
    five = _window(lim.get("five"), lim.get("five_resets"), now)
    seven = _window(lim.get("seven"), lim.get("seven_resets"), now)
    at = lim.get("at")
    state = _state(five, seven)
    resume_at = None
    if state == "blocked":
        cands = [w["resets_at"] for w in (five, seven) if w["pct"] is not None and w["pct"] >= 100 and w["resets_at"]]
        resume_at = max(cands) if cands else None
    return {"config": PREFIX + pid, "seat": pid, "label": label, "excluded": False,
            "five_hour": five, "seven_day": seven,
            "meter_at": at, "meter_age_min": round((now - at) / 60) if at else None,
            "usage_meter": "on" if lim else "off", "state": state,
            "resume_at": resume_at, "resume_at_ct": None, "queued": [], "provider": pid}
