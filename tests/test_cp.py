"""cp (control plane) tests: banner parsing, seat state, needs-you classification, health, auth."""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatdash.cp import api, db, devserver, limits, resume, sources  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

CT = ZoneInfo("America/Chicago")


def ct(y, mo, d, h, mi=0):
    from datetime import datetime
    return datetime(y, mo, d, h, mi, tzinfo=CT).timestamp()


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    p = str(tmp_path / "cp.db")
    monkeypatch.setattr(db, "DB", p)
    db.init()
    return p


# ------------------------------------------------------------------ banners
def test_banner_session_same_day():
    b = limits.parse_banner("You've hit your session limit · resets 4:40pm (America/Chicago)", ct(2026, 10, 2, 13, 5))
    assert b["kind"] == "session" and b["resets_at"] == ct(2026, 10, 2, 16, 40)


def test_banner_rolls_to_next_day():
    b = limits.parse_banner("You've hit your session limit · resets 1:10am (America/Chicago)", ct(2026, 10, 2, 22, 0))
    assert b["resets_at"] == ct(2026, 10, 3, 1, 10)


def test_banner_bare_hour_and_weekly_date():
    assert limits.parse_banner("You've hit your session limit · resets 4am (America/Chicago)",
                               ct(2026, 10, 2, 1, 0))["resets_at"] == ct(2026, 10, 2, 4, 0)
    b = limits.parse_banner("You've hit your weekly limit · resets Sep 25 at 11am (America/Chicago)",
                            ct(2026, 9, 22, 9, 0))
    assert b["kind"] == "weekly" and b["resets_at"] == ct(2026, 9, 25, 11, 0)


def test_banner_inside_job_needs_text():
    b = limits.parse_banner("rate limited - wait and retry · You've hit your session limit · resets 5:20am "
                            "(America/Chicago)", ct(2026, 10, 2, 3, 0))
    assert b["resets_at"] == ct(2026, 10, 2, 5, 20)


def test_not_a_banner(tmp_path, monkeypatch):
    cfg = tmp_path / "tz.json"
    cfg.write_text(json.dumps({"timezone": "America/Chicago"}))
    monkeypatch.setenv("CHATDASH_CONFIG", str(cfg))
    assert limits.parse_banner("I hit a limit in the test fixture, resets nothing", time.time()) is None
    assert limits.fmt_clock(ct(2026, 10, 2, 16, 40)) == "4:40pm"
    assert limits.fmt_clock(ct(2026, 10, 2, 5, 0)) == "5am"


# ------------------------------------------------------------------ seats
def W(pct, resets=None, now=1000.0):
    return sources.window(pct, resets, now)


def test_seat_states():
    now = 1000.0
    assert sources.seat_state(W(None), W(None), None, now) == "unknown"          # no meter line at all
    assert sources.seat_state(W(20, 2000), W(None), None, now) == "unknown"      # 7d missing is not green
    assert sources.seat_state(W(20, 2000), W(30, 5000), None, now) == "ok"
    assert sources.seat_state(W(85, 2000), W(30, 5000), None, now) == "near"
    assert sources.seat_state(W(100, 2000), W(30, 5000), None, now) == "blocked"
    assert sources.seat_state(W(20, 2000), W(30, 5000), now + 60, now) == "blocked"   # live banner


def test_window_after_reset_is_unknown_not_zero():
    w = sources.window(97, 900.0, 1000.0)
    assert w["pct"] is None and w["since_reset"] is True


# ------------------------------------------------------------------ needs you
def chat(**kw):
    c = {"key": "work:s1", "account": "work", "config": "/x/.claude-work", "session_id": "s1", "kind": "bg",
         "state": "idle", "name": "PROJ-08 papers", "ws": "PROJ-08", "activity": 100.0, "banner": None,
         "excluded": False, "units_today": 3, "final": "done", "waiting_for": None, "pending_tool": None}
    c.update(kw)
    return c


def job(**kw):
    j = {"id": "j1", "config": "/x/.claude-work", "seat": "work", "state": "blocked", "needs": "go or no-go?",
         "detail": "", "suggested": "go", "name": "PROJ-08 papers", "session_id": "s2", "updated": 200.0,
         "banner": None}
    j.update(kw)
    return j


