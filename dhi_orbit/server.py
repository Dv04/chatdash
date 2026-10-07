#!/usr/bin/env python3
"""DHI Orbit: one local page for every Claude chat across all config dirs.

    dhi-orbit                                 # http://127.0.0.1:8787/      (also `orbit`; `chatdash` still works)
    dhi-orbit --port 8790 --window-hours 48

Viewing costs zero model tokens (files only). A reply is one normal turn in that chat.
Bound to 127.0.0.1; every /api call needs the token from <data dir>/.token (injected into the page);
the Host header is checked so a web page cannot reach it by DNS rebinding.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import _plat, actions, collector, config, keepwarm, panels
from .index import Index
from .cp import mount as cp_mount  # DHI Orbit dashboard and control plane: / (also /v2/) and /api/cp/

HERE = os.path.dirname(os.path.abspath(__file__))


def load_public() -> dict:
    """Optional public access through a tunnel YOU set up. <data dir>/public.json:
    {"host", "key_login", "team_domain", "emails": [...]}; "host" falls back to config.json public_url.
    With neither, only 127.0.0.1 and localhost are accepted."""
    try:
        pub = json.load(open(config.public_path(), encoding="utf-8-sig"))
        pub = pub if isinstance(pub, dict) else {}
    except (OSError, ValueError):
        pub = {}
    if not pub.get("host") and config.public_url():
        pub = dict(pub, host=urllib.parse.urlparse(config.public_url() if "//" in config.public_url()
                                                   else "//" + config.public_url()).netloc)
    return pub


_IDENT: dict[str, tuple] = {}
FAILS: dict[str, list] = {}
LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DHI Orbit</title><style>body{font:16px -apple-system,system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px}
input,button{font:inherit;padding:8px;width:100%;box-sizing:border-box;margin-top:8px}</style></head><body>
<h2>DHI Orbit</h2><p>Enter the access key (shown on the computer running DHI Orbit, Seats tab).</p>
<input id="k" type="password" autocomplete="current-password"><button id="b">Sign in</button><p id="m"></p>
<script>
async function go(k){const r=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:k})});
 if(r.ok){location.replace("/")}else{document.getElementById("m").textContent=(await r.json()).error}}
document.getElementById("b").onclick=()=>go(document.getElementById("k").value);
document.getElementById("k").onkeydown=e=>{if(e.key==="Enter")go(e.target.value)};
const h=new URLSearchParams(location.hash.slice(1)).get("k");if(h){history.replaceState(null,"","/");go(h)}
</script></body></html>"""


