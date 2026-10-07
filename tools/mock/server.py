#!/usr/bin/env python3
"""Mock server for the v2 UI: contract v0 with SYNTHETIC data, http://127.0.0.1:8791/v2/

    python3 tools/mock/server.py [--port 8791] [--scenario normal]

Scenarios (switch at runtime: GET /mock/scenario?name=...):
  normal   a busy day: decisions, blocked asks, a limit stall, 7 seats
  missing  meter unreadable: health unknown, seats unknown (absence-is-never-green test)
  empty    nothing needs you
  big      100+ graph nodes (graph performance test)
  truth    normal, but gamma has no reading and echo is at 98% 7-day (nebula field data-truth check)
Token is the literal "mock". Nothing here reads real chats or touches DHI Orbit.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dhi_orbit", "ui")
TOKEN = "mock"
STATE = {"scenario": "normal", "answers": [], "settings": {"focus": {"on": False, "min_age_min": 15, "windows": ["10:00", "14:00"]},
                                                           "modes": [], "keepwarm": {"available": False}, "pace": {"pct_per_day": 15.0}}}
SEATS = ["main", "alpha", "work", "beta", "delta", "echo", "gamma"]
WIS = [("PROJ-02", "Outreach campaign"), ("PROJ-06", "Edge device ops"), ("PROJ-08", "Research writeup"), ("PROJ-10", "Repos and code review"),
       ("PROJ-12", "Agent fleet and cost"), ("PROJ-14", "Dashboard and alerts"), ("PROJ-16", "Integration work")]


def seat(name, five, seven, now, state=None, resume=None):
    return {"config": f"/Users/demo/.claude-{name}", "seat": name, "label": name.title(), "excluded": name == "main",
            "five_hour": {"pct": five, "resets_at": now + 5400 if five is not None else None, "since_reset": False},
            "seven_day": {"pct": seven, "resets_at": now + 3 * 86400 if seven is not None else None, "since_reset": False},
            "meter_at": now - 120, "meter_age_min": 2, "state": state or ("unknown" if five is None else "blocked" if five >= 100
                                                                           else "near" if max(five, seven or 0) >= 80 else "ok"),
            "resume_at": resume, "resume_at_ct": None, "queued": []}


def seats(now):
    if STATE["scenario"] == "missing":
        return [seat(s, None, None, now) for s in SEATS]
    vals = {"main": (12, 61), "alpha": (83, 50), "work": (28, 7), "beta": (100, 47), "delta": (41, 18), "echo": (5, 88), "gamma": (64, 43)}
    if STATE["scenario"] == "truth":       # nebula data-truth check: one seat with no reading, one at 98% of its week
        vals.update({"gamma": (None, None), "echo": (5, 98)})
    out = [seat(s, *vals[s], now) for s in SEATS]
    out[3]["resume_at"] = now + 2400
    return out


def needs(now):
    if STATE["scenario"] in ("empty",):
        return []
    fin = ("SYNTHETIC. Paper 08 draft compiles; 14 figures regenerated. I have not run the real-data pass yet because it "
           "needs about 40 GB free and the disk has 22 GB. Waiting on your go and the cleanup decision.")
    items = [
        {"id": "decision:d-101", "kind": "decision", "decision_id": "d-101", "session_id": "s-1", "key": "work:s-1", "seat": "work",
         "work_item": "PROJ-10", "title": "Merge PR #481 (app-server SSE fix)?", "text": "Merge PR #481 now, or wait for the device smoke test?",
         "options": [{"id": "a", "label": "Merge now", "desc": "CI is green locally"}, {"id": "b", "label": "Wait for smoke test"},
                     {"id": "c", "label": "Close it"}], "recommended": "b", "risk": "high", "on_timeout": "dialog", "held": True, "mode": "on",
         "timeout_at": now + 300, "since": now - 420, "seconds": 420},
        {"id": "dialog:work:s-7", "kind": "dialog", "session_id": "s-7", "key": "work:s-7", "seat": "work", "work_item": "PROJ-08",
         "title": "SYNTHETIC chat 7", "text": "Do you want to proceed?", "since": now - 60, "seconds": 60,
         "screen": {"kind": "permission", "question": "Do you want to proceed?", "tabs": None, "cursor": 1,
                    "options": [{"n": 1, "label": "Yes", "desc": "", "free": False}, {"n": 2, "label": "No", "desc": "", "free": False}]}},
        {"id": "blocked:work:j-2", "kind": "blocked", "session_id": "s-2", "key": "work:s-2", "seat": "work", "work_item": "PROJ-08",
         "title": "PROJ-08 Research writeup", "text": "(1) go/no-go on paper 08 real-data run (2) pull + clean LaTeX artifacts? (3) route the deploy thread elsewhere?",
         "suggested": "go ahead, start the paper 08 real-data run", "since": now - 7 * 3600, "seconds": 7 * 3600},
        {"id": "decision:d-103", "kind": "decision", "decision_id": "d-103", "session_id": "s-6", "key": "delta:s-6", "seat": "delta",
         "work_item": "PROJ-03", "title": "Post schedule", "text": "Which days? / Which networks?", "risk": "low", "on_timeout": "default",
         "held": True, "mode": "on", "timeout_at": now + 420, "since": now - 30, "seconds": 30,
         "parts": [{"question": "Which days should posts go out?", "header": "Days", "multi": False, "recommended": "1",
                    "options": [{"id": "1", "label": "Tue and Thu (Recommended)", "desc": "matches last month's best reach"},
                                {"id": "2", "label": "Every weekday", "desc": ""}]},
                   {"question": "Which networks?", "header": "Networks", "multi": True, "recommended": "1",
                    "options": [{"id": "1", "label": "LinkedIn (Recommended)", "desc": ""}, {"id": "2", "label": "X", "desc": ""},
                                {"id": "3", "label": "Instagram", "desc": ""}]}]},
        {"id": "decision:d-102", "kind": "decision", "decision_id": "d-102", "session_id": "s-3", "key": "alpha:s-3", "seat": "alpha",
         "work_item": "PROJ-14", "title": "Event log retention", "text": "Keep 30 or 90 days of events in D1?",
         "options": [{"id": "a", "label": "30 days"}, {"id": "b", "label": "90 days"}], "recommended": "a", "risk": "low", "held": True, "mode": "on",
         "on_timeout": "default", "timeout_at": now + 480, "since": now - 60, "seconds": 60},
        {"id": "limit:beta:j-4", "kind": "limit", "session_id": "s-4", "key": "beta:s-4", "seat": "beta", "work_item": "PROJ-13",
         "title": "A4 product build", "text": "Limit reset at 4:40am passed; chat still stalled", "since": now - 1900, "seconds": 1900},
        {"id": "blocked:main:j-5", "kind": "blocked", "session_id": "s-5", "key": "main:s-5", "seat": "main", "work_item": "PROJ-06",
         "title": "PROJ-06 Edge device ops", "text": "Approve the box reboot window tonight?", "since": now - 2 * 86400,
         "seconds": 2 * 86400, "excluded": True},
    ]
    rc_ok = {"session_id": "s-1", "turn": 14, "at": now - 400, "verified": True, "cost_units": 48000,
             "diff": {"files": 3, "add": 42, "del": 7, "source": "git + tool calls", "commits": ["a1b2c3d fix SSE reconnect"]},
             "tests": {"cmd": "npm test", "last_line": "Tests: 128 passed, 128 total", "exit": 0},
             "prs": ["https://github.com/acme/app-server/pull/481"],
             "files": [{"path": "src/sse.ts", "source": "git"}, {"path": "src/sse.test.ts", "source": "tool"}, {"path": "README.md", "source": "git"}]}
    rc_bad = {"session_id": "s-2", "turn": 31, "at": now - 7 * 3600, "verified": False, "cost_units": 91000,
              "diff": {"files": 2, "add": None, "del": None, "source": "tool calls (cwd is not a git repo)", "commits": []},
              "tests": None, "prs": [], "files": [{"path": "paper08/fig3.py", "source": "tool"}, {"path": "paper08/out.log", "source": "bash redirect"}]}
    for x in items:
        x.setdefault("receipt", rc_ok if x["session_id"] == "s-1" else rc_bad if x["session_id"] == "s-2" else None)
        x.setdefault("gate_on", x["session_id"] in STATE.setdefault("gates", {"s-1": True}) and STATE["gates"][x["session_id"]])
        x.setdefault("final", fin)
        x.setdefault("final_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x["since"])))
        x.setdefault("last_prompt", "SYNTHETIC. carry on with the paper and tell me what blocks you")
    answered = {a["id"] for a in STATE["answers"]}
    return [x for x in items if x["id"] not in answered]


def proposals(now):
    if STATE["scenario"] != "normal":
        return []
    done = {a["id"] for a in STATE["answers"]}
    items = [(1, "state_doc", "State of play update: PROJ-08", "receipts, decisions or finals changed", "PROJ-08"),
             (2, "rule", "Proposed rule from your corrections", "2 scope corrections on 2026-10-03: Do exactly the scope asked", None),
             (3, "handoff", "Handoff: PROJ-10", "looks done: verified receipt and no open decisions", "PROJ-10")]
    return [{"id": f"proposal:{i}", "kind": "proposal", "proposal_id": i, "proposal_kind": k, "work_item": wi, "title": t, "text": w,
             "since": now - 3600 * i, "seconds": 3600 * i} for i, k, t, w, wi in items if f"proposal:{i}" not in done]


def overview(now):
    ny = needs(now)
    unknown = STATE["scenario"] == "missing"
    oldest = min(ny, key=lambda x: x["since"]) if ny else None
    return {"generated_at": now, "snapshot_at": now - 2, "health": "unknown" if unknown else "ok",
            "health_reasons": ["meter: meter log not found"] if unknown else [], "needs_you": ny,
            "longest_wait": {"id": oldest["id"], "seconds": oldest["seconds"], "kind": oldest["kind"], "title": oldest["title"]} if oldest else None,
            "counts": {"needs_you": len(ny), "decisions_open": sum(1 for x in ny if x["kind"] == "decision"),
                       "sessions_working": 5, "sessions_needs_you": 1, "sessions_idle": 9, "sessions_stopped": 3, "sessions": 18,
                       "limited": 1, "seats_blocked": 1},
            "capacity": {"generated_at": now, "seats": seats(now)}, "proposals": proposals(now)}


def graph(now):
    rnd = random.Random(7)
    nodes, edges = [], []
    for s in seats(now):
        nodes.append({"id": f"seat:{s['seat']}", "type": "seat", "label": s["label"], "state": s["state"], "seat": s["seat"],
                      "spend": s["five_hour"]["pct"], "parent": None, "excluded": s["excluded"]})
    n = 110 if STATE["scenario"] == "big" else 18
    states = ["working", "idle", "idle", "stopped", "working", "idle", "needs_you"]
    for wi, title in WIS:
        nodes.append({"id": f"work_item:{wi}", "type": "work_item", "label": f"{wi} {title}", "state": "idle", "seat": None,
                      "spend": 0, "parent": None})
    for i in range(n):
        wi = WIS[i % len(WIS)][0]
        st = states[(i * 3 + i // 7) % len(states)]
        sid = f"session:{SEATS[i % 7]}:s{i}"
        nodes.append({"id": sid, "type": "session", "label": f"SYNTHETIC chat {i} ({wi})", "state": st, "seat": SEATS[i % 7],
                      "spend": rnd.randint(0, 900), "parent": f"work_item:{wi}", "needs_you": st == "needs_you",
                      "limited": i == 3, "kind": "bg", "final": "SYNTHETIC final message for this chat.",
                      "activity": now - rnd.randint(0, 20000)})
        edges += [{"from": sid, "to": f"seat:{SEATS[i % 7]}", "kind": "runs_on"},
                  {"from": sid, "to": f"work_item:{wi}", "kind": "belongs_to"}]
    return {"generated_at": now, "nodes": nodes, "edges": edges}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ui(self, rel):
        rel = urllib.parse.unquote(rel).lstrip("/") or "index.html"
        root = os.path.realpath(UI)
        p = os.path.realpath(os.path.join(root, rel))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        data = open(p, "rb").read()
        if p.endswith(".html"):
            data = data.replace(b"__TOKEN__", TOKEN.encode())
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript",
                 ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
                 ".webmanifest": "application/manifest+json"}.get(os.path.splitext(p)[1], "application/octet-stream")
        self._send(200, data, ctype)

    def _api(self, method):
        u = urllib.parse.urlparse(self.path)
        if self.headers.get("X-Token") != TOKEN:
            return self._send(401, {"error": "token"})
        path = u.path[len("/api/cp/"):]
        now = time.time()
        if method == "POST" and path == "uploads":     # raw bytes, as the real route takes them; kept in a temp dir
            import tempfile
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "file")
            d = STATE.setdefault("updir", tempfile.mkdtemp(prefix="mock-up-"))
            fp = os.path.join(d, f"{len(os.listdir(d))}-{os.path.basename(name)}")
            open(fp, "wb").write(data)
            return self._send(200, {"ok": True, "path": fp, "name": name, "size": len(data), "image": name.lower().endswith((".png", ".jpg"))})
        if method == "GET" and path == "_replies":      # test hook: what the reply route received
            return self._send(200, {"replies": STATE.get("replies", [])})
        if method == "POST" and path.startswith("sessions/") and path.endswith("/reply"):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            STATE.setdefault("replies", []).append(body.get("text"))
            return self._send(200, {"ok": True})
        body = {}
        if method != "GET":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if method == "GET" and path == "overview":
            return self._send(200, overview(now))
        if method == "GET" and path == "workitems":
            home = next((x["seat"] for x in seats(now) if x["seat"] != "main"), None)
            return self._send(200, {"work_items": [{"id": f"PROJ-{n:02d}", "title": f"PROJ-{n:02d} Sample stream {n} - synthetic work item", "home_seat": home} for n in range(1, 7)]})
        if method == "GET" and path.startswith("workitems/") and path.endswith("/brief"):
            wi = path.split("/")[1]
            home = next((x["seat"] for x in seats(now) if x["seat"] != "main"), None)
            return self._send(200, {"brief": f"You are continuing work item {wi}. State of play: (synthetic mock brief, a few words so the box is not empty).", "home_seat": home})
        if method == "GET" and path == "capacity":
            return self._send(200, {"generated_at": now, "seats": seats(now)})
        if method == "GET" and path == "graph":
            return self._send(200, graph(now))
        if method == "GET" and path.startswith("sessions/") and path.endswith("/transcript"):
            sid = path.split("/")[1]
            sess = {"session_id": sid, "name": f"SYNTHETIC chat {sid[1:]}" if sid[:1] == "s" else sid, "seat": "alpha", "state": "idle", "live": True,
                    "kind": "bg", "work_item": "PROJ-08", "limited": False, "excluded": False, "kw": None, "cache_age_min": 4, "warmth": "warm",
                    "resume": {"pref": None, "global": "dry-run", "effective": "off", "available": True, "why": None}}
            entries = []
            for i in range(14):
                entries.append({"i": i, "kind": "user" if i % 2 == 0 else "text", "ts": now - (30 - i) * 60,
                                "text": (f"SYNTHETIC prompt {i // 2}: please continue." if i % 2 == 0 else
                                         f"SYNTHETIC answer {i // 2}.\n\n- first point about the work\n- second point\n\nThis paragraph is filler so the pane has enough text to scroll. " * 3)})
            entries.append({"i": 14, "kind": "tool", "name": "Bash", "summary": "ls -la", "input": "{\"command\": \"ls -la\"}", "result": "total 0", "ts": now - 60})
            if sid == "s-7":      # a call that is waiting on the approval
                entries.append({"i": 99, "kind": "tool", "name": "Monitor", "id": "t-7", "summary": "until grep -q FIN x.out",
                                "input": json.dumps({"command": "until grep -q FIN /tmp/x.out 2>/dev/null; do sleep 2; done; cat /tmp/x.out", "description": "SYNTHETIC wait for the result", "timeout_ms": 30000}),
                                "result": None, "error": False, "ts": now - 55})
            entries.append({"i": 15, "kind": "thinking", "text": "SYNTHETIC thought: the plan is to check the list first.", "ts": now - 50})
            for k in range(int(now // 4) % 1000):          # the transcript keeps growing, as a working chat does: every poll sees a change
                entries.append({"i": 16 + k, "kind": "text", "ts": now - 40 + k, "text": f"SYNTHETIC live line {k}"})
            return self._send(200, {"session": sess, "entries": entries, "start": 0, "total": len(entries), "counts": {"user": 7, "text": 7}})
        if method == "POST" and path.startswith("sessions/") and path.endswith("/seen"):
            return self._send(200, {"ok": True})
        if method == "GET" and path == "settings":
            return self._send(200, STATE["settings"])
        if method == "PUT" and path == "settings/focus":
            STATE["settings"]["focus"] = body
            return self._send(200, STATE["settings"])
        if method == "PUT" and path.startswith("gate/"):
            STATE.setdefault("gates", {})[path.split("/")[1]] = bool(body.get("on"))
            return self._send(200, {"ok": True, "on": bool(body.get("on"))})
        if method == "POST" and path.startswith("decisions/") and path.endswith("/answer"):
            did = path.split("/")[1]
            STATE["answers"].append({"id": f"decision:{did}", "body": body})
            return self._send(200, {"ok": True, "confirmed": True})
        if method == "POST" and path.startswith("sessions/") and path.endswith("/reply"):
            sid = path.split("/")[1]
            hit = next((x for x in needs(now) if x["session_id"] == sid), None)
            if hit:
                STATE["answers"].append({"id": hit["id"], "body": body})
            return self._send(200, {"ok": True, "confirmed": False, "route": "mock"})
        if method == "POST" and path == "sessions":      # spawn: recorded, never started
            STATE.setdefault("spawned", []).append(body)
            return self._send(200, {"ok": True, "queued": False, "job_id": "mock0001", "seat": body.get("seat")})
        if method == "POST" and path.startswith("sessions/") and path.endswith("/resume_pref"):
            STATE.setdefault("prefs", {})[path.split("/")[1]] = body.get("pref")
            return self._send(200, {"ok": True})
        if method == "POST" and path.startswith("proposals/") and path.rsplit("/", 1)[-1] in ("apply", "skip"):
            STATE["answers"].append({"id": "proposal:" + path.split("/")[1], "body": {"action": path.rsplit("/", 1)[-1]}})
            return self._send(200, {"ok": True, "state": path.rsplit("/", 1)[-1] + ("ed" if path.endswith("skip") else "lied")})
        if method == "POST" and path == "needs/dismiss":
            STATE["answers"].append({"id": body.get("id"), "body": {"dismissed": True}})
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found in mock"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/mock/state":
            return self._send(200, {"spawned": STATE.get("spawned", []), "prefs": STATE.get("prefs", {}), "answers": STATE["answers"]})
        if u.path == "/mock/scenario":
            STATE["scenario"] = urllib.parse.parse_qs(u.query).get("name", ["normal"])[0]
            STATE["answers"] = []; STATE["spawned"] = []; STATE["prefs"] = {}
            return self._send(200, {"scenario": STATE["scenario"]})
        if u.path.startswith("/api/cp/"):
            return self._api("GET")
        if u.path == "/":
            self.send_response(302); self.send_header("Location", "/v2/"); self.end_headers(); return
        if u.path.startswith("/v2"):
            return self._ui(u.path[3:])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self._api("POST")

    def do_PUT(self):
        self._api("PUT")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--scenario", default="normal")
    a = ap.parse_args()
    STATE["scenario"] = a.scenario
    print(f"mock on http://127.0.0.1:{a.port}/v2/ (scenario {a.scenario}, synthetic data)", flush=True)
    ThreadingHTTPServer.request_queue_size = 128   # default 5 resets Chrome's parallel module fetches (measured)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
