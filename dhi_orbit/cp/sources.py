"""Read model for the control plane: seats, sessions, jobs, banners. Files only, zero tokens.

Sources and what is trusted:
  meter log (your status line, see config)    5h / 7d per seat. No live usage endpoint here:
                                              missing or stale data is "unknown", never green.
  $CFG/jobs/<id>/state.json                   job state incl. "blocked" + needs + suggestedReply
                                              (Claude Code's own summary of what the chat waits on)
  collector.Collector().snapshot()            live chat rows (the same reader DHI Orbit uses)
  transcript tail                             last assistant record: is it a limit banner?
Accounts listed in config read_only_accounts (none by default) are shown but marked excluded:
nothing in cp acts on them.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time

from .. import _plat, collector, config, usage_meter
from ..extract import iso_epoch

from . import limits

NEAR_PCT = 80
STALL_SHOW_S = 12 * 3600      # a limit banner whose reset passed longer ago than this is not a stalled chat (same cutoff as resume.MAX_STALL_S)


def excluded(cfg: str | None) -> bool:
    """True for a config dir cp must not act on: none given, or its account is in read_only_accounts."""
    return not cfg or config.is_read_only(cfg=cfg)


# ------------------------------------------------------------------ meter
class Meter:
    """Incremental reader of meter.log: last reading per config dir."""

    def __init__(self, path: str | None = None):
        self.path, self.off, self.last = path, 0, {}      # path None: config.meter_log_path() at each read
        self.ok = False

    def read(self) -> dict[str, dict]:
        path = self.path or config.meter_log_path()
        try:
            st = os.stat(path)
        except OSError:
            self.ok = False
            return self.last
        self.ok = True
        if st.st_size < self.off:
            self.off, self.last = 0, {}
        if st.st_size > self.off:
            with open(path, "rb") as fh:
                fh.seek(self.off)
                data = fh.read()
            cut = data.rfind(b"\n") + 1          # leave a partial last line for next time
            self.off += cut
            for raw in data[:cut].decode(errors="replace").splitlines():
                parts = raw.split("\t")
                cfg = next((p for p in parts if _plat.is_abs_path(p)), None)
                if not cfg or not parts[-1].startswith("{"):
                    continue
                try:
                    j, at = json.loads(parts[-1]), iso_epoch(parts[0])
                except ValueError:
                    continue
                f5, s7 = j.get("five_hour") or {}, j.get("seven_day") or {}
                key = os.path.normpath(cfg)
                prev = self.last.get(key) or {}
                # A status line can omit a window (seen: lines with only seven_day after the 5h
                # reset). Keep the last reading of that window; window() then reports "since reset".
                self.last[key] = {
                    "at": at,
                    "five": f5.get("used_percentage") if f5 else prev.get("five"),
                    "five_resets": f5.get("resets_at") if f5 else prev.get("five_resets"),
                    "seven": s7.get("used_percentage") if s7 else prev.get("seven"),
                    "seven_resets": s7.get("resets_at") if s7 else prev.get("seven_resets"),
                    "five_omitted": not f5, "seven_omitted": not s7}
        return self.last


def window(pct, resets_at, now: float) -> dict:
    """One limit window. A reading whose reset time has passed no longer says anything about now:
    pct is None and since_reset is True (the meter writes on every status-line change, so no newer
    line means the seat has not been used since; still reported as unknown, not as 0)."""
    if pct is None:
        return {"pct": None, "resets_at": resets_at, "since_reset": False}
    if resets_at and resets_at <= now:
        return {"pct": None, "resets_at": None, "since_reset": True}
    return {"pct": pct, "resets_at": resets_at, "since_reset": False}


def seat_state(five: dict, seven: dict, banner_until: float | None, now: float) -> str:
    if banner_until and banner_until > now:
        return "blocked"
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


# ------------------------------------------------------------------ jobs and banners
def blocked_since(timeline: str, tail: int = 65536) -> float | None:
    """When the job's current blocked run began: the earliest of the trailing "blocked" rows of its timeline.
    Claude Code rewrites a blocked job (detail, updatedAt) after you answer it, so updatedAt is not the ask time."""
    try:
        size = os.path.getsize(timeline)
        with open(timeline, "rb") as fh:
            fh.seek(max(0, size - tail))
            rows = fh.read().split(b"\n")
    except OSError:
        return None
    start = None
    for raw in reversed(rows):
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("state") != "blocked":
            break
        start = iso_epoch(r.get("at")) or start
    return start


def jobs_all(since: float) -> list[dict]:
    out = []
    for cfg in collector.config_dirs():
        for f in glob.glob(os.path.join(cfg, "jobs", "*", "state.json")):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            upd = iso_epoch(d.get("updatedAt")) or os.path.getmtime(f)
            if d.get("state") in ("done", "stopped", "failed") and upd < since:
                continue
            out.append({"id": os.path.basename(os.path.dirname(f)), "config": cfg,
                        "seat": collector.account_name(cfg), "state": d.get("state"),
                        "tempo": d.get("tempo"), "detail": d.get("detail"), "needs": d.get("needs"),
                        "suggested": d.get("suggestedReply"), "name": d.get("name"),
                        "session_id": (os.path.basename(d["linkScanPath"])[:-6] if d.get("linkScanPath")
                                       else d.get("sessionId")),
                        "updated": upd, "cwd": d.get("cwd"),
                        "blocked_since": blocked_since(os.path.join(os.path.dirname(f), "timeline.jsonl"))
                        if d.get("state") == "blocked" else None,
                        "banner": limits.parse_banner(d.get("needs") or d.get("detail") or "", upd)})
    return out


def last_assistant(path: str, tail: int = 262144) -> dict | None:
    """The last top-level assistant record of a transcript (read from the tail only)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail))
            data = fh.read()
    except OSError:
        return None
    last_a, last_u = None, None
    for raw in data.split(b"\n"):
        if b'"type":"assistant"' not in raw and b'"type":"user"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("isSidechain"):
            continue
        if r.get("type") == "assistant":
            last_a = r
        elif r.get("type") == "user" and not r.get("isMeta"):
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str) or (isinstance(c, list) and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c)):
                last_u = r
    if not last_a:
        return None
    a_at = iso_epoch(last_a.get("timestamp")) or 0
    u_at = iso_epoch((last_u or {}).get("timestamp")) or 0
    text = " ".join(b.get("text", "") for b in (last_a.get("message") or {}).get("content") or []
                    if isinstance(b, dict) and b.get("type") == "text")
    return {"at": a_at, "text": text, "api_error": bool(last_a.get("isApiErrorMessage")),
            "user_after": u_at > a_at}


