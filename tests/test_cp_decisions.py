"""Decision inbox: parsing, risk, answer validation, hook hold / release race, dialog fallback."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatdash.cp import db, decisions, risk  # noqa: E402
from chatdash.cp.hooks import ask_hook  # noqa: E402

HOME = os.path.expanduser("~")
BG_ENV = {"CLAUDE_CONFIG_DIR": HOME + "/.claude-work", "CLAUDE_JOB_DIR": HOME + "/.claude-work/jobs/abcd1234"}


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    p = str(tmp_path / "cp.db")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"decision_hook": "on", "decision_hold_s": 5, "read_only_accounts": ["main"]}))
    monkeypatch.setattr(db, "DB", p)
    monkeypatch.setattr(db, "CONFIG", str(cfg))
    monkeypatch.setenv("CHATDASH_CONFIG", str(cfg))
    db.init()
    return cfg


def payload(questions, sid="11111111-2222", tuid="toolu_abcdef123456"):
    return {"session_id": sid, "tool_use_id": tuid, "tool_name": "AskUserQuestion",
            "transcript_path": "/x.jsonl", "cwd": "/x", "tool_input": {"questions": questions}}


Q_LOW = [{"question": "Which date format?", "header": "Format", "multiSelect": False,
          "options": [{"label": "ISO (Recommended)"}, {"label": "US"}]}]
Q_HIGH = [{"question": "Deploy the worker now?", "header": "Deploy", "multiSelect": False,
           "options": [{"label": "Yes (Recommended)"}, {"label": "No"}]}]
Q_TWO = Q_LOW + [{"question": "Color theme?", "header": "Theme", "multiSelect": True,
                  "options": [{"label": "Dark"}, {"label": "Light"}]}]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_hook(p, env=BG_ENV, clock=None, on_sleep=None):
    clock = clock or Clock()
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out

    def sleep(s):
        clock.sleep(s)
        if on_sleep:
            on_sleep(clock.t)
    try:
        ask_hook.main(stdin=io.StringIO(json.dumps(p)), env=env, sleep=sleep, clock=clock)
    finally:
        sys.stdout = old
    return out.getvalue()


# ------------------------------------------------------------------ parsing and risk
def test_parts_and_recommended():
    parts = decisions.parts_from_input({"questions": Q_TWO})
    assert [p["recommended"] for p in parts] == ["1", None]
    assert parts[1]["multi"] is True and parts[0]["options"][1] == {"id": "2", "label": "US", "desc": ""}


def test_risk_rules_and_raise_only():
    assert risk.classify("Which date format?")[0] == "low"
    assert risk.classify("Deploy the worker now?")[0] == "high"
    assert risk.classify("Commit this?")[0] == "med"
    assert risk.classify("Which font? risk: high")[0] == "high"        # chat may raise
    assert risk.classify("Delete the bucket? risk: low")[0] == "high"  # never lower


def test_answers_require_every_part():
    parts = decisions.parts_from_input({"questions": Q_TWO})
    assert decisions.answers_for(parts, {"option_id": "1"})[1].startswith("this decision has 2 parts")
    assert decisions.answers_for(parts, {"answers": {"0": "1"}})[1] == "part 2 is not answered"
    a, err = decisions.answers_for(parts, {"answers": {"0": "2", "1": ["1", "2"]}})
    assert err is None and a == {"Which date format?": "US", "Color theme?": "Dark, Light"}
    assert decisions.answers_for(parts, {"answers": {"0": ["1", "2"], "1": "1"}})[1] == "part 1 takes one option"
    a, _ = decisions.answers_for(parts, {"answers": {"0": {"text": "RFC 3339"}, "1": "2"}})
    assert a["Which date format?"] == "RFC 3339"


# ------------------------------------------------------------------ hook
def test_hook_skips_read_only_account_terminal_and_other_tools(tmp):
    assert run_hook(payload(Q_LOW), env={"CLAUDE_JOB_DIR": "/j"}) == ""                         # unset dir = ~/.claude = main, read-only here
    assert run_hook(payload(Q_LOW), env={**BG_ENV, "CLAUDE_CONFIG_DIR": HOME + "/.claude"}) == ""
    assert run_hook(payload(Q_LOW), env={"CLAUDE_CONFIG_DIR": BG_ENV["CLAUDE_CONFIG_DIR"]}) == ""  # terminal
    p = payload(Q_LOW)
    p["tool_name"] = "Bash"
    assert run_hook(p) == ""
    assert db.rows("SELECT * FROM decisions") == []


def test_hook_returns_devs_answer(tmp):
    did = decisions.decision_id("11111111-2222", "toolu_abcdef123456", decisions.parts_from_input({"questions": Q_HIGH}))

    def dev_answers(t):
        if t >= 1002:
            decisions.take_if_held(did, {"Deploy the worker now?": "No"})
    out = json.loads(run_hook(payload(Q_HIGH), on_sleep=dev_answers))
    hs = out["hookSpecificOutput"]
    assert hs["permissionDecision"] == "allow"
    assert hs["updatedInput"] == {"questions": Q_HIGH, "answers": {"Deploy the worker now?": "No"}}
    d = decisions.get(did)
    assert d["state"] == "answered" and d["delivered"] == 1 and d["delivery"] == "hook" and d["risk"] == "high"


def test_hook_low_risk_takes_recommended_on_timeout(tmp):
    out = json.loads(run_hook(payload(Q_LOW)))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {"Which date format?": "ISO (Recommended)"}
    d = db.rows("SELECT * FROM decisions")[0]
    assert d["answered_by"] == "timeout-default" and d["held"] == 0


def test_hook_high_risk_times_out_to_dialog(tmp):
    assert run_hook(payload(Q_HIGH)) == ""
    d = db.rows("SELECT * FROM decisions")[0]
    assert d["state"] == "open" and d["held"] == 0 and d["on_timeout"] == "dialog"


def test_answer_after_release_goes_to_dialog(tmp):
    run_hook(payload(Q_HIGH))
    did = db.rows("SELECT id FROM decisions")[0]["id"]
    typed = []
    code, res = decisions.answer(did, {"option_id": "2"}, {"key": "k"}, lambda c, n, label: typed.append((n, label)) or {"ok": True})
    assert code == 200 and res["route"] == "dialog" and typed == [(2, "No")]
    assert decisions.get(did)["delivery"] == "dialog"
    code, _ = decisions.answer(did, {"option_id": "1"}, {"key": "k"}, lambda *a: {"ok": True})
    assert code == 409                                  # already answered


def test_multi_part_after_release_refuses_dialog(tmp):
    tmp.write_text(json.dumps({"decision_hook": "dry-run"}))
    run_hook(payload(Q_TWO))
    d = db.rows("SELECT * FROM decisions")[0]
    assert d["held"] == 0 and d["mode"] == "dry-run"
    code, res = decisions.answer(d["id"], {"answers": {"0": "1", "1": "2"}}, {"key": "k"}, lambda *a: {"ok": True})
    assert code == 409 and "several parts" in res["error"]


def test_race_answer_wins_over_release(tmp):
    parts = decisions.parts_from_input({"questions": Q_HIGH})
    row = decisions.create(payload(Q_HIGH), mode="on", hold_s=5, seat="work", config="/c", job_id="j")
    assert decisions.take_if_held(row["id"], {"Deploy the worker now?": "Yes (Recommended)"})
    assert decisions.release(row["id"]) is False         # hook sees the answer instead of releasing
    assert decisions.take_if_held(row["id"], {"x": "y"}) is False
    assert parts


def test_hook_off_mode_writes_nothing(tmp):
    tmp.write_text(json.dumps({"decision_hook": "off"}))
    assert run_hook(payload(Q_LOW)) == ""
    assert db.rows("SELECT * FROM decisions") == []


def test_hook_fails_open_on_garbage():
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        assert ask_hook.main(stdin=io.StringIO("not json"), env=BG_ENV) == 0
    finally:
        sys.stdout = old
    assert out.getvalue() == ""


# ------------------------------------------------------------------ replies (send.Sender)
from chatdash.cp import send  # noqa: E402


class Src:
    def __init__(self, chats):
        self.chats = chats

    def get(self):
        return {"chats": self.chats}


def chat(**kw):
    c = {"key": "work:s1", "session_id": "s1", "account": "work", "config": HOME + "/.claude-work",
         "state": "idle", "kind": "bg", "excluded": False}
    c.update(kw)
    return c


def test_reply_refuses_main_seat(tmp):
    s = send.Sender(Src([chat(config=HOME + "/.claude", excluded=True)]), reply_fn=lambda c, t: {"ok": True})
    assert s.reply("s1", "go")[0] == 403


def test_reply_queues_while_working_then_drains(tmp):
    src = Src([chat(state="working")])
    sent = []
    s = send.Sender(src, reply_fn=lambda c, t: sent.append(t) or {"ok": True, "confirmed": True, "route": "attach"})
    code, res = s.reply("s1", "go ahead")
    assert code == 200 and res["queued"] and sent == []
    assert s.drain() == []                       # still working
    src.chats[0]["state"] = "idle"
    out = s.drain()
    assert sent == ["go ahead"] and out[0]["confirmed"] is True and s.queue == {}


def test_reply_failure_is_reported_not_swallowed(tmp):
    s = send.Sender(Src([chat()]), reply_fn=lambda c, t: {"ok": False, "error": "dialog showing"})
    code, res = s.reply("s1", "go")
    assert code == 409 and res["error"] == "dialog showing"
    assert db.rows("SELECT decision FROM auto_log")[-1]["decision"] == "failed"


def test_reply_validation(tmp):
    s = send.Sender(Src([chat()]), reply_fn=lambda c, t: {"ok": True})
    assert s.reply("s1", "   ")[0] == 400 and s.reply("nope", "x")[0] == 404
