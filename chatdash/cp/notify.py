"""Focus-mode notifier (replaces the old needs-you / answered pings while cp owns notifications).

Focus off: every new needs-you item notifies once.
Focus on:  urgent items (high risk, or a hook hold under 3 minutes from expiry) notify at once; the rest wait
           until they are min_age_min old and then arrive together in ONE notification at each check-in
           window (HH:MM local time). Limit stalls never notify (limit-resume owns them). No "answered" pings.
macOS notifications only (no phone push in v1).
"""
from __future__ import annotations

import time
from datetime import datetime

from .. import config
from . import db

URGENT_HOLD_S = 180


class Notifier:
    def __init__(self, send=None):
        self.send = send or (lambda title, body: None)
        self.seen: set[str] = set()
        self.urgent_sent: set[str] = set()
        self.batched: set[str] = set()
        self.windows_fired: set[str] = set()
        self.primed = False

    def urgent(self, x: dict, now: float) -> bool:
        if x.get("risk") == "high":
            return True
        return bool(x.get("held") and x.get("timeout_at") and x["timeout_at"] - now < URGENT_HOLD_S)

    def tick(self, ov: dict, now: float | None = None) -> list[tuple[str, str]]:
        now = now or time.time()
        items = [x for x in ov.get("needs_you") or [] if x.get("kind") != "limit"]
        sent = []
        if not self.primed:                      # do not flood on start: everything already waiting counts as seen
            self.seen = {x["id"] for x in items}
            self.primed = True
            return sent
        focus = (db.settings().get("focus") or {})
        if not focus.get("on"):
            for x in items:
                if x["id"] not in self.seen:
                    self.seen.add(x["id"])
                    sent.append(self._send(f"{x['title'][:60]} needs you", (x.get("text") or x.get("kind") or "")[:180]))
            return sent
        min_age = float(focus.get("min_age_min") or 0) * 60
        for x in items:
            if self.urgent(x, now) and x["id"] not in self.urgent_sent:
                self.urgent_sent.add(x["id"])
                self.seen.add(x["id"])
                why = "high risk" if x.get("risk") == "high" else "the chat's hold ends in under 3 minutes"
                sent.append(self._send(f"Urgent: {x['title'][:60]}", f"{why}. {(x.get('text') or '')[:140]}"))
        t = datetime.fromtimestamp(now, config.tz())
        hhmm, day = t.strftime("%H:%M"), t.strftime("%Y-%m-%d")
        for w in focus.get("windows") or []:
            key = f"{day} {w}"
            if hhmm >= w and key not in self.windows_fired and self._in_window(hhmm, w):
                self.windows_fired.add(key)
                due = [x for x in items if x["id"] not in self.urgent_sent and x["id"] not in self.batched
                       and (x.get("seconds") or 0) >= min_age]
                if due:
                    self.batched.update(x["id"] for x in due)
                    titles = "; ".join(x["title"][:40] for x in due[:5])
                    sent.append(self._send(f"Check-in {w}: {len(due)} waiting", titles + (" ..." if len(due) > 5 else "")))
        return sent

    @staticmethod
    def _in_window(hhmm: str, w: str) -> bool:
        """Fire within 10 minutes after the window time (a sleeping computer must not fire 5 hours late)."""
        h1, m1 = map(int, hhmm.split(":"))
        h0, m0 = map(int, w.split(":"))
        return 0 <= (h1 * 60 + m1) - (h0 * 60 + m0) <= 10

    def _send(self, title: str, body: str) -> tuple[str, str]:
        self.send(title, body)
        db.log_auto("notify", "on", None, None, "sent", title[:120], {"body": body[:300]})
        return title, body
