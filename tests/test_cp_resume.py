"""Limit-resume policy and its auto_log trail."""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import db, resume  # noqa: E402

HOME = os.path.expanduser("~")
NOW = 100_000.0


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()
    before = set(threading.enumerate())
    yield
    # A resume send runs on its own thread and writes its result last; let it finish before this test's database
    # goes away (on a slower machine it outlived the test and logged "no such table: cp_resume").
    for t in set(threading.enumerate()) - before:
        t.join(timeout=5)


def chat(**kw):
    c = {"key": "work:s1", "session_id": "s1", "account": "work", "config": HOME + "/.claude-work", "kind": "bg",
         "job_id": "abcd1234", "state": "idle", "excluded": False, "path": "/t.jsonl", "last_prompt": "do the thing",
         "banner": {"kind": "session", "resets_at": NOW - 120, "shown_at": NOW - 3600, "text": "You've hit..."}}
    c.update(kw)
    return c


SEAT = {"config": HOME + "/.claude-work", "five_hour": {"pct": 3}, "seven_day": {"pct": 10}}


def test_decide_waits_for_reset_and_skips_unsafe():
    assert resume.decide(chat(), None, SEAT, NOW)[0] == "resume"
    b = dict(chat()["banner"], resets_at=NOW + 600)
    assert resume.decide(chat(banner=b), None, SEAT, NOW)[0] == "wait"
    assert resume.decide(chat(state="working"), None, SEAT, NOW)[0] == "wait"
    assert resume.decide(chat(state="needs_you"), None, SEAT, NOW)[0] == "wait"
    assert resume.decide(chat(kind="interactive"), None, SEAT, NOW)[0] == "skip"
    assert resume.decide(chat(job_id=None), None, SEAT, NOW)[0] == "skip"
    assert resume.decide(chat(config=HOME + "/.claude", excluded=True), None, SEAT, NOW)[0] == "skip"
    assert resume.decide(chat(banner=None), None, SEAT, NOW)[0] == "skip"
    full = dict(SEAT, five_hour={"pct": 100})
    assert resume.decide(chat(), None, full, NOW)[0] == "wait"


def test_rearm_counts_only_our_own_resume():
    row = {"banner_at": NOW - 9000, "status": "sent", "arms": 2}
    assert resume.rearm(chat(last_prompt=resume.RESUME_TEXT), row) == 3          # limit hit again right after us
    assert resume.decide(chat(last_prompt=resume.RESUME_TEXT), row, SEAT, NOW)[0] == "skip"
    assert resume.rearm(chat(last_prompt="The user typed something new"), row) == 0   # new stall starts over
    assert resume.decide(chat(last_prompt="The user typed something new"), row, SEAT, NOW)[0] == "resume"


def test_cancel_and_already_sent():
    c = chat()
    assert resume.decide(c, {"banner_at": c["banner"]["shown_at"], "cancelled": 1}, SEAT, NOW)[0] == "skip"
    assert resume.decide(c, {"banner_at": c["banner"]["shown_at"], "status": "sent", "arms": 0}, SEAT, NOW)[0] == "wait"


def test_dry_run_logs_once_per_banner_and_sends_nothing(tmp):
    sent = []
    r = resume.Resumer(sender=lambda c, t: sent.append(t) or {"ok": True}, mode_fn=lambda n: "dry-run")
    snap = {"chats": [chat()], "seats": [SEAT]}
    assert r.tick(snap, NOW) == [{"session_id": "s1", "action": "would resume"}]
    assert r.tick(snap, NOW + 10) == []
    assert sent == []
    logs = db.rows("SELECT * FROM auto_log")
    assert len(logs) == 1 and logs[0]["decision"] == "would resume"
    assert json.loads(logs[0]["evidence"])["transcript"] == "/t.jsonl"


def test_on_mode_sends_context_line_once(tmp):
    sent = []
    r = resume.Resumer(sender=lambda c, t: sent.append(t) or {"ok": True, "confirmed": True, "route": "attach"},
                       mode_fn=lambda n: "on")
    snap = {"chats": [chat()], "seats": [SEAT]}
    r.tick(snap, NOW)
    for _ in range(50):
        if r.row("s1")["status"] == "sent":
            break
        time.sleep(0.02)
    assert sent == [resume.RESUME_TEXT] and r.row("s1")["status"] == "sent"
    assert r.tick(snap, NOW + 30) == []            # same banner: never twice
    assert db.rows("SELECT decision FROM auto_log")[-1]["decision"] == "sent"