def snap(chats=(), jobs=(), seats=(), errors=None, at=None):
    return {"at": at or time.time(), "chats": list(chats), "jobs": list(jobs), "seats": list(seats),
            "fleet": [], "errors": errors or {}}


def test_needs_you_kinds_and_order(tmpdb):
    now = 10_000.0
    db.execute("INSERT INTO decisions(id, session_id, seat, question, asked_at, state, risk, options)"
               " VALUES('d1','s9','work','Merge #12?',50,'open','low','[]')")
    s = snap(chats=[chat(state="needs_you", session_id="s1", activity=300.0, live=True),
                    chat(key="work:s2", session_id="s2", state="idle", live=True)], jobs=[job()])
    ny = api.needs_you(s, now)
    assert [x["kind"] for x in ny] == ["decision", "blocked", "dialog"]       # oldest first
    assert ny[1]["suggested"] == "go"


def test_blocked_question_stays_until_answered_or_dismissed_even_if_the_chat_stopped(tmpdb):
    dead = chat(key="work:s2", session_id="s2", state="stopped", live=False, last_prompt_at="1970-01-01T00:01:00Z")
    ny = api.needs_you(snap(chats=[dead], jobs=[job()]), 10_000.0)            # prompt at 60 s, blocked at 200 s
    assert [x["kind"] for x in ny] == ["blocked"] and ny[0]["running"] is False
    answered = dict(dead, last_prompt_at="1970-01-01T00:05:00Z")            # prompt at 300 s, after the block
    assert api.needs_you(snap(chats=[answered], jobs=[job()]), 10_000.0) == []
    resumed_only = dict(answered, last_prompt=resume.RESUME_TEXT)           # our own resume line is not an answer
    assert len(api.needs_you(snap(chats=[resumed_only], jobs=[job()]), 10_000.0)) == 1
    api.dismiss("blocked:work:j1", 200.0, "PROJ-08 papers")
    assert api.needs_you(snap(chats=[dead], jobs=[job()]), 10_000.0) == []
    assert len(api.needs_you(snap(chats=[dead], jobs=[job(updated=500.0)]), 10_000.0)) == 1   # a new block shows again


def test_dismiss_route(tmpdb):
    src = type("S", (), {"get": lambda self: snap(jobs=[job()])})()
    assert api.handle("POST", "needs/dismiss", {}, {"id": "blocked:work:j1", "since": 200.0}, src)[0] == 200
    assert api.needs_you(snap(jobs=[job()]), 10_000.0) == []
    db.execute("INSERT INTO decisions(id, session_id, question, asked_at, state, options) VALUES('d1','s9','Q?',50,'open','[]')")
    assert api.handle("POST", "needs/dismiss", {}, {"id": "decision:d1"}, src)[0] == 200
    assert db.rows("SELECT state FROM decisions WHERE id='d1'")[0]["state"] == "dismissed"
    assert api.handle("POST", "needs/dismiss", {}, {"id": "rm:x"}, src)[0] == 400


def test_dead_limit_stall_drops_after_the_resume_window(tmpdb):
    now = 100_000.0
    old = {"kind": "session", "resets_at": now - resume.MAX_STALL_S - 60, "text": ""}
    recent = {"kind": "session", "resets_at": now - 900, "text": ""}
    assert api.needs_you(snap(jobs=[job(banner=old)]), now) == []
    assert [x["kind"] for x in api.needs_you(snap(jobs=[job(banner=recent)]), now)] == ["limit"]
    live = chat(key="work:s2", session_id="s2", live=True)
    assert [x["kind"] for x in api.needs_you(snap(chats=[live], jobs=[job(banner=old)]), now)] == ["limit"]


def _ask_transcript(tmp_path, answered: bool, error: bool = False):
    p = tmp_path / "s5.jsonl"
    recs = [{"type": "assistant", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": [
        {"type": "tool_use", "id": "toolu_abc", "name": "AskUserQuestion", "input": {"questions": []}}]}}]
    if answered:
        recs.append({"type": "user", "timestamp": "2026-10-03T00:05:00Z", "toolUseResult": {"answers": {"Which?": "B"}},
                     "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_abc", "is_error": error,
                                              "content": "The user answered: Which? B"}]}})
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(p)