def access_identity(team_domain: str, token: str) -> str | None:
    """Email of a valid Cloudflare Access session, asked of Cloudflare itself
    (https://<team>/cdn-cgi/access/get-identity), cached 5 min per token. None if invalid."""
    import urllib.request
    hit = _IDENT.get(token)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    email = None
    try:
        req = urllib.request.Request(f"https://{team_domain}/cdn-cgi/access/get-identity",
                                     headers={"Cookie": f"CF_Authorization={token}",
                                              "User-Agent": "dhi-orbit/1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            email = (json.loads(r.read() or b"{}").get("email") or "").lower() or None
    except Exception:
        email = None
    _IDENT[token] = (time.time(), email)
    return email
BUILD = str(int(time.time()))
# A bare "." can read as "continue" and restart work; this says exactly what is wanted.
KEEP_WARM = keepwarm.PING_TEXT     # the button and the automatic pinger send the same text; extract.py hides it


def load_token() -> str:
    """The access token, created on first run in the data directory with mode 600."""
    config.ensure_home()
    try:
        tok = open(config.token_path(), encoding="utf-8").read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(config.token_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _plat.O_BIN, 0o600)
    os.write(fd, tok.encode())
    os.close(fd)
    os.chmod(config.token_path(), 0o600)
    return tok


class State:
    def __init__(self, window_s: int, notify: bool):
        self.col = collector.Collector(window_s)
        self.idx = Index()
        self.chats: list[dict] = []
        self.by_key: dict[str, dict] = {}
        self.seats: dict = {}
        self.version = 0
        self.notify = notify
        self.cond = threading.Condition()
        self.prev: dict[str, tuple] = {}
        self.error = None
        self.queue: dict[str, str] = {}     # key -> reply waiting for the chat to go idle
        self.dialogs: dict[str, tuple] = {}
        self.busy: dict[str, str] = {}      # key -> bulk action in progress on that chat
        self.refresh_lock = threading.Lock()
        self.log: list[dict] = []           # recent action results, newest first
        self.cp = None
        self.kw = keepwarm.KeepWarm(
            notify=lambda title, body: actions.notify(title, body) if self.notify else None,
            record=lambda what, key, res: self.record(what, key, res))

    def record(self, what: str, key: str, res: dict) -> dict:
        self.log.insert(0, {"at": time.time(), "what": what, "key": key, "ok": res.get("ok"),
                            "detail": res.get("error") or res.get("route") or res.get("sent") or ""})
        del self.log[50:]
        return res

    def _dialog(self, c: dict):
        """What a waiting background chat is showing (permission prompt or question), read with
        `claude logs` (no tokens), cached 8 s per chat."""
        if c["state"] != "needs_you" or not c.get("job_id"):
            self.dialogs.pop(c["key"], None)
            return None
        hit = self.dialogs.get(c["key"])
        if hit and time.time() - hit[0] < 8:
            return hit[1]
        d = actions.read_dialog(c)
        self.dialogs[c["key"]] = (time.time(), d)
        return d

    def bulk(self, action: str) -> dict:
        """compact_big (warm, idle, context > 500k) / compact_warm (every warm idle chat) /
        allow_all (every waiting permission prompt, plain Yes). Runs one chat at a time in a
        background thread; each result lands in the action log."""
        idle = [c for c in self.chats if c["kind"] in ("bg",) and c["state"] in ("idle",)
                and c["warmth"] in ("warm", "cooling") and not self.busy.get(c["key"])]
        if action == "compact_big":
            targets = [c for c in idle if c["ctx_tokens"] > 500_000]
        elif action == "compact_warm":
            targets = [c for c in idle if c["ctx_tokens"] > 60_000]
        elif action == "allow_all":
            targets = [c for c in self.chats if c["state"] == "needs_you" and c.get("dialog")
                       and c["dialog"]["kind"] == "permission"]
        else:
            return {"ok": False, "error": "unknown action"}

        def run():
            for c in targets:
                self.busy[c["key"]] = action
                try:
                    if action == "allow_all":
                        yes = next((o for o in c["dialog"]["options"] if o["label"].strip().lower() == "yes"),
                                   None) or next((o for o in c["dialog"]["options"]
                                                  if o["label"].lower().startswith("yes")), None)
                        res = actions.answer_dialog(c, yes["n"], yes["label"]) if yes else {"ok": False, "error": "no Yes"}
                    else:
                        res = actions.reply(c, "/compact")
                    self.record(action, c["key"], res)
                finally:
                    self.busy.pop(c["key"], None)
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "count": len(targets), "targets": [c["name"][:50] for c in targets]}

    def _drain_queue(self) -> None:
        for key, text in list(self.queue.items()):
            c = self.by_key.get(key)
            if not c or c["state"] in ("working", "needs_you"):
                continue
            del self.queue[key]
            def run(c=c, text=text):
                res = self.record("queued reply", c["key"], actions.reply(c, text, manual=True))
                if self.notify:
                    actions.notify("Queued reply " + ("sent" if res.get("ok") else "FAILED"),
                                   f"{c['name'][:40]}: {res.get('error') or text[:80]}")
            threading.Thread(target=run, daemon=True).start()

    def refresh(self) -> None:
        with self.refresh_lock:          # the loop and action handlers both call this
            self._refresh()

    def _refresh(self) -> None:
        rows = self.col.snapshot()
        seen = self.idx.seen_map()
        for c in rows:
            c["unread"] = bool(c["final"]) and (c["final_at"] or "") > (seen.get(c["key"]) or "")
            c["column"] = ("needs_you" if c["state"] == "needs_you" else
                           "working" if c["state"] == "working" else
                           "ready" if c["unread"] and c["state"] == "idle" else
                           "idle" if c["state"] == "idle" else "stopped")
            c["cold_next"] = bool(c["idle_min"] is not None and c["idle_min"] > 55 and c["ctx_tokens"] > 30000)
            try:
                self.idx.sync(c, self.col.turns(c["path"]))
            except Exception:
                pass
            self._maybe_notify(c)
        sig = lambda cs: [(c["key"], c["version"], c["state"], c.get("warmth")) for c in cs]
        changed = sig(rows) != sig(self.chats)
        states = self.col.pr_states(sorted({p["url"] for c in rows for p in c.get("prs") or []}))
        for c in rows:
            c["queued"] = self.queue.get(c["key"])
            c["dialog"] = self._dialog(c)
            prs = [dict(p, state=states.get(p["url"])) for p in c.get("prs") or []]
            c["prs"] = [p for p in prs if p["state"] in (None, "open")]   # merged/closed hidden
            c["busy"] = self.busy.get(c["key"])
            c["kw"] = self.kw.view(c)
        self.chats, self.by_key = rows, {c["key"]: c for c in rows}
        self._drain_queue()
        try:                             # a keep-warm bug must never stop the dashboard refreshing
            self.kw.tick(self.by_key, queued=set(self.queue), busy={k for k, v in self.busy.items() if v}, seats=self.seats)
        except Exception as e:
            self.error = f"keepwarm: {type(e).__name__}: {e}"
        self.seats = self.col.seat_usage()
        if self.cp:
            self.cp.refresh(rows)
        if changed:
            with self.cond:
                self.version += 1
                self.cond.notify_all()

    def _maybe_notify(self, c: dict) -> None:
        was = self.prev.get(c["key"])
        self.prev[c["key"]] = (c["state"], c["final_at"])
        if not self.notify or was is None or c["kind"] == "headless":
            return
        if c["state"] == "needs_you" and was[0] != "needs_you":
            actions.notify(f"{c['name'][:40]} needs you", (c.get("waiting_for") or c["account"]))
        elif was[0] == "working" and c["state"] == "idle" and c["final_at"] != was[1]:
            actions.notify(f"{c['name'][:40]} answered", (c["final"] or "")[:160])

    def loop(self, every: float) -> None:
        while True:
            try:
                self.refresh()
                self.error = None
            except Exception as e:          # keep serving; show the error in the page
                self.error = f"{type(e).__name__}: {e}"
            time.sleep(every)


def make_handler(st: State, token: str, port: int):
    page = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    js = lambda v: json.dumps(v)[1:-1]            # safe inside a JS string literal
    page = page.replace("__PUBLIC_URL__", js(config.public_url().rstrip("/"))).replace("__DEFAULT_CWD__", js(config.default_cwd()))
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, q) -> bool:
            host = self.headers.get("Host")
            pub = load_public()
            if host not in hosts:
                # Public hostname through the Cloudflare Tunnel: FAIL CLOSED unless Cloudflare
                # Access is configured and this request carries a valid Access session for an
                # allowed email. Checked on every request, page and API alike.
                if not pub.get("host") or host != pub["host"]:
                    self._send(403, {"error": "bad host"}); return False
                if pub.get("key_login") and not (pub.get("team_domain") and pub.get("emails")):
                    return self._key_guard(token)
                if not (pub.get("team_domain") and pub.get("emails")):
                    self._send(403, {"error": "Cloudflare Access is not configured yet; locked"}); return False
                tok = self.headers.get("Cf-Access-Jwt-Assertion") or ""
                if not tok:
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        k, _, v = part.strip().partition("=")
                        if k == "CF_Authorization":
                            tok = v
                email = access_identity(pub["team_domain"], tok) if tok else None
                if not email or email not in [e.lower() for e in pub["emails"]]:
                    self._send(403, {"error": "not signed in through Cloudflare Access"}); return False
            if self.path.startswith("/api/") and \
                    self.headers.get("X-Token") != token and q.get("token", [""])[0] != token:
                self._send(401, {"error": "token"}); return False
            return True

        def _cookie(self, name: str) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _key_guard(self, token: str) -> bool:
            """Public access without Cloudflare Access: the device must hold the secret key as a
            Secure HttpOnly cookie, set once through /login. Wrong keys are rate limited per IP."""
            import hmac
            if hmac.compare_digest(self._cookie("cd_key"), token):
                return True
            path = urllib.parse.urlparse(self.path).path
            if path == "/login" and self.command == "POST":
                ip = self.headers.get("Cf-Connecting-Ip") or self.client_address[0]
                fails = [t for t in FAILS.get(ip, []) if time.time() - t < 3600]
                if len(fails) >= 10:
                    self._send(429, {"error": "too many wrong keys; try again in an hour"}); return False
                try:
                    key = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}").get("key", "")
                except ValueError:
                    key = ""
                if key and hmac.compare_digest(key.strip(), token):
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Set-Cookie", f"cd_key={token}; Max-Age=2592000; Path=/; Secure; HttpOnly; SameSite=Strict")
                    self.end_headers()
                    self.wfile.write(body)
                    return False
                FAILS[ip] = fails + [time.time()]
                self._send(401, {"error": "wrong key"}); return False
            if path == "/" and self.command == "GET":
                self._send(200, LOGIN_PAGE, "text/html; charset=utf-8"); return False
            self._send(401, {"error": "sign in first"}); return False

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._guard(q):
                return
            if cp_mount.route(self, "GET", token):
                return
            p = u.path
            if p == "/classic":                     # the first-generation page, no longer linked from the dashboard
                return self._send(200, page.replace("__TOKEN__", token), "text/html; charset=utf-8")
            if p == "/api/state":
                return self._send(200, {"version": st.version, "chats": st.chats, "seats": st.seats,
                                        "error": st.error, "now": time.time(), "build": BUILD,
                                        "kw_auto": st.kw.auto_on()})
            if p == "/api/chat":
                c = st.by_key.get(q.get("key", [""])[0])
                if not c:
                    return self._send(404, {"error": "no such chat"})
                return self._send(200, {"chat": c, "turns": st.col.turns(c["path"])[-80:]})
            if p == "/api/search":
                return self._send(200, {"hits": st.idx.search(q.get("q", [""])[0])})
            if p == "/api/schedules":
                return self._send(200, {"jobs": panels.schedules()})
            if p == "/api/history":
                by_path = {c["path"]: c for c in st.chats}
                costs = []
                for r in st.col.cost_days(7):
                    c = by_path.get(r["path"])
                    if c:
                        costs.append({"key": c["key"], "name": c["name"], "ws": c.get("ws") or "other",
                                      "account": c["account"], "by_day": r["by_day"]})
                return self._send(200, {"seats": st.col.seat_history(), "costs": costs})
            if p == "/api/digest":
                hours = float(q.get("hours", ["24"])[0])
                since = time.time() - hours * 3600
                groups: dict[str, list] = {}
                for c in st.chats:
                    for t in st.col.turns(c["path"]):
                        ts = collector.iso_epoch(t.get("fts") or t.get("pts"))
                        if ts and ts >= since and t.get("final"):
                            groups.setdefault(c.get("ws") or "other", []).append(
                                {"key": c["key"], "name": c["name"], "account": c["account"],
                                 "prompt": t["prompt"][:400], "final": t["final"], "ts": t.get("fts")})
                for g in groups.values():
                    g.sort(key=lambda x: x["ts"] or "", reverse=True)
                return self._send(200, {"groups": dict(sorted(groups.items()))})
            if p == "/api/approvals":
                return self._send(200, {"log": st.log})
            if p == "/api/events":
                return self._events()
            self._send(404, {"error": "not found"})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = -1
            try:
                while True:
                    with st.cond:
                        st.cond.wait_for(lambda: st.version != last, timeout=25)
                    if st.version != last:
                        last = st.version
                        self.wfile.write(f"data: {last}\n\n".encode())
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_PUT(self):
            u = urllib.parse.urlparse(self.path)
            if not self._guard(urllib.parse.parse_qs(u.query)):
                return
            if not cp_mount.route(self, "PUT", token):
                self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if not self._guard(urllib.parse.parse_qs(u.query)):
                return
            if cp_mount.route(self, "POST", token):
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            p = u.path
            chat = st.by_key.get(body.get("key", ""))
            if p in ("/api/reply", "/api/stop", "/api/open", "/api/seen", "/api/unseen", "/api/permission",
                     "/api/keepwarm", "/api/keepwarm_auto", "/api/compact",
                     "/api/queue") and not chat:
                return self._send(404, {"error": "no such chat"})
            if p == "/api/reply":
                return self._send(200, st.record("reply", chat["key"], actions.reply(chat, body.get("text", ""), manual=True)))
            if p == "/api/queue":
                text = (body.get("text") or "").strip()
                if text:
                    st.queue[chat["key"]] = text
                else:
                    st.queue.pop(chat["key"], None)
                return self._send(200, {"ok": True, "queued": bool(text)})
            if p == "/api/permission":
                try:
                    n = int(body.get("n"))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "This page is out of date: reload it (Cmd+R)."})
                res = actions.answer_dialog(chat, n, body.get("label") or "", body.get("text") or "")
                st.dialogs.pop(chat["key"], None)
                return self._send(200, st.record(f"answer {n}", chat["key"], res))
            if p == "/api/keepwarm":
                return self._send(200, st.record("keep warm", chat["key"], actions.reply(chat, KEEP_WARM)))
            if p == "/api/keepwarm_auto":
                if body.get("on"):
                    res = st.kw.enable(chat, float(body.get("hours") or keepwarm.DEFAULT_HOURS))
                else:
                    st.kw.disable(chat["key"])
                    res = {"ok": True}
                threading.Thread(target=st.refresh, daemon=True).start()
                return self._send(200, st.record("auto keep warm " + ("on" if body.get("on") else "off"),
                                                 chat["key"], res))
            if p == "/api/compact":
                return self._send(200, st.record("compact", chat["key"], actions.reply(chat, "/compact")))
            if p == "/api/keepwarm_all":
                st.kw.set_auto(bool(body.get("on")))
                threading.Thread(target=st.refresh, daemon=True).start()
                return self._send(200, {"ok": True, "on": st.kw.auto_on()})
            if p == "/api/bulk":
                return self._send(200, st.bulk(body.get("action", "")))
            if p == "/api/sched":
                return self._send(200, panels.schedule_toggle(body.get("label", ""), bool(body.get("on"))))
            if p == "/api/stop":
                res = st.record("stop", chat["key"], actions.stop(chat))
                threading.Thread(target=st.refresh, daemon=True).start()   # show it at once
                return self._send(200, res)
            if p == "/api/open":
                return self._send(200, actions.open_terminal(chat))
            if p == "/api/seen":
                st.idx.mark_seen(chat["key"], chat.get("final_at"))
                return self._send(200, {"ok": True})
            if p == "/api/unseen":
                st.idx.mark_seen(chat["key"], "")
                threading.Thread(target=st.refresh, daemon=True).start()
                return self._send(200, {"ok": True})
            if p == "/api/new":
                acct = body.get("account") or best_seat(st.seats)
                cfg = next((c for c in collector.config_dirs() if collector.account_name(c) == acct), None)
                if not cfg:
                    return self._send(400, {"error": f"unknown account {acct}"})
                if config.is_read_only(acct):
                    return self._send(403, {"error": f"account {acct} is read-only here"})
                return self._send(200, dict(actions.new_chat(cfg, body.get("name") or "dhi-orbit",
                                                             body.get("text", ""),
                                                             os.path.expanduser(body.get("cwd") or config.default_cwd())),
                                            account=acct))
            self._send(404, {"error": "not found"})

    return H


