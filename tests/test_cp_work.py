"""A5 work items: state-doc proposals, handoff, spawn queue, collisions."""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import db, receipts, work  # noqa: E402

HOME = os.path.expanduser("~")


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"handoff": "dry-run"}))
    monkeypatch.setattr(db, "CONFIG", str(cfg))
    monkeypatch.setattr(work, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(work, "HISTORY", str(tmp_path / "state" / ".history"))
    monkeypatch.setenv("DHI_ORBIT_CONFIG", str(tmp_path / "orbit.json"))
    (tmp_path / "orbit.json").write_text(json.dumps({"read_only_accounts": ["main"]}))
    os.makedirs(tmp_path / "state")
    db.init()
    work.init()
    db.execute("INSERT INTO work_items(id, title, workstream) VALUES('PROJ-10', 'Repos', 'PROJ-10')")
    return tmp_path


def chat(**kw):
    c = {"key": "work:s1", "session_id": "s1", "account": "work", "config": HOME + "/.claude-work", "kind": "bg",
         "state": "idle", "excluded": False, "name": "PROJ-10 Repos", "ws": "PROJ-10", "final": "All merged.\nresult: PR #12 merged",
         "final_at": "2026-10-02T10:00:00Z", "activity": 1000.0, "banner": None}
    c.update(kw)
    return c


def rc(sid="s1", verified=True, files=(("/r/a.py", "git"),)):
    return {"turn": 3, "at": 1000.0, "cwd": "/r", "diff": {"files": len(files), "add": 1, "del": 0, "source": "git"},
            "tests": {"cmd": "pytest", "last_line": "3 passed", "exit": 0}, "checks": [], "prs": [],
            "files": [{"path": p, "source": s} for p, s in files], "cost_units": 10}


SNAP = lambda chats: {"chats": chats, "seats": [{"seat": "work", "state": "ok"}, {"seat": "alpha", "state": "near"}], "jobs": []}


def test_state_doc_proposal_keeps_devs_text_and_applies_with_history(tmp):
    p = work.state_path("PROJ-10")
    open(p, "w").write("kind: handoff\nnext: ship it\n")
    receipts.store("s1", "/c", rc(), True)
    prop = work.propose_state("PROJ-10", SNAP([chat()]))
    assert prop and work.AUTO_START in prop["new"] and prop["new"].startswith("kind: handoff\nnext: ship it\n")
    assert work.propose_state("PROJ-10", SNAP([chat()])) is None          # same facts: no new proposal
    full = work.proposal(prop["id"])
    assert "+cp_auto:" in full["diff"]
    assert work.decide(prop["id"], "apply", SNAP([]))[0] == 200
    assert open(p).read() == prop["new"]
    assert len(os.listdir(work.HISTORY)) == 1
    again = work.merged_doc(prop["new"], work.AUTO_START + "\nX\n" + work.AUTO_END)
    assert again.count(work.AUTO_START) == 1 and "next: ship it" in again


def test_stale_proposal_is_not_written(tmp):
    p = work.state_path("PROJ-10")
    open(p, "w").write("v1\n")
    prop = work.propose_state("PROJ-10", SNAP([chat()]), force=True)
    open(p, "w").write("v2 edited by the user\n")
    code, res = work.decide(prop["id"], "apply", SNAP([]))
    assert code == 409 and open(p).read() == "v2 edited by the user\n"
    assert work.proposal(prop["id"])["state"] == "stale"


def test_skip(tmp):
    prop = work.propose_state("PROJ-10", SNAP([chat()]), force=True)
    assert work.decide(prop["id"], "skip", SNAP([]))[1]["state"] == "skipped"
    assert work.decide(prop["id"], "apply", SNAP([]))[0] == 409


def test_looks_done_needs_all_three(tmp):
    assert work.looks_done(chat())[0] is False                            # no receipt yet
    receipts.store("s1", "/c", rc(), True)
    assert work.looks_done(chat()) == (True, "verified receipt, no open decisions, final says done")
    assert work.looks_done(chat(final="Working on the next step"))[0] is False
    assert work.looks_done(chat(final="The result: is unclear, still digging"))[0] is False   # mid-line result: is not a result line
    db.execute("INSERT INTO decisions(id, session_id, question, state) VALUES('d1','s1','q','open')")
    assert work.looks_done(chat())[0] is False
    assert work.looks_done(chat(excluded=True))[0] is False


def test_handoff_proposal_once_and_dry_run_confirm_sends_nothing(tmp):
    p = work.propose_handoff(chat(), "You marked it done")
    assert p and work.propose_handoff(chat(), "again") is None
    sent = []
    code, res = work.decide(p["id"], "apply", SNAP([chat()]), sender=lambda c, t: sent.append(t), stopper=lambda c: {"ok": True})
    assert code == 200 and res["sent"] is False and sent == []
    assert db.rows("SELECT decision FROM auto_log WHERE action='handoff'")[0]["decision"] == "would send"


def test_handoff_on_mode_sends_then_stops_after_note(tmp, monkeypatch):
    (tmp / "config.json").write_text(json.dumps({"handoff": "on"}))
    p = work.propose_handoff(chat(), "You marked it done")
    path = p["target"]
    stopped = []

    def sender(c, text):
        assert text == "## handoff text"
        open(path, "a").write("\n## handoff 2026-10-02 5pm CT (s1)\ndone: x\n")
        return {"ok": True}
    monkeypatch.setattr(work.time, "sleep", lambda s: None)
    work._run_handoff(work.proposal(p["id"]), chat(), "## handoff text", sender, lambda c: stopped.append(1) or {"ok": True})
    assert stopped == [1] and work.proposal(p["id"])["state"] == "applied"


class R:
    def __init__(self, out="Session backgrounded\n  claude attach 1a2b3c4d\n", code=0):
        self.calls, self.out, self.code = [], out, code

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")


def test_spawn_refuses_main_queues_near_and_starts_ok(tmp):
    r = R()
    assert work.spawn({"work_item": "PROJ-10", "seat": "main", "brief": "x"}, SNAP([]), runner=r)[0] == 403
    code, res = work.spawn({"work_item": "PROJ-10", "seat": "alpha", "brief": "do x"}, SNAP([]), runner=r)
    assert code == 200 and res["queued"] and r.calls == []
    code, res = work.spawn({"work_item": "PROJ-10", "seat": "work", "brief": "do y", "interview": True}, SNAP([]), runner=r)
    assert code == 200 and res["job_id"] == "1a2b3c4d"
    cmd, kw = r.calls[0]
    assert kw["env"]["CLAUDE_CONFIG_DIR"].replace("\\", "/").endswith("/.claude-work") and "CLAUDE_JOB_DIR" not in kw["env"]
    assert cmd[-1].startswith("Before any work: interview the user") and cmd[-1].endswith("do y")
    seats_ok = {"chats": [], "seats": [{"seat": "alpha", "state": "ok"}]}
    assert work.drain_queue(seats_ok, runner=r)[0]["job_id"] == "1a2b3c4d"
    assert db.rows("SELECT state FROM cp_spawn_queue")[0]["state"] == "started"



def test_fresh_unknown_seat_starts_at_once_and_near_or_blocked_still_queue(tmp):
    r = R()
    seat = lambda state, **kw: {"chats": [], "jobs": [], "seats": [dict({"seat": "fresh", "state": state}, **kw)]}
    code, res = work.spawn({"seat": "fresh", "brief": "pong"}, seat("unknown", five_hour={"pct": None}, seven_day={"pct": None}), runner=r)
    assert code == 200 and res["queued"] is False and res["job_id"] == "1a2b3c4d" and len(r.calls) == 1
    assert db.rows("SELECT * FROM cp_spawn_queue") == []
    for state in ("near", "blocked"):
        code, res = work.spawn({"seat": "fresh", "brief": "pong"}, seat(state), runner=r)
        assert code == 200 and res["queued"] is True
    # a partial reading already near the limit (seven_day not read yet) is still held back
    code, res = work.spawn({"seat": "fresh", "brief": "pong"}, seat("unknown", five_hour={"pct": 91}, seven_day={"pct": None}), runner=r)
    assert res["queued"] is True and len(r.calls) == 1 and len(db.rows("SELECT * FROM cp_spawn_queue")) == 3


def test_drain_starts_a_queued_job_on_a_seat_with_no_reading_but_not_a_blocked_one(tmp):
    r = R()
    db.execute("INSERT INTO cp_spawn_queue(work_item, seat, brief, interview, created_at) VALUES(NULL,'fresh','pong',0,1)")
    assert work.drain_queue({"seats": [{"seat": "fresh", "state": "blocked"}]}, runner=r) == [] and r.calls == []
    assert work.drain_queue({"seats": [{"seat": "fresh", "state": "unknown"}]}, runner=r)[0]["job_id"] == "1a2b3c4d"
    assert db.rows("SELECT state FROM cp_spawn_queue")[0]["state"] == "started"


def test_spawn_says_when_the_folder_was_not_used(tmp, monkeypatch):
    home = tmp / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setattr(work, "HOME", str(home))
    r = R()
    snap = {"chats": [], "jobs": [], "seats": [{"seat": "work", "state": "ok"}, {"seat": "alpha", "state": "near"}]}
    code, res = work.spawn({"seat": "work", "brief": "x", "cwd": "/root/cdtest"}, snap, runner=r)
    assert code == 200 and "/root/cdtest" in res["note"] and "was not used" in res["note"]
    assert r.calls[0][1]["cwd"] == os.path.expanduser("~")               # still the safe default, never the outside folder
    code, res = work.spawn({"seat": "alpha", "brief": "x", "cwd": "/etc"}, snap, runner=r)
    assert res["queued"] is True and "/etc" in res["note"]
    assert db.rows("SELECT cwd FROM cp_spawn_queue")[0]["cwd"] is None
    code, res = work.spawn({"seat": "work", "brief": "x", "cwd": str(home / "proj")}, snap, runner=r)
    assert "note" not in res and r.calls[-1][1]["cwd"] == os.path.realpath(home / "proj")
    code, res = work.spawn({"seat": "work", "brief": "x"}, snap, runner=r)
    assert "note" not in res


def test_collisions_from_receipts(tmp, monkeypatch):
    monkeypatch.setattr(work.time, "time", lambda: 1100.0)
    receipts.store("s1", "/c", rc("s1", files=(("/r/a.py", "git"),)), True)
    receipts.store("s2", "/c", rc("s2", files=(("/r/a.py", "tool"), ("/r/b.py", "tool"))), True)
    out = work.collisions({"chats": [chat(), chat(session_id="s2", key="work:s2")]})
    assert out == [("/r/a.py", "s1", "s2")]


def test_auto_handoff_goes_stale_after_a_newer_turn(tmp, monkeypatch):
    monkeypatch.setattr(work, "looks_done", lambda c: (True, "verified receipt, no open decisions, final says done"))
    p = work.propose_handoff(chat(), "verified receipt, no open decisions, final says done")
    assert work.expire_handoffs(SNAP([chat()])) == 0
    newer = chat(final_at="2026-10-02T12:00:00Z", state="working")
    assert work.expire_handoffs(SNAP([newer])) == 1
    assert work.proposal(p["id"])["state"] == "stale"
    code, res = work.decide(p["id"], "apply", SNAP([newer]))
    assert code == 409


def test_auto_handoff_goes_stale_when_no_longer_done_and_confirm_refuses(tmp, monkeypatch):
    monkeypatch.setattr(work, "looks_done", lambda c: (True, "ok"))
    p = work.propose_handoff(chat(), "verified receipt, no open decisions, final says done")
    monkeypatch.setattr(work, "looks_done", lambda c: (False, "open decision"))
    code, res = work.decide(p["id"], "apply", SNAP([chat()]), sender=lambda c, t: {"ok": True}, stopper=lambda c: {"ok": True})
    assert code == 409 and "open decision" in res["error"] and work.proposal(p["id"])["state"] == "stale"


def test_no_handoff_without_a_work_item(tmp):
    assert work.propose_handoff(chat(ws=None), "You marked it done") is None


def test_spawn_cwd_is_validated(tmp, monkeypatch):
    home = tmp / "home"
    (home / "proj").mkdir(parents=True)
    (home / ".ssh").mkdir()
    monkeypatch.setattr(work, "HOME", str(home))
    assert work.safe_cwd(str(home / "proj")) == os.path.realpath(home / "proj")
    assert work.safe_cwd("/etc") is None and work.safe_cwd(str(home / ".ssh")) is None and work.safe_cwd(str(home / "nope-xyz")) is None
    cmd, cwd, env = work.launch_cmd(None, "alpha", "hi", str(home / "proj"))
    assert cwd == os.path.realpath(home / "proj")
    cmd, cwd, env = work.launch_cmd(None, "alpha", "hi", None)
    assert cwd == os.path.expanduser("~")           # no folder picked: the home directory, not a project folder


def test_open_proposals_expire_after_a_day_and_rule_cards_keep_the_newest_per_kind(tmp):
    now = 1_000_000.0
    db.execute("INSERT INTO cp_proposals(kind, work_item, target, why, created_at) VALUES('state_doc','PROJ-08','/x','old',?)", (now - 30 * 3600,))
    db.execute("INSERT INTO cp_proposals(kind, work_item, target, why, created_at) VALUES('state_doc','PROJ-10','/y','fresh',?)", (now - 3600,))
    for day, at in (("2026-10-02", now - 20 * 3600), ("2026-10-03", now - 2 * 3600)):
        for kind in ("honesty", "scope"):
            db.execute("INSERT INTO rules_proposed(week, pattern, examples, draft_rule, check_cmd, created_at) VALUES(?,?,?,?,?,?)",
                       (day, kind, "[]", "r", "c", at))
            rid = db.rows("SELECT id FROM rules_proposed WHERE week=? AND pattern=?", (day, kind))[0]["id"]
            db.execute("INSERT INTO cp_proposals(kind, target, why, created_at) VALUES('rule',?,?,?)", (str(rid), f"{kind} {day}", at))
    work.sweep_proposals(now)
    left = db.rows("SELECT kind, why FROM cp_proposals WHERE state='proposed' ORDER BY id")
    assert [(r["kind"], r["why"]) for r in left] == [("state_doc", "fresh"), ("rule", "honesty 2026-10-03"), ("rule", "scope 2026-10-03")]
    assert db.rows("SELECT state FROM cp_proposals WHERE why='old'")[0]["state"] == "expired"