def test_decision_answered_in_terminal_closes(tmpdb, tmp_path):
    from chatdash.cp import decisions
    t = _ask_transcript(tmp_path, answered=True)
    db.execute("INSERT INTO decisions(id, session_id, question, asked_at, state, options, tool_use_id, evidence)"
               " VALUES('s5-abc','s5','Which?',50,'open','[]','toolu_abc',?)", (json.dumps({"transcript": t}),))
    decisions._last_run[0] = -1e9
    ny = api.needs_you(snap(chats=[chat(key="work:s5", session_id="s5", live=True, path=t)]), 1000.0)
    assert ny == []
    r = db.rows("SELECT state, answered_by, delivery, answer FROM decisions WHERE id='s5-abc'")[0]
    assert (r["state"], r["answered_by"], r["delivery"]) == ("answered", "terminal", "answered in the terminal")
    assert json.loads(r["answer"]) == {"Which?": "B"}


def test_decision_unanswered_stays_open_even_after_session_ends(tmpdb, tmp_path):
    from chatdash.cp import decisions
    t = _ask_transcript(tmp_path, answered=False)
    db.execute("INSERT INTO decisions(id, session_id, question, asked_at, state, options, tool_use_id, evidence)"
               " VALUES('s5-abc','s5','Which?',50,'open','[]','toolu_abc',?)", (json.dumps({"transcript": t}),))
    decisions._last_run[0] = -1e9
    live = chat(key="work:s5", session_id="s5", live=True, path=t)
    assert [x["kind"] for x in api.needs_you(snap(chats=[live]), 10_000.0)] == ["decision"]
    decisions._last_run[0] = -1e9
    dead = dict(live, live=False, state="stopped")
    ny = api.needs_you(snap(chats=[dead]), 10_000.0)
    assert [x["kind"] for x in ny] == ["decision"] and ny[0]["running"] is False       # unanswered stays
    assert db.rows("SELECT state FROM decisions WHERE id='s5-abc'")[0]["state"] == "open"


def test_limit_stall_is_not_needs_you_until_reset_passed(tmpdb):
    now = 10_000.0
    future = {"kind": "session", "resets_at": now + 3600, "text": ""}
    past = {"kind": "session", "resets_at": now - 900, "text": ""}
    ny = api.needs_you(snap(jobs=[job(banner=future)]), now)
    assert ny == []
    ny = api.needs_you(snap(jobs=[job(banner=past)]), now)
    assert [x["kind"] for x in ny] == ["limit"]


def test_decision_hides_duplicate_dialog(tmpdb):
    db.execute("INSERT INTO decisions(id, session_id, question, asked_at, state, options)"
               " VALUES('d1','s1','Which?',50,'open','[]')")
    ny = api.needs_you(snap(chats=[chat(state="needs_you")]), 1000.0)
    assert [x["kind"] for x in ny] == ["decision"]


def test_dialog_item_carries_the_terminal_options_and_answers_by_number(tmpdb):
    scr = {"kind": "permission", "question": "Do you want to create x.txt?", "tabs": None, "cursor": 1,
           "options": [{"n": 1, "label": "Yes", "desc": "", "free": False},
                       {"n": 2, "label": "Yes, and always allow access to /Users/demo for this session", "desc": "", "free": False},
                       {"n": 3, "label": "No", "desc": "", "free": False}]}
    s = snap(chats=[chat(state="needs_you", dialog=scr), chat(key="main:s9", session_id="s9", state="needs_you",
                                                              excluded=True, dialog=scr)])
    ny = api.needs_you(s, 1000.0)
    assert [x["screen"] for x in ny] == [scr, scr]           # exactly what the screen shows, never generated
    got = []
    ctx = {"dialog": lambda c, n, label, text="": got.append((c["session_id"], n, label, text)) or {"ok": True}}
    body = {"n": 2, "label": scr["options"][1]["label"]}
    assert api.handle("POST", "sessions/s1/dialog", {}, body, FakeSrc(s), ctx)[0] == 200
    assert got == [("s1", 2, scr["options"][1]["label"], "")]
    assert api.handle("POST", "sessions/s9/dialog", {}, body, FakeSrc(s), ctx)[0] == 403
    assert api.handle("POST", "sessions/s1/dialog", {}, {}, FakeSrc(s), ctx)[0] == 400
    assert api.handle("POST", "sessions/s1/dialog", {}, body, FakeSrc(s), {})[0] == 501
    assert len(got) == 1


