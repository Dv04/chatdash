"""Limit banners: "You've hit your session limit · resets 4:40pm (America/Chicago)".

Measured 2026-10-02 over every transcript on this Mac: the banner is an assistant record with
isApiErrorMessage true. Variants seen: session limit (351), weekly limit (12), with reset text
"4:40pm", "3am", or "Sep 25 at 11am", always followed by an IANA zone in parentheses.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .. import config

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover
    ZoneInfo = None

BANNER_RE = re.compile(
    r"You've hit your (?P<kind>session|weekly) limit\s*·\s*resets\s+"
    r"(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+at\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_]+)\)")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_banner(text: str, at: float) -> dict | None:
    """-> {kind, resets_at (epoch), text} for a banner shown at epoch `at`, else None.
    A bare time means its next occurrence after `at` in the banner's zone."""
    m = BANNER_RE.search(text or "")
    if not m or ZoneInfo is None:
        return None
    try:
        tz = ZoneInfo(m["tz"])
    except Exception:
        return None
    h = int(m["h"]) % 12 + (12 if m["ap"] == "pm" else 0)
    mi = int(m["m"] or 0)
    shown = datetime.fromtimestamp(at, tz)
    if m["mon"]:
        mon = MONTHS.get(m["mon"])
        if not mon:
            return None
        t = shown.replace(month=mon, day=int(m["day"]), hour=h, minute=mi, second=0, microsecond=0)
        if t < shown - timedelta(days=1):
            t = t.replace(year=t.year + 1)
    else:
        t = shown.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= shown:
            t += timedelta(days=1)
    return {"kind": m["kind"], "resets_at": t.timestamp(), "text": m.group(0)}


def fmt_clock(epoch: float | None) -> str | None:
    """'4:40pm' style time of day in the configured time zone (default: this computer's), never UTC."""
    if epoch is None:
        return None
    t = datetime.fromtimestamp(epoch, config.tz())
    s = t.strftime("%I:%M%p").lstrip("0").lower()
    return s.replace(":00", "")


def iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()