def banner_of(path: str) -> dict | None:
    """The limit banner if it is the chat's last message (nothing typed after it), else None."""
    la = last_assistant(path)
    if not la or not la["api_error"] or la["user_after"]:
        return None
    b = limits.parse_banner(la["text"], la["at"])
    if b:
        b["shown_at"] = la["at"]
    return b


# ------------------------------------------------------------------ the snapshot
class Sources:
    """One refresh builds everything the API serves; reads are from the last snapshot."""

    def __init__(self, window_s: int = 24 * 3600, col=None, meter=None, fleet=None):
        self.col = col or collector.Collector(window_s)
        self.meter = meter or Meter()
        self.fleet = fleet
        self.window_s = window_s
        self.lock = threading.Lock()
        self.snap: dict = {"at": None, "chats": [], "jobs": [], "seats": [], "errors": {}}
        self._banner_cache: dict[str, tuple] = {}

    def _banner(self, path: str, version) -> dict | None:
        hit = self._banner_cache.get(path)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if hit and hit[0] == mt:
            return hit[1]
        b = banner_of(path)
        self._banner_cache[path] = (mt, b)
        return b

    def refresh(self, chats: list | None = None) -> dict:
        """chats: rows already built by the live server (its collector), so transcripts are parsed once."""
        now, errors = time.time(), {}
        if chats is None:
            try:
                chats = self.col.snapshot()
            except Exception as e:
                chats, errors["sessions"] = [], f"{type(e).__name__}: {e}"
        else:
            chats = [dict(c) for c in chats]
        for c in chats:
            c["banner"] = self._banner(c["path"], c.get("version")) if c["kind"] != "headless" else None
            c["excluded"] = excluded(c["config"])
        try:
            jobs = jobs_all(now - self.window_s)
        except Exception as e:
            jobs, errors["jobs"] = [], f"{type(e).__name__}: {e}"
        try:
            meter = self.meter.read()
            if not self.meter.ok:
                on = [collector.account_name(c) for c in collector.config_dirs() if usage_meter.meter_state(c) == "on"]
                errors["meter"] = (f"no usage reading yet: waiting for the first chat on {', '.join(on)}" if on else
                                   "usage not connected: turn it on per account in Settings > Accounts")
        except Exception as e:
            meter, errors["meter"] = {}, f"{type(e).__name__}: {e}"
        fleet = []
        if self.fleet is not None:
            try:
                fleet = self.fleet()
            except Exception as e:
                errors["fleet"] = f"{type(e).__name__}: {e}"
        seats = []
        for cfg in collector.config_dirs():
            seat = collector.account_name(cfg)
            m = meter.get(os.path.normpath(cfg)) or {}
            five = window(m.get("five"), m.get("five_resets"), now)
            seven = window(m.get("seven"), m.get("seven_resets"), now)
            stalled = [c for c in chats if c["config"] == cfg and c.get("banner") and c["banner"]["resets_at"] > now - STALL_SHOW_S]
            seen = {c["session_id"] for c in stalled}
            stalled += [{"session_id": j["session_id"], "name": j["name"], "banner": j["banner"]}
                        for j in jobs if j["config"] == cfg and j.get("banner") and j["state"] == "blocked"
                        and j["banner"]["resets_at"] > now - STALL_SHOW_S and j["session_id"] not in seen]
            until = max((c["banner"]["resets_at"] for c in stalled if c["banner"]["resets_at"] > now - 6 * 3600),
                        default=None)
            state = seat_state(five, seven, until, now)
            resume_at = None
            if state == "blocked":
                cands = [until] if until and until > now else []
                cands += [w["resets_at"] for w in (five, seven) if w["pct"] is not None and w["pct"] >= 100]
                resume_at = max(cands) if cands else None
            seats.append({
                "config": cfg, "seat": seat, "label": config.account_label(seat),
                "excluded": excluded(cfg),
                "five_hour": {"pct": five["pct"], "resets_at": five["resets_at"], "since_reset": five["since_reset"]},
                "seven_day": {"pct": seven["pct"], "resets_at": seven["resets_at"], "since_reset": seven["since_reset"]},
                "meter_at": m.get("at"), "meter_age_min": round((now - m["at"]) / 60) if m.get("at") else None,
                "usage_meter": usage_meter.meter_state(cfg),
                "state": state, "resume_at": resume_at, "resume_at_ct": limits.fmt_clock(resume_at),
                "queued": [{"session_id": c["session_id"], "name": c["name"],
                            "resets_at": c["banner"]["resets_at"]} for c in stalled]})
        snap = {"at": now, "chats": chats, "jobs": jobs, "seats": seats, "fleet": fleet, "errors": errors}
        with self.lock:
            self.snap = snap
        return snap

    def get(self) -> dict:
        with self.lock:
            return self.snap
