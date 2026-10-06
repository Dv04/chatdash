#!/usr/bin/env python3
"""cp dev server: contract v0 on http://127.0.0.1:8788/api/cp/ and the v2 UI at /v2/.

    python3 -m dhi_orbit.cp.devserver --port 8788

No keep-warm and no notifications, so it can run beside the main DHI Orbit server without double-pinging
anything. Your own actions (answer a decision, reply to a chat) go through DHI Orbit actions.py exactly as
the main page's buttons do. Same auth as DHI Orbit: the token in <data dir>/.token (read, never created
here), X-Token header or ?token=, Host header checked.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import config
from . import api, autolock, db, sources

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("CP_UI_DIR") or os.path.join(os.path.dirname(HERE), "ui")


def read_token() -> str:
    try:
        tok = open(config.token_path()).read().strip()
    except OSError:
        tok = ""
    if not tok:
        sys.exit(f"{config.token_path()} missing: start DHI Orbit once (it creates it); the dev server never writes it")
    return tok


def ui_file(rel: str) -> str | None:
    """Resolve a /v2/ path inside UI_DIR; None for anything outside it."""
    rel = urllib.parse.unquote(rel).lstrip("/") or "index.html"
    root = os.path.realpath(UI_DIR)
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        return None
    if os.path.isdir(p):
        p = os.path.join(p, "index.html")
    return p if os.path.isfile(p) else None


def make_handler(src: sources.Sources, token: str, port: int, ui_token_inject: bool = True, ctx: dict | None = None):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, q) -> bool:
            if self.headers.get("Host") not in hosts:
                self._send(403, {"error": "bad host"})
                return False
            if self.path.startswith("/api/") and self.headers.get("X-Token") != token \
                    and (q.get("token") or [""])[0] != token:
                self._send(401, {"error": "token"})
                return False
            return True

        def _body(self) -> dict | None:
            try:
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return None

        def _route(self, method: str):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._guard(q):
                return
            if u.path == "/" and method == "GET":
                return self._send(302, b"", "text/plain", {"Location": "/v2/"})
            if u.path.startswith("/v2") and method == "GET":
                p = ui_file(u.path[3:])
                if not p:
                    return self._send(404, {"error": "not found"})
                data = open(p, "rb").read()
                ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
                if p.endswith(".js"):
                    ctype = "text/javascript"
                if p.endswith(".webmanifest"):
                    ctype = "application/manifest+json"
                if ui_token_inject and p.endswith(".html"):
                    data = data.replace(b"__TOKEN__", token.encode())
                return self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            if u.path.startswith("/api/cp/"):
                body = {} if method == "GET" else self._body()
                if body is None:
                    return self._send(400, {"error": "bad json"})
                code, out = api.handle(method, u.path[len("/api/cp/"):], q, body, src, ctx)
                return self._send(code, out)
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return H


def loop(src: sources.Sources, every: float, sender=None, auto=None) -> None:
    while True:
        try:
            snap = src.refresh()
            if sender:
                sender.drain()
            if auto and autolock.acquire():
                for a in auto:
                    a(snap)
        except Exception as e:                       # keep serving; health turns unknown
            src.snap = dict(src.snap, errors={"refresh": f"{type(e).__name__}: {e}"})
        time.sleep(every)


class History:
    """One compact graph frame a minute (id, state, needs_you, seat, label) for the day replay; 2 days kept."""

    def __init__(self, src):
        self.src, self.last = src, 0.0

    def tick(self, snap):
        now = time.time()
        if now - self.last < 60:
            return
        self.last = now
        g = api.graph(snap, now)
        frame = [[n["id"], n["state"], 1 if n.get("needs_you") else 0, n.get("seat"), (n.get("label") or "")[:60], n["type"]]
                 for n in g["nodes"]]
        db.execute("INSERT OR REPLACE INTO cp_graph_history(at, frame) VALUES(?,?)", (now, json.dumps(frame)))
        db.execute("DELETE FROM cp_graph_history WHERE at < ?", (now - 2 * 86400,))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--window-hours", type=float, default=24)
    a = ap.parse_args()
    token = read_token()
    db.init()
    from . import work as _work
    _work.init()
    src = sources.Sources(int(a.window_hours * 3600))
    src.refresh()
    from .. import actions
    from . import send
    sender = send.Sender(src)
    ctx = {"sender": sender, "dialog": lambda chat, n, label, text="": actions.answer_dialog(chat, n, label, text),
           "stop": actions.stop, "terminal": actions.open_terminal, "reply": actions.reply}
    from . import resume
    resumer = resume.Resumer(sender=lambda chat, text: actions.reply(chat, text))
    from . import corrections, drafts, shadow, work
    shadower, drafter, daily = shadow.Shadow(), drafts.Drafter(), corrections.Daily()
    worker = work.WorkLoop()
    corrections.init()
    threading.Thread(target=loop, args=(src, a.every, sender, [resumer.tick, shadower.tick, worker.tick, drafter.tick, daily.tick, History(src).tick]),
                     daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 128   # the default of 5 resets a browser's parallel module fetches
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(src, token, a.port, ctx=ctx))
    print(f"cp dev server on http://127.0.0.1:{a.port}/v2/  ({len(src.get()['chats'])} chats)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
