"""Shadow prose-decision layer: candidates, dry-run spends nothing, cap, seat state."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatdash.cp import db, shadow  # noqa: E402

HOME = os.path.expanduser("~")


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()


def chat(**kw):
    c = {"session_id": "s1", "account": "work", "config": HOME + "/.claude-work", "kind": "bg", "state": "idle",
         "excluded": False, "banner": None, "final": "Done. Should I merge it or wait for CI?", "final_at": "t1"}
    c.update(kw)
    return c


def test_candidates_filter():
    snap = {"chats": [chat(), chat(session_id="s2", final="All done."), chat(session_id="s3", kind="interactive"),
                      chat(session_id="s4", config=HOME + "/.claude", excluded=True), chat(session_id="s5", state="working"),
                      chat(session_id="s6", banner={"x": 1})]}
    assert [c["session_id"] for c in shadow.candidates(snap, set())] == ["s1"]
    assert shadow.candidates(snap, {"s1:t1"}) == []


def test_dry_run_never_calls_model(tmp):
    calls = []
    s = shadow.Shadow(mode_fn=lambda n: "dry-run", model=lambda cfg, items: calls.append(items) or ([], 0))
    out = s.tick({"chats": [chat()], "seats": []}, now=1000)
    assert calls == [] and out["candidates"] == 1
    assert db.rows("SELECT decision FROM auto_log")[0]["decision"] == "would classify"


def test_on_mode_stores_suggestion_and_skips_hot_seats(tmp):
    calls = []

    def model(cfg, items):
        calls.append(cfg)
        return [{"id": items[0]["id"], "parts": [{"question": "Merge or wait?", "options": ["Merge", "Wait"], "recommended": "Wait"}]}], 0.03
    s = shadow.Shadow(mode_fn=lambda n: "on", model=model)
    seats = [{"config": HOME + "/.claude-work", "state": "ok"}, {"config": HOME + "/.claude-alpha", "state": "near"}]
    s.tick({"chats": [chat(), chat(session_id="d1", account="alpha", config=HOME + "/.claude-alpha")], "seats": seats}, now=1000)
    assert calls == [HOME + "/.claude-work"]
    sug = shadow.latest_for("s1")
    assert sug["parts"][0]["recommended"] == "Wait" and sug["used"] == 0


def test_daily_cap_stops_calls(tmp, monkeypatch):
    monkeypatch.setattr(shadow, "spent_today", lambda: 2.5)
    calls = []
    s = shadow.Shadow(mode_fn=lambda n: "on", model=lambda cfg, items: calls.append(1) or ([], 0))
    s.tick({"chats": [chat()], "seats": [{"config": HOME + "/.claude-work", "state": "ok"}]}, now=1000)
    assert calls == []
