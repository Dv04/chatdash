"""A6 corrections and A7 retrieval drafts."""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import corrections, db, drafts, work  # noqa: E402

HOME = os.path.expanduser("~")


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    p = str(tmp_path / "cp.db")
    monkeypatch.setattr(db, "DB", p)
    monkeypatch.setattr(db, "CONFIG", str(tmp_path / "none.json"))
    db.init()
    work.init()
    corrections.init()
    c = sqlite3.connect(p)
    c.executescript("CREATE VIRTUAL TABLE turns_fts USING fts5(key UNINDEXED, name UNINDEXED, account UNINDEXED, idx UNINDEXED,"
                    " ts UNINDEXED, prompt, final, tokenize='porter unicode61');")
    rows = [("k1", "PROJ-10 a", "work", 0, "2026-10-01T15:00:00Z", "fix it", "Merged the branch. Should I deploy the worker now?"),
            ("k1", "PROJ-10 a", "work", 1, "2026-10-01T15:30:00Z", "No, wait for the smoke test first", "ok waiting"),
            ("k2", "PROJ-06 b", "alpha", 0, "2026-10-01T16:00:00Z", "go", "Tests pass, all done."),
            ("k2", "PROJ-06 b", "alpha", 1, "2026-10-01T16:10:00Z", "are you sure? did you check the box logs", "checked"),
            ("k3", "PROJ-02 c", "echo", 0, "2026-10-01T17:00:00Z", "x", "Finished, the campaign is live."),
            ("k3", "PROJ-02 c", "echo", 1, "2026-10-01T17:05:00Z", "you lied, nothing was sent. did you verify?", "sorry")]
    c.executemany("INSERT INTO turns_fts VALUES(?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return p


def test_classify_kinds():
    assert {k for k, _ in corrections.classify("are you sure? did you check the logs")} == {"honesty", "verification"}
    assert corrections.classify("Looks good, ship it") == []
    assert {k for k, _ in corrections.classify("I said only touch the docs folder")} == {"instruction", "scope"}


def test_scan_and_daily_rule_proposal(tmp):
    assert corrections.scan() >= 4
    assert corrections.scan() == 0                         # idempotent
    made = corrections.propose_for("2026-10-01")
    kinds = {r["pattern"] for r in db.rows("SELECT pattern FROM rules_proposed")}
    assert kinds == {"honesty", "verification"} and len(made) == 2
    props = db.rows("SELECT kind, why FROM cp_proposals WHERE kind='rule'")
    assert len(props) == 2 and all("corrections on 2026-10-01" in p["why"] for p in props)
    assert corrections.propose_for("2026-10-01") == []     # once per day per kind
    pid = db.rows("SELECT id FROM cp_proposals WHERE kind='rule'")[0]["id"]
    code, res = work.decide(pid, "apply", {"chats": []})
    assert code == 200 and "no CLAUDE.md was edited" in res["note"]


def test_retrieval_draft_uses_older_other_chats_only(tmp):
    pairs = drafts.similar_pairs("All green. Should I deploy the worker to prod?", "k9", "2026-10-02T00:00:00Z")
    assert pairs and pairs[0]["key"] == "k1" and pairs[0]["reply"].startswith("No, wait for the smoke test")
    assert drafts.similar_pairs("Should I deploy the worker?", "k1", None) == [] or \
        all(p["key"] != "k1" for p in drafts.similar_pairs("Should I deploy the worker?", "k1", None))
    assert drafts.similar_pairs("Should I deploy the worker?", "k9", "2026-09-01T00:00:00Z") == []   # nothing older


def test_drafter_dry_run_makes_retrieved_draft_and_marks_used(tmp):
    d = drafts.Drafter(mode_fn=lambda n: "dry-run", model=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model in dry-run")))
    chat = {"session_id": "s9", "key": "work:k9", "account": "work", "config": HOME + "/.claude-work", "kind": "bg", "state": "idle",
            "excluded": False, "banner": None, "final": "Built and merged. Should I deploy the worker now?", "final_at": "2026-10-02T09:00:00Z"}
    import time
    assert d.tick({"chats": [chat], "seats": []}, now=time.time()) == 1
    dr = drafts.latest_draft("s9")
    assert dr["class"] == "retrieved" and dr["draft"]["text"].startswith("No, wait")
    chat["last_prompt"], chat["last_prompt_at"] = "No, wait for the smoke test first please", "2033-01-01T00:00:00Z"
    d.mark_used({"chats": [chat]})
    assert drafts.precision()["matched"] == 1


# ------------------------------------------------------------------ intent routing (B7/B9)
from dhi_orbit.cp import intent  # noqa: E402

CTX = {"chats": ["PROJ-08 Research writeup - paper 08 run", "PROJ-10 Repos and code review", "game bug fixes"],
       "work_items": ["PROJ-08", "PROJ-10", "PROJ-14"], "seats": ["alpha", "work", "tryalpha"]}


def test_grammar_and_name_matching():
    assert intent.parse("spawn PROJ-14 on alpha", CTX) == {"cmd": "spawn", "work_item": "PROJ-14", "seat": "alpha", "confirm": True}
    assert intent.parse("open PROJ-10", CTX)["target"] == "PROJ-10"
    r = intent.parse("reply go ahead to writeup", CTX)
    assert r["chat"].startswith("PROJ-08") and r["text"] == "go ahead" and r["confirm"]
    assert intent.parse("option three", CTX)["option"] == 3
    assert intent.parse("stop nonexistent thing", CTX) is None


def test_model_output_is_validated_and_always_confirmed():
    post = lambda body: json.dumps({"message": {"content": json.dumps({"cmd": "graph"})}}).encode()
    assert intent.route("let me see the agents", CTX, post=post) == {"cmd": "graph", "source": "model", "confirm": True}
    bad = lambda body: json.dumps({"message": {"content": json.dumps({"cmd": "stop", "chat": "made up chat"})}}).encode()
    assert intent.route("kill it", CTX, post=bad)["cmd"] is None
    junk = lambda body: json.dumps({"message": {"content": json.dumps({"cmd": "format_disk"})}}).encode()
    assert intent.route("do it", CTX, post=junk)["cmd"] is None
