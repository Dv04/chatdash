"""Side panel: scheduled jobs (launchd jobs with your label prefix, plus cron lines). Files only.

The label prefix is config launchd_prefix (default com.chatdash). Only jobs whose label starts with it can be
switched on or off from the page; the job that runs the dashboard itself (and its tunnel) never can.
"""
from __future__ import annotations

import glob
import os
import plistlib
import re
import subprocess
import sys

from . import config

HOME = os.path.expanduser("~")
AGENTS = os.path.join(HOME, "Library", "LaunchAgents")


def _schedule_text(p: dict) -> str:
    if p.get("StartInterval"):
        return f"every {p['StartInterval'] // 60} min"
    cal = p.get("StartCalendarInterval")
    if cal:
        cal = cal if isinstance(cal, list) else [cal]
        days = sorted({c.get("Weekday") for c in cal if "Weekday" in c})
        c0 = cal[0]
        hm = f"{c0.get('Hour', 0):02d}:{c0.get('Minute', 0):02d}"
        if days == [1, 2, 3, 4, 5]:
            return f"weekdays {hm}"
        return f"{hm}" + (f" days {days}" if days else " daily")
    if p.get("KeepAlive"):
        return "always on"
    if p.get("RunAtLoad"):
        return "at login"
    return "-"


def _last_line(path: str | None) -> tuple[str | None, float | None]:
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 2000))
            lines = [l for l in fh.read().decode(errors="replace").splitlines() if l.strip()]
        return (lines[-1][:220] if lines else None), os.path.getmtime(path)
    except OSError:
        return None, None


def schedules() -> list[dict]:
    try:
        listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        listed = ""
    loaded = {}
    for line in listed.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            loaded[parts[2]] = {"pid": parts[0], "exit": parts[1]}
    out = []
    prefix = config.launchd_prefix()
    for f in sorted(glob.glob(os.path.join(AGENTS, glob.escape(prefix) + ".*plist"))):
        try:
            p = plistlib.load(open(f, "rb"))
        except Exception:
            continue
        label = p.get("Label") or os.path.basename(f)[:-6]
        prog = " ".join(p.get("ProgramArguments") or [p.get("Program", "")])
        log = p.get("StandardOutPath") or p.get("StandardErrorPath")
        last, mt = _last_line(log)
        l = loaded.get(label)
        out.append({"kind": "launchd", "label": label, "on": l is not None,
                    "running": bool(l and l["pid"] != "-"), "last_exit": l["exit"] if l else None,
                    "schedule": _schedule_text(p), "program": prog[-160:],
                    "uses_claude": "claude" in prog.split("/")[-1],
                    "protected": is_protected(label),
                    "log": log, "last_line": last, "last_at": mt})
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        cron = ""
    for line in cron.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.search(r">>\s*(\S+)", s)
        last, mt = _last_line(m.group(1) if m else None)
        fields = s.split(None, 5)
        out.append({"kind": "cron", "label": os.path.basename(fields[5].split()[1]) if len(fields) > 5 and
                    len(fields[5].split()) > 1 else s[:40], "on": True, "running": False,
                    "schedule": " ".join(fields[:5]), "program": (fields[5] if len(fields) > 5 else s)[:160],
                    "uses_claude": False, "protected": True, "log": m.group(1) if m else None, "last_line": last, "last_at": mt})
    return out


def is_protected(label: str) -> bool:
    """The jobs that run this page (the server and its tunnel): turning them off would kill the page."""
    prefix = config.launchd_prefix()
    rest = label[len(prefix):].lstrip(".") if label.startswith(prefix) else ""
    return label == prefix or rest.split("-")[0].split(".")[0] in ("server", "tunnel", "chatdash")


def schedule_toggle(label: str, on: bool) -> dict:
    """Load (on) or unload (off) a launchd job with the configured prefix. The plist stays on disk either way."""
    prefix = config.launchd_prefix()
    if not re.fullmatch(re.escape(prefix) + r"(\.[\w.-]+)?", label or ""):
        return {"ok": False, "error": f"not a {prefix} job"}
    if is_protected(label):
        return {"ok": False, "error": "this job runs the dashboard itself; turn it off from Terminal"}
    if sys.platform != "darwin":
        return {"ok": False, "error": "launchd jobs exist on macOS only"}
    plist = os.path.join(AGENTS, label + ".plist")
    if not os.path.exists(plist):
        return {"ok": False, "error": "no plist"}
    uid = str(os.getuid())
    cmd = ["launchctl", "bootstrap", f"gui/{uid}", plist] if on else ["launchctl", "bootout", f"gui/{uid}/{label}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return {"ok": p.returncode == 0, "error": (p.stderr or p.stdout).strip()[-200:] or None}