def test_health_unknown_on_errors_and_stale(tmpdb):
    now = time.time()
    seat_ok = {"seat": "work", "state": "ok"}
    assert api.health(snap(seats=[seat_ok], at=now), now)["state"] == "ok"
    assert api.health(snap(seats=[seat_ok], errors={"meter": "unreadable"}, at=now), now)["state"] == "unknown"
    assert api.health(snap(seats=[seat_ok], at=now - 600), now)["state"] == "unknown"
    assert api.health(snap(seats=[{"seat": "alpha", "state": "unknown"}], at=now), now)["state"] == "unknown"


def test_graph_edges_reference_nodes(tmpdb):
    seats = [{"seat": "work", "label": "Work", "state": "ok", "five_hour": {"pct": 10}, "excluded": False}]
    g = api.graph(snap(chats=[chat(), chat(key="work:s3", session_id="s3", kind="headless", state="stopped")],
                       seats=seats), 1000.0)
    ids = {n["id"] for n in g["nodes"]}
    assert "session:work:s1" in ids and "session:work:s3" not in ids and "work_item:PROJ-08" in ids
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])


def test_excluded_seat(tmp_path, monkeypatch):
    assert not sources.excluded(os.path.expanduser("~/.claude"))           # nothing is read-only by default
    assert sources.excluded(None)                                          # no config dir: nothing to act on
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"read_only_accounts": ["main", "audit"]}))
    monkeypatch.setenv("CHATDASH_CONFIG", str(cfg))
    assert sources.excluded(os.path.expanduser("~/.claude"))
    assert sources.excluded(os.path.expanduser("~/.claude-audit"))
    assert not sources.excluded(os.path.expanduser("~/.claude-work"))