def test_off_mode_does_nothing(tmp):
    r = resume.Resumer(sender=lambda c, t: {"ok": True}, mode_fn=lambda n: "off")
    assert r.tick({"chats": [chat()], "seats": [SEAT]}, NOW) == []
    assert db.rows("SELECT * FROM auto_log") == []


def test_failed_send_is_recorded(tmp):
    r = resume.Resumer(sender=lambda c, t: {"ok": False, "error": "prompt did not reach the transcript"},
                       mode_fn=lambda n: "on")
    r.tick({"chats": [chat()], "seats": [SEAT]}, NOW)
    for _ in range(50):
        if r.row("s1")["status"] == "failed":
            break
        time.sleep(0.02)
    row = r.row("s1")
    assert row["status"] == "failed" and "transcript" in row["detail"]


def test_old_stall_is_left_to_dev():
    old = dict(chat()["banner"], resets_at=NOW - 13 * 3600)
    assert resume.decide(chat(banner=old), None, SEAT, NOW) == ("skip", "reset passed over 12 h ago: left to the user (the stall may be parked on purpose)")


def _wait(r, sid, status):
    for _ in range(50):
        row = r.row(sid)
        if row and row["status"] == status:
            return True
        time.sleep(0.02)
    return False


def test_per_chat_on_resumes_while_global_is_dry_run_and_others_only_log(tmp):
    sent = []
    r = resume.Resumer(sender=lambda c, t: sent.append(c["session_id"]) or {"ok": True, "confirmed": True}, mode_fn=lambda n: "dry-run")
    resume.set_pref(chat(), "on")
    other = chat(key="work:s2", session_id="s2")
    out = r.tick({"chats": [chat(), other], "seats": [SEAT]}, NOW)
    assert sorted((o["session_id"], o["action"]) for o in out) == [("s1", "resume"), ("s2", "would resume")]
    assert _wait(r, "s1", "sent") and sent == ["s1"]


def test_per_chat_off_never_resumes_even_when_global_is_on(tmp):
    sent = []
    r = resume.Resumer(sender=lambda c, t: sent.append(c["session_id"]) or {"ok": True}, mode_fn=lambda n: "on")
    resume.set_pref(chat(), "off")
    assert r.tick({"chats": [chat()], "seats": [SEAT]}, NOW) == [] and sent == []


def test_per_chat_on_works_when_global_is_off_and_default_restores_global(tmp):
    r = resume.Resumer(sender=lambda c, t: {"ok": True, "confirmed": True}, mode_fn=lambda n: "off")
    resume.set_pref(chat(), "on")
    assert [o["action"] for o in r.tick({"chats": [chat()], "seats": [SEAT]}, NOW)] == ["resume"]
    resume.set_pref(chat(), None)
    assert resume.prefs() == {}
    assert db.rows("SELECT decision FROM auto_log WHERE action='limit_resume_pref'")[-1]["decision"] == "set default"


def test_keepwarm_pauses_on_a_spent_seat():
    from dhi_orbit import keepwarm
    row = {"stop_at": NOW + 3600, "fails": 0, "last_attempt": 0, "units": 0, "pings": 0}
    c = {"kind": "bg", "job_id": "j", "state": "idle", "cache_age_min": 56.0, "ctx_tokens": 100_000, "ttl": "1h"}
    assert keepwarm.decide(c, row, NOW, seat={"five": 100, "seven": 20})[0] == "wait"
    assert "7d" in keepwarm.decide(c, row, NOW, seat={"five": 10, "seven": 100})[1]
    assert "limit" not in keepwarm.decide(c, row, NOW, seat={"five": None, "seven": None})[1]   # unknown never blocks


# ------------------------------------------------------------------ idle compaction
from dhi_orbit.cp import idlecompact  # noqa: E402

ISEAT = {"config": HOME + "/.claude-work", "five_hour": {"pct": 40}, "seven_day": {"pct": 30}}


def ichat(**kw):
    c = chat(banner=None, state="idle", ctx_tokens=400_000, cache_age_min=20.0, last_prompt_at="2026-10-03T00:00:00Z")
    c.update(kw)
    return c