def best_seat(seats: dict) -> str:
    """Most headroom = lowest max(5h, 7d); a read-only account is never auto-picked. With no usable
    reading, the first discovered account that is not read-only ("" when there is none)."""
    ro = config.read_only_accounts()
    used = lambda v: 100 if v is None else v        # no reading counts as full; 0% used is the most headroom
    ok = [(max(used(s["five"]), used(s["seven"])), a) for a, s in seats.items() if a not in ro and s.get("ok")]
    if ok:
        return min(ok)[1]
    names = [collector.account_name(c) for c in collector.config_dirs()]
    return next((a for a in names if a not in ro), names[0] if names else "")



class QuietServer(ThreadingHTTPServer):
    """A browser that navigates away mid-response is not an error: no traceback per closed tab."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="dhi-orbit",
        description="A local dashboard for every Claude Code chat across your config dirs. "
                    "Serves http://127.0.0.1:<port>/ (loopback only; the access token is in the data dir).",
        epilog="Data dir: $DHI_ORBIT_HOME or ~/.config/dhi-orbit (token, database, config.json).")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--window-hours", type=float, default=24)
    ap.add_argument("--every", type=float, default=1.5, help="refresh seconds (a snapshot costs about 0.1 s)")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    a = ap.parse_args()
    if _plat.IS_WIN:                       # a Windows console defaults to cp1252: chat names and arrows must not crash print
        for st_ in (sys.stdout, sys.stderr):
            try:
                st_.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    token = load_token()
    st = State(int(a.window_hours * 3600), not a.no_notify)
    st.cp = cp_mount.attach(st)
    st.refresh()
    threading.Thread(target=st.loop, args=(a.every,), daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 128   # the default of 5 resets a browser's parallel module fetches
    srv = QuietServer(("127.0.0.1", a.port), make_handler(st, token, a.port))
    live = sum(1 for c in st.chats if c.get("kind") in ("bg", "interactive"))
    print(f"DHI Orbit on http://127.0.0.1:{a.port}/  ({len(st.chats)} chats found in your Claude folders: {live} open, "
          f"{len(st.chats) - live} closed or headless, the closed ones are folded under 'closed' in the chat list; "
          f"Orbit's own data is in {config.home()})", flush=True)
    if a.open:
        import webbrowser
        try:
            webbrowser.open(f"http://127.0.0.1:{a.port}/")
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:                    # Ctrl+C is the normal way to stop it, not an error with a traceback
        print("DHI Orbit stopped", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
