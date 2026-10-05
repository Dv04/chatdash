"""Sending your answers to the chat that asked. Always your own action (a button), never automatic.

Route: chatdash actions.reply (claude attach through a pty for background chats, confirmed by reading
the prompt back from the transcript; refused for a chat open in a terminal tab). A chat that is
working gets the reply queued and sent when it goes idle (contract: "queued if working").
A read-only account (config read_only_accounts) is refused.
"""
from __future__ import annotations

import threading
import time

from . import db, sources

from .. import actions as _actions       # chatdash/actions.py


class Sender:
    def __init__(self, src, reply_fn=None):
        self.src = src
        self.reply_fn = reply_fn or (lambda chat, text: _actions.reply(chat, text))
        self.queue: dict[str, dict] = {}      # session_id -> {text, at, key}
        self.lock = threading.Lock()

    def _chat(self, session_id: str, key: str | None = None) -> dict | None:
        chats = self.src.get()["chats"]
        return next((c for c in chats if (key and c["key"] == key) or c["session_id"] == session_id), None)

    def reply(self, session_id: str, text: str, key: str | None = None) -> tuple[int, dict]:
        text = (text or "").strip()
        if not text:
            return 400, {"error": "empty reply"}
        if len(text) > 20000:
            return 400, {"error": "reply too long"}
        c = self._chat(session_id, key)
        if not c:
            return 404, {"error": "no such session in the last 24 h"}
        if c["excluded"] or sources.excluded(c["config"]):
            return 403, {"error": "this account is read-only here"}
        if c["state"] == "working":
            with self.lock:
                self.queue[c["session_id"]] = {"text": text, "at": time.time(), "key": c["key"]}
            db.log_auto("reply", "manual", c["session_id"], c["account"], "queued", "chat is working", {"text": text[:400]})
            return 200, {"ok": True, "queued": True, "confirmed": False}
        res = self.reply_fn(c, text)
        db.log_auto("reply", "manual", c["session_id"], c["account"], ("queued" if res.get("queued") else "sent") if res.get("ok") else "failed",
                    res.get("error") or res.get("route") or "", {"text": text[:400], "result": res})
        if not res.get("ok"):
            return 409, {"error": res.get("error") or "not delivered", "route": res.get("route")}
        return 200, {"ok": True, "queued": bool(res.get("queued")), "confirmed": bool(res.get("confirmed")), "route": res.get("route")}

    def drain(self) -> list[dict]:
        """Send queued replies whose chat is no longer working. Called from the refresh loop."""
        done = []
        with self.lock:
            items = list(self.queue.items())
        for sid, q in items:
            c = self._chat(sid, q["key"])
            if not c or c["state"] in ("working", "needs_you"):
                continue
            with self.lock:
                self.queue.pop(sid, None)
            code, res = self.reply(sid, q["text"], q["key"])
            done.append({"session_id": sid, "code": code, **res})
        return done