def test_idle_decide():
    d = lambda c, pings=2, seat=ISEAT, row=None, on=True: idlecompact.decide(c, on, pings, seat, row)
    assert d(ichat())[0] == "compact"
    assert d(ichat(), pings=1)[0] == "skip"
    assert d(ichat(), on=False)[0] == "skip"
    assert d(ichat(ctx_tokens=90_000))[0] == "cold"                       # small: not worth compacting
    assert d(ichat(cache_age_min=70.0))[0] == "cold"                      # cold already: never compact
    assert d(ichat(), seat=dict(ISEAT, five_hour={"pct": 100}))[0] == "cold"
    assert d(ichat(state="needs_you"))[0] == "skip"                       # never type into a dialog
    assert d(ichat(job_id=None))[0] == "skip"
    assert d(ichat(), row={"basis": "2026-10-03T00:00:00Z", "status": "compacted"})[0] == "skip"
    assert d(ichat(last_prompt_at="2026-10-03T05:00:00Z"), row={"basis": "2026-10-03T00:00:00Z", "status": "compacted"})[0] == "compact"


def test_pings_since_counts_only_keepalives_after_the_last_real_prompt(tmp_path):
    t = tmp_path / "t.jsonl"
    recs = [{"type": "user", "timestamp": "2026-10-03T00:30:00Z", "message": {"content": "[keepalive] Reply with exactly: ok."}},
            {"type": "user", "timestamp": "2026-10-03T01:00:00Z", "message": {"content": "real work"}},
            {"type": "user", "timestamp": "2026-10-03T02:00:00Z", "message": {"content": "[keepalive] Reply with exactly: ok."}},
            {"type": "user", "timestamp": "2026-10-03T03:00:00Z", "message": {"content": [{"type": "text", "text": "[keepalive] Reply with exactly: ok."}]}}]
    t.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert idlecompact.pings_since(str(t), "2026-10-03T01:00:00Z") == 2


class KW:
    def __init__(self, keys):
        self.keys, self.disabled = set(keys), []

    def active_rows(self):
        return [{"key": k} for k in self.keys]

    def disable(self, key, why="", status="off"):
        self.disabled.append((key, why))
        self.keys.discard(key)


def _wait_status(r, want, n=100):
    for _ in range(n):
        if (r.row("s1") or {}).get("status") in want:
            return
        time.sleep(0.02)


def test_idle_compactor_dry_run_then_on_compacts_first_then_stops_keepwarm(tmp, tmp_path, monkeypatch):
    monkeypatch.setattr(idlecompact, "ping_status", lambda p, s: {"n": 2, "last": None, "reply": None})
    path = _tx(tmp_path, [{"type": "user", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": "real work"}}])
    kw, sent = KW({"work:s1"}), []
    r = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: sent.append(t) or {"ok": True}, mode_fn=lambda n: "dry-run", every=0)
    snap = {"chats": [ichat(path=path)], "seats": [ISEAT]}
    assert r.tick(snap, NOW) == [{"session_id": "s1", "action": "would compact"}] and sent == [] and kw.disabled == []
    db.execute("DELETE FROM cp_idle_compact")
    r2 = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: sent.append(t) or {"ok": True}, mode_fn=lambda n: "on", every=0)
    assert [o["action"] for o in r2.tick(snap, NOW)] == ["compact"]
    _wait_status(r2, ("sent",))
    assert sent == ["/compact"] and kw.disabled == []                    # keep-warm still on while unconfirmed
    assert r2.pending(ichat()) is True
    at = r2.row("s1")["at"]
    with open(path, "a") as fh:                                            # the boundary lands, then the /compact prompt is ours
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2099-01-01T00:00:00Z",
                             "compactMetadata": {"preTokens": 400_000, "postTokens": 12_345}}) + "\n")
    c2 = ichat(path=path, last_prompt="/compact", last_prompt_at="2026-10-03T03:00:00Z", ctx_tokens=400_000)
    r2.tick({"chats": [c2], "seats": [ISEAT]}, at + 30)
    row = r2.row("s1")
    assert (row["status"], row["ctx_after"]) == ("compacted", 12_345)  # from the boundary, not the stale usage
    assert [k for k, _ in kw.disabled] == ["work:s1"]                     # disabled exactly once, after the boundary
    assert r2.blocked(c2) is True                                         # the /compact prompt does not clear the block
    assert r2.blocked(ichat(last_prompt="real", last_prompt_at="2026-10-03T09:00:00Z")) is False
    assert r2.pending(c2) is False


