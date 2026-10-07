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


def seat(pid: str, label: str, chats: list[dict]) -> dict:
    """The pseudo-seat a provider appears as in the capacity list and the graph: no usage meter, so limits read as unknown."""
    return {"config": PREFIX + pid, "seat": pid, "label": label, "excluded": False,
            "five_hour": {"pct": None, "resets_at": None, "since_reset": None},
            "seven_day": {"pct": None, "resets_at": None, "since_reset": None},
            "meter_at": None, "meter_age_min": None, "usage_meter": "off", "state": "unknown",
            "resume_at": None, "resume_at_ct": None, "queued": [], "provider": pid}