def test_mode_defaults_to_dry_run(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text('{"limit_resume": "on", "x": "bogus"}')
    monkeypatch.setattr(db, "CONFIG", str(p))
    assert db.mode("limit_resume") == "on" and db.mode("x") == "dry-run" and db.mode("missing") == "dry-run"
    p.write_text("not json")
    assert db.mode("limit_resume") == "dry-run"


def test_every_auto_action_defaults_to_dry_run(tmp_path, monkeypatch):
    """No config file is shipped: with none, every auto-action the page lists is dry-run (never on)."""
    monkeypatch.setenv("CHATDASH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(db, "CONFIG", None)
    assert api.MODE_DOCS and all(db.mode(k) == "dry-run" for k in api.MODE_DOCS)


# ------------------------------------------------------------------ dev server
def test_ui_file_blocks_traversal(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("x")
    monkeypatch.setattr(devserver, "UI_DIR", str(tmp_path))
    assert devserver.ui_file("/") == str(tmp_path / "index.html")
    assert devserver.ui_file("/../../etc/passwd") is None
    assert devserver.ui_file("/%2e%2e/secret") is None


class FakeSrc:
    def __init__(self, s):
        self.s = s

    def get(self):
        return self.s


def test_devserver_auth_and_host(tmpdb):
    src = FakeSrc(snap(seats=[{"seat": "work", "label": "Work", "state": "ok", "five_hour": {"pct": 1},
                               "seven_day": {"pct": 1}, "excluded": False}]))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), devserver.make_handler(src, "tok", 0))
    port = srv.server_address[1]
    srv.RequestHandlerClass = devserver.make_handler(src, "tok", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        assert get("/api/cp/overview", {})[0] == 401
        assert get("/api/cp/overview", {"X-Token": "tok", "Host": "evil.example"})[0] == 403
        code, body = get("/api/cp/overview", {"X-Token": "tok"})
        assert code == 200 and body["health"] == "ok" and body["counts"]["needs_you"] == 0
        assert get("/api/cp/receipts", {"X-Token": "tok"})[0] == 400
    finally:
        srv.shutdown()


def test_graph_drops_old_fleet_jobs_and_marks_stale_unknown(tmpdb):
    now = 100_000.0
    seats = [{"seat": "beta", "label": "Beta", "state": "ok", "five_hour": {"pct": 1}, "excluded": False}]
    s = snap(seats=seats)
    s["fleet"] = [{"id": "old", "status": "working", "account": None, "at": now - 30 * 3600},
                  {"id": "stale", "status": "working", "account": ".claude-beta", "at": now - 7 * 3600},
                  {"id": "fresh", "status": "working", "account": ".claude-beta", "at": now - 60}]
    g = api.graph(s, now)
    jobs = {n["id"]: n for n in g["nodes"] if n["type"] == "job"}
    assert set(jobs) == {"job:stale", "job:fresh"}
    assert jobs["job:stale"]["state"] == "unknown" and jobs["job:fresh"]["state"] == "working"
    assert {"from": "job:fresh", "to": "seat:beta", "kind": "runs_on"} in g["edges"]


def test_meter_keeps_last_reading_of_an_omitted_window(tmp_path):
    log = tmp_path / "meter.log"
    log.write_text("2026-10-02T07:25:24Z\t/u/.claude-echo\tsid\t{\"five_hour\":{\"used_percentage\":40,\"resets_at\":1000},"
                   "\"seven_day\":{\"used_percentage\":88,\"resets_at\":9000}}\n"
                   "2026-10-02T10:20:01Z\t/u/.claude-echo\tsid\t{\"seven_day\":{\"used_percentage\":89,\"resets_at\":9000}}\n")
    m = sources.Meter(str(log)).read()["/u/.claude-echo"]
    assert m["five"] == 40 and m["five_resets"] == 1000 and m["seven"] == 89 and m["five_omitted"]
    w = sources.window(m["five"], m["five_resets"], 2000)
    assert w["pct"] is None and w["since_reset"] is True


def test_put_modes_writes_config_atomically_and_validates(tmpdb, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_doc": "keep me", "limit_resume": "dry-run", "decision_hold_s": 630}))
    monkeypatch.setattr(db, "CONFIG", str(cfg))
    assert api.put_modes({"limit_resume": "on"}) is None
    d = json.loads(cfg.read_text())
    assert d["limit_resume"] == "on" and d["_doc"] == "keep me" and d["decision_hold_s"] == 630
    assert db.mode("limit_resume") == "on"
    assert api.put_modes({"limit_resume": "yes"}) and api.put_modes({"rm_rf": "on"})
    assert json.loads(cfg.read_text())["limit_resume"] == "on"               # rejected writes change nothing
    assert db.rows("SELECT decision FROM auto_log WHERE action='mode_change'")[0]["decision"] == "limit_resume: dry-run -> on"
    code, out = api.handle("PUT", "settings/modes", {}, {"stop_gate": "off"}, type("S", (), {"get": lambda self: snap()})())
    assert code == 200 and next(m for m in out["modes"] if m["key"] == "stop_gate")["mode"] == "off"


def test_transcript_entries_include_thinking_tools_and_results(tmp_path):
    from chatdash.cp import transcript
    p = tmp_path / "t.jsonl"
    recs = [
        {"type": "user", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": "fix the bug"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "assistant", "timestamp": "2026-10-03T00:00:01Z", "message": {"content": [
            {"type": "thinking", "thinking": "look at the code first"},
            {"type": "text", "text": "Reading it."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "timestamp": "2026-10-03T00:00:02Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent"}]}},
        {"type": "assistant", "isApiErrorMessage": True, "timestamp": "2026-10-03T00:00:03Z",
         "message": {"content": [{"type": "text", "text": "You've hit your session limit"}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    pg = transcript.page(str(p))
    kinds = [e["kind"] for e in pg["entries"]]
    assert kinds == ["user", "thinking", "text", "tool", "notice"]
    tool = pg["entries"][3]
    assert tool["summary"] == "pytest -q" and tool["result"] == "3 passed" and not tool["error"]
    assert transcript.page(str(p), before=2, limit=1)["entries"][0]["kind"] == "thinking"


def test_sessions_list_and_transcript_route(tmpdb, tmp_path):
    p = tmp_path / "s1.jsonl"
    p.write_text(json.dumps({"type": "user", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": "hi"}}) + "\n")
    s = snap(chats=[chat(path=str(p), live=True, state="working"), chat(key="work:s3", session_id="s3", state="stopped", path=str(p))])
    src = type("S", (), {"get": lambda self: s})()
    code, out = api.handle("GET", "sessions", {}, {}, src)
    assert code == 200 and [r["state"] for r in out["sessions"]] == ["working", "stopped"]
    code, out = api.handle("GET", "sessions/s1/transcript", {}, {}, src)
    assert code == 200 and out["total"] == 1 and out["session"]["live"] is True
    assert api.handle("GET", "sessions/nope/transcript", {}, {}, src)[0] == 404


def test_pace_setting_default_and_validation(tmpdb):
    assert db.settings()["pace"]["pct_per_day"] == 14.3
    assert api.validate_setting("pace", {"pct_per_day": 15}) is None
    assert api.validate_setting("pace", {"pct_per_day": 50}) and api.validate_setting("pace", {"pct_per_day": "x"})
    src = type("S", (), {"get": lambda self: snap()})()
    code, out = api.handle("PUT", "settings/pace", {}, {"pct_per_day": 15}, src)
    assert code == 200 and out["pace"]["pct_per_day"] == 15


def _iso(t):
    import datetime as _d
    return _d.datetime.fromtimestamp(t, _d.timezone.utc).isoformat().replace("+00:00", "Z")


def test_blocked_job_rewritten_after_dev_answered_is_not_a_question(tmpdb):
    # 2026-10-03 PROJ-16: blocked at 4:14 with a real question, the user answered at 4:18:20, then Claude Code kept the job
    # "blocked", copied the answer into detail and bumped updatedAt to 4:18:29. The card must not come back.
    now = 10_000.0
    c = chat(session_id="s2", key="work:s2", live=True, last_prompt="Yes, go ahead", last_prompt_at=_iso(9_000.0))
    j = job(needs=None, detail="Yes, go ahead", updated=9_009.0, blocked_since=8_700.0)
    assert api.needs_you(snap(chats=[c], jobs=[j]), now) == []
    # no timeline: the echo of the user's own last prompt is still recognised as an answer, not a question
    assert api.needs_you(snap(chats=[c], jobs=[job(needs=None, detail="Yes, go ahead", updated=9_009.0)]), now) == []
    # a real question asked after the answer still shows, timed from when it was asked
    j2 = job(needs="Run on production now?", updated=9_600.0, blocked_since=9_500.0)
    ny = api.needs_you(snap(chats=[c], jobs=[j2]), now)
    assert [x["kind"] for x in ny] == ["blocked"] and ny[0]["since"] == 9_500.0


def test_blocked_since_from_timeline(tmp_path):
    t = tmp_path / "timeline.jsonl"
    rows = [{"at": "2026-10-03T21:14:25.497Z", "state": "working"},
            {"at": "2026-10-03T21:14:43.355Z", "state": "blocked", "detail": "awaiting permission"},
            {"at": "2026-10-03T21:18:20.196Z", "state": "blocked", "detail": "Yes, I provide you yes"}]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    from chatdash.extract import iso_epoch
    assert sources.blocked_since(str(t)) == iso_epoch("2026-10-03T21:14:43.355Z")
    assert sources.blocked_since(str(tmp_path / "missing.jsonl")) is None


def test_chat_that_replied_after_devs_prompt_and_is_still_blocked_asks_again(tmpdb):
    # blocked at 200 s; the user typed at 300 s (a side remark); the chat replied at 400 s and its job is still blocked
    c = chat(key="work:s2", session_id="s2", state="idle", live=True,
             last_prompt_at="1970-01-01T00:05:00Z", final_at="1970-01-01T00:06:40Z")
    ny = api.needs_you(snap(chats=[c], jobs=[job()]), 10_000.0)
    assert [x["kind"] for x in ny] == ["blocked"] and ny[0]["since"] == 400            # the renewed ask, from the reply
    working = dict(c, state="working")                                               # still working on the user's prompt
    assert api.needs_you(snap(chats=[working], jobs=[job()]), 10_000.0) == []
    not_replied = dict(c, final_at="1970-01-01T00:04:00Z")                          # last reply before the user's prompt
    assert api.needs_you(snap(chats=[not_replied], jobs=[job()]), 10_000.0) == []
    api.dismiss("blocked:work:j1", 200.0, "old ask")                                 # dismissing the old ask
    assert len(api.needs_you(snap(chats=[c], jobs=[job()]), 10_000.0)) == 1          # does not hide the new one
    api.dismiss("blocked:work:j1", 400, "new ask")
    assert api.needs_you(snap(chats=[c], jobs=[job()]), 10_000.0) == []
