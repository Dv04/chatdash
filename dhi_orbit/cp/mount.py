"""Mount the control plane into the DHI Orbit server. server.py calls only:
    cp_mount = mount.attach(state)              in State.__init__
    cp_mount.refresh(rows)                      at the end of State._refresh
    if mount.route(handler, method, token): return    in do_GET / do_POST / do_PUT, after the guard
Everything else lives here. cp reuses the server's own transcript reader (no second parse), keeps its own
read model, and runs its auto-actions in one background loop guarded by <data dir>/run/auto.lock, so a dev
server and the main server can never both act. While this process owns the lock, cp's focus-mode notifier
replaces DHI Orbit's old needs-you / answered notifications; otherwise the old ones stay.
"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.parse

from .. import actions
from . import uploads, api, autolock, corrections, db, idlecompact, drafts, notify, resume, send, shadow, sources, work
from .devserver import History, ui_file

_ATTACHED = None


class Attached:
    def __init__(self, st):
        db.init()
        work.init()
        corrections.init()
        self.st = st
        self.src = sources.Sources(col=st.col)
        self.sender = send.Sender(self.src)
        self.ctx = {"sender": self.sender, "dialog": self._answer_dialog,
                    "stop": actions.stop, "terminal": actions.open_terminal, "reply": actions.reply,
                    "kw": st.kw, "chat_row": lambda key: st.by_key.get(key),
                    "seen": lambda key, final_at: st.idx.mark_seen(key, final_at),
                    "col": st.col, "search": lambda q: st.idx.search(q), "queue": self.sender.queue}
        self.notifier = notify.Notifier(send=lambda t, b: actions.notify(t, b) if st.notify else None)
        self.auto = [resume.Resumer(sender=lambda chat, text: actions.reply(chat, text)).tick,
                     self._idle_tick, shadow.Shadow().tick,
                     work.WorkLoop().tick, drafts.Drafter().tick, corrections.Daily().tick, History(self.src).tick]
        self.idle = idlecompact.IdleCompactor(kw=st.kw, sender=lambda chat, text: actions.reply(chat, text))
        st.kw.skip = self.idle.blocked
        st.kw.hold = self.idle.pending
        self.owner = False
        from . import fileindex
        self.indexer = fileindex.Indexer(owner=lambda: self.owner)
        self.ctx["indexer"] = self.indexer
        legacy = st._maybe_notify

        def gated(c):                              # old pings only while cp does not own notifications
            if self.owner:
                st.prev[c["key"]] = (c["state"], c["final_at"])
                return
            legacy(c)
        st._maybe_notify = gated
        threading.Thread(target=self._loop, daemon=True).start()

    def _idle_tick(self, snap: dict) -> None:
        self.idle.tick(snap)

    def _answer_dialog(self, chat: dict, n: int, label: str, text: str = "") -> dict:
        res = actions.answer_dialog(chat, n, label, text)
        self.st.dialogs.pop(chat["key"], None)         # the 8 s screen cache must not show the old dialog
        return res

    def refresh(self, rows: list) -> None:
        try:
            self.src.refresh(chats=rows)
        except Exception as e:                     # cp must never stop the old board refreshing
            self.src.snap = dict(self.src.get(), errors={"refresh": f"{type(e).__name__}: {e}"})

    def _loop(self, every: float = 5.0) -> None:
        while True:
            time.sleep(every)
            snap = self.src.get()
            if not snap.get("at"):
                continue
            try:
                self.sender.drain()
                self.owner = autolock.acquire()
                if not self.owner:
                    continue
                for a in self.auto:
                    try:
                        a(snap)
                    except Exception as e:
                        db.log_auto("loop", "on", None, None, "error", f"{getattr(a, '__qualname__', a)}: {type(e).__name__}: {e}"[:300])
                self.notifier.tick(api.overview(snap, time.time()))
            except Exception:
                pass


def attach(st) -> Attached:
    global _ATTACHED
    _ATTACHED = Attached(st)
    return _ATTACHED


ROOT_FILES = ("/", "/index.html", "/sw.js", "/manifest.webmanifest", "/icon.svg")   # the dashboard's own files, served at the root
ROOT_DIRS = ("/css/", "/js/")


def route(h, method: str, token: str) -> bool:
    """Serve the dashboard (at / and, for old bookmarks, /v2/) and /api/cp/ on the live server. Called after DHI Orbit's own
    guard (host, token, key)."""
    u = urllib.parse.urlparse(h.path)
    sub = u.path[3:] if u.path.startswith("/v2") else u.path if u.path in ROOT_FILES or u.path.startswith(ROOT_DIRS) else None
    if sub is not None:
        if method != "GET":
            return False
        p = ui_file(sub)
        if not p:
            h._send(404, {"error": "not found"})
            return True
        data = open(p, "rb").read()
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if p.endswith(".js"):
            ctype = "text/javascript"
        if p.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        if p.endswith(".html"):
            data = data.replace(b"__TOKEN__", token.encode())
        h._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        return True
    if not u.path.startswith("/api/cp/") or _ATTACHED is None:
        return False
    if method == "POST" and u.path == "/api/cp/uploads":
        # Raw bytes, not JSON: the dashboard attaches a file to a reply. Refused before reading if it is over the cap.
        n = int(h.headers.get("Content-Length") or 0)
        if n > uploads.MAX_BYTES:
            h.close_connection = True
            h._send(413, {"error": f"file too large (limit {uploads.MAX_BYTES // (1024 * 1024)} MB)"})
            return True
        code, out = uploads.save(urllib.parse.unquote(h.headers.get("X-Filename") or ""), h.rfile.read(n))
        h._send(code, out)
        return True
    body = {}
    if method != "GET":
        try:
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            h._send(400, {"error": "bad json"})
            return True
    code, out = api.handle(method, u.path[len("/api/cp/"):], urllib.parse.parse_qs(u.query), body, _ATTACHED.src, _ATTACHED.ctx)
    h._send(code, out)
    return True