def test_idle_compaction_without_boundary_fails_and_keeps_keepwarm_on(tmp, tmp_path, monkeypatch):
    monkeypatch.setattr(idlecompact, "ping_status", lambda p, s: {"n": 2, "last": None, "reply": None})
    path = _tx(tmp_path, [{"type": "user", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": "real work"}}])
    kw = KW({"work:s1"})
    r = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: {"ok": True}, mode_fn=lambda n: "on", every=0)
    snap = {"chats": [ichat(path=path)], "seats": [ISEAT]}
    r.tick(snap, NOW)
    _wait_status(r, ("sent",))
    at = r.row("s1")["at"]
    r.tick(snap, at + idlecompact.VERIFY_S - 10)
    assert r.row("s1")["status"] == "sent"
    r.tick(snap, at + idlecompact.VERIFY_S + 1)
    assert r.row("s1")["status"] == "failed" and kw.disabled == []
    logs = db.rows("SELECT * FROM auto_log WHERE action='idle_compact' AND decision LIKE 'compact failed%'")
    assert len(logs) == 1
    r.tick(snap, at + 2 * idlecompact.VERIFY_S)                           # no retry for this basis
    assert r.row("s1")["status"] == "failed" and len(db.rows("SELECT * FROM auto_log WHERE action='idle_compact' AND decision LIKE 'compact failed%'")) == 1


def test_idle_send_failure_keeps_keepwarm_on(tmp, tmp_path, monkeypatch):
    monkeypatch.setattr(idlecompact, "ping_status", lambda p, s: {"n": 2, "last": None, "reply": None})
    kw = KW({"work:s1"})
    r = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: {"ok": False, "error": "job gone"}, mode_fn=lambda n: "on", every=0)
    r.tick({"chats": [ichat()], "seats": [ISEAT]}, NOW)
    _wait_status(r, ("failed",))
    assert r.row("s1")["status"] == "failed" and kw.disabled == [] and r.pending(ichat()) is False


def test_idle_skips_while_keepwarm_is_pinging(tmp, monkeypatch):
    monkeypatch.setattr(idlecompact, "ping_status", lambda p, s: {"n": 2, "last": None, "reply": None})
    kw = KW({"work:s1"})
    kw.pinging = {"work:s1"}
    r = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: {"ok": True}, mode_fn=lambda n: "on", every=0)
    assert r.tick({"chats": [ichat()], "seats": [ISEAT]}, NOW) == []


# ---- idle compaction: ping-reply race, compact-before-stop order, no ping while a compaction is pending (SPEC.md)
KA = "[keepalive] Reply with exactly: ok."


def _tx(tmp_path, recs):
    t = tmp_path / "s1.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(t)


def _ka_transcript(tmp_path, reply_ts=None):
    recs = [{"type": "user", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": "real work"}},
            {"type": "user", "timestamp": "2026-10-03T00:55:00Z", "message": {"content": KA}},
            {"type": "assistant", "timestamp": "2026-10-03T00:55:05Z", "message": {"id": "m1", "content": "ok"}},
            {"type": "user", "timestamp": "2026-10-03T01:50:00Z", "message": {"content": KA}}]
    if reply_ts:
        recs.append({"type": "assistant", "timestamp": reply_ts, "message": {"id": "m2", "content": "ok"}})
    return _tx(tmp_path, recs)


def test_idle_race_waits_for_the_second_pings_reply_then_compacts(tmp, tmp_path):
    from dhi_orbit.extract import iso_epoch
    t_ping = iso_epoch("2026-10-03T01:50:00Z")
    kw = KW({"work:s1"})
    r = idlecompact.IdleCompactor(kw=kw, sender=lambda c, t: {"ok": True}, mode_fn=lambda n: "dry-run", every=0)
    c = ichat(path=_ka_transcript(tmp_path), cache_age_min=56.0)            # 2nd ping typed, reply not in yet
    out = r.tick({"chats": [c], "seats": [ISEAT]}, t_ping + 1)
    assert out == [] and kw.disabled == []                                  # must wait, never "cold" here
    c = ichat(path=_ka_transcript(tmp_path, "2026-10-03T01:50:06Z"), cache_age_min=0.1)
    assert [o["action"] for o in r.tick({"chats": [c], "seats": [ISEAT]}, t_ping + 10)] == ["would compact"]


def test_idle_unanswered_ping_goes_cold_after_two_minutes(tmp, tmp_path):
    from dhi_orbit.extract import iso_epoch
    t_ping = iso_epoch("2026-10-03T01:50:00Z")
    r = idlecompact.IdleCompactor(kw=KW({"work:s1"}), sender=lambda c, t: {"ok": True}, mode_fn=lambda n: "dry-run", every=0)
    c = ichat(path=_ka_transcript(tmp_path), cache_age_min=56.0)
    assert r.tick({"chats": [c], "seats": [ISEAT]}, t_ping + 60) == []
    assert [o["action"] for o in r.tick({"chats": [c], "seats": [ISEAT]}, t_ping + 130)] == ["would cold"]
