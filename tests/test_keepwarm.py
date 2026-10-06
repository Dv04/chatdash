import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from dhi_orbit import extract  # noqa: E402
from dhi_orbit import keepwarm  # noqa: E402
from dhi_orbit.keepwarm import KeepWarm, decide, judge, new_calls  # noqa: E402

NOW = 1_000_000.0


def chat(**k):
    c = {"key": "a:s1", "kind": "bg", "job_id": "abcd1234", "state": "idle", "cache_age_min": 56.0,
         "ctx_tokens": 400_000, "model": "claude-opus-5-5", "warmth": "cooling", "name": "test chat",
         "config": "/x", "cwd": "/x", "path": None}
    c.update(k)
    return c


def row(**k):
    r = {"key": "a:s1", "active": 1, "on_at": NOW - 3600, "stop_at": NOW + 3600, "pings": 0, "units": 0.0,
         "last_ping": 0.0, "last_attempt": 0.0, "fails": 0}
    r.update(k)
    return r


class TestDecide(unittest.TestCase):
    def a(self, c, r=None, **kw):
        return decide(c, r or row(), NOW, **kw)[0]

    def test_ping_window(self):
        self.assertEqual(self.a(chat(cache_age_min=54.0)), "wait")
        self.assertEqual(self.a(chat(cache_age_min=55.0)), "ping")           # 5 minutes left
        self.assertEqual(self.a(chat(cache_age_min=58.9)), "ping")
        self.assertEqual(self.a(chat(cache_age_min=59.0)), "stop")     # already gone: never pay a cold ping
        self.assertEqual(self.a(chat(cache_age_min=75.0)), "stop")

    def test_never_types_over_work_or_a_dialog(self):
        self.assertEqual(self.a(chat(state="working")), "wait")
        self.assertEqual(self.a(chat(state="needs_you")), "wait")
        self.assertEqual(self.a(chat(), queued=True), "wait")
        self.assertEqual(self.a(chat(), busy=True), "wait")
        self.assertEqual(self.a(chat(), pinging=True), "wait")

    def test_only_background_chats_and_never_stopped(self):
        self.assertEqual(self.a(chat(kind="interactive", job_id=None)), "stop")
        self.assertEqual(self.a(chat(job_id=None)), "stop")
        self.assertEqual(self.a(chat(state="stopped")), "stop")

    def test_five_minute_ttl_chats_are_refused(self):
        self.assertEqual(self.a(chat(ttl="5m")), "stop")
        self.assertEqual(self.a(chat(ttl="1h")), "ping")
        self.assertEqual(self.a(chat(ttl=None)), "ping")                  # unknown label: do not block

    def test_unknowns_wait_not_stop(self):
        self.assertEqual(self.a(None), "wait")
        self.assertEqual(self.a(chat(cache_age_min=None)), "wait")

    def test_limits(self):
        self.assertEqual(self.a(chat(), row(stop_at=NOW - 1)), "stop")               # time limit
        self.assertEqual(self.a(chat(), row(fails=2)), "stop")
        self.assertEqual(self.a(chat(), row(fails=1)), "ping")
        self.assertEqual(self.a(chat(), row(last_attempt=NOW - 30)), "wait")          # retry gap
        # budget = half of a cold re-cache = 200k units for a 400k Opus context
        self.assertEqual(self.a(chat(), row(pings=10, units=100_000.0)), "ping")
        self.assertEqual(self.a(chat(), row(pings=10, units=195_000.0)), "stop")
        # a Sonnet chat's cold re-cache is 0.72x, so its budget is smaller
        self.assertEqual(self.a(chat(model="claude-sonnet-5-5"), row(pings=10, units=150_000.0)), "stop")


def line(mid, read, write, out, model="claude-opus-5-5", ts="2026-09-29T04:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model, "content": [{"type": "text", "text": "ok"}],
        "usage": {"cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                  "input_tokens": 3, "output_tokens": out}}}) + "\n"


class TestJudge(unittest.TestCase):
    def _f(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_new_calls_reads_after_offset_and_takes_largest_output(self):
        old = line("m0", 1000, 10, 5)
        p = self._f(old + line("m1", 400_000, 60, 1) + line("m1", 400_000, 60, 5))
        calls = new_calls(p, len(old.encode()))
        self.assertEqual([(c["id"], c["out"]) for c in calls], [("m1", 5)])

    def test_warm_ping(self):
        v = judge([{"id": "m1", "model": "claude-opus-5-5", "read": 400_000, "write": 60, "inp": 3, "out": 5}])
        self.assertTrue(v["warm"])
        # 60 + 3 + 25 written-equivalent + 1% of 400k read = 4,088 units (the read is an upper bound)
        self.assertAlmostEqual(v["units"], 60 + 3 + 25 + 4000, delta=1)

    def test_cold_ping(self):
        v = judge([{"id": "m1", "model": "claude-opus-5-5", "read": 5_583, "write": 394_000, "inp": 3, "out": 5}])
        self.assertFalse(v["warm"])
        self.assertEqual(v["write"], 394_000)

    def test_no_reply_is_unknown(self):
        self.assertIsNone(judge([])["warm"])


class FakeAwake:
    def __init__(self):
        self.calls = []

    def sync(self, want):
        self.calls.append(want)


class TestKeepWarm(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.tp = os.path.join(d, "s1.jsonl")
        open(self.tp, "w").write(line("m0", 5000, 100, 5))
        self.notes, self.recs, self.sent = [], [], []
        self.awake = FakeAwake()
        self.reply = lambda: line("mp%d" % len(self.sent), 399_000, 55, 5)
        self.ok = True

        def sender(cfg, job, text, cwd, path):
            self.sent.append(text)
            if not self.ok:
                return {"ok": False, "error": "prompt did not land"}
            with open(path, "a") as fh:
                fh.write(self.reply())
            return {"ok": True}

        self.kw = KeepWarm(db=os.path.join(d, "kw.db"), notify=lambda t, b: self.notes.append((t, b)),
                           record=lambda w, k, r: self.recs.append((w, k, r)), sender=sender, awake=self.awake,
                           verify_s=5, poll_s=0.05, settle_s=0.05)
        self.chat = chat(path=self.tp)

    def tick(self, now=None, **c):
        by = {"a:s1": dict(self.chat, **c)}
        self.kw.tick(by, now=now or time.time())
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=10)

    def test_enable_refusals(self):
        self.assertFalse(self.kw.enable(chat(kind="interactive", job_id=None))["ok"])
        self.assertFalse(self.kw.enable(chat(state="stopped"))["ok"])
        self.assertFalse(self.kw.enable(chat(warmth="cold"))["ok"])
        self.assertFalse(self.kw.enable(chat(ttl="5m"))["ok"])
        # "working" chats are labelled warm, but one that made no call for hours is cold
        self.assertFalse(self.kw.enable(chat(state="working", warmth="warm", cache_age_min=332.0))["ok"])
        self.assertTrue(self.kw.enable(chat(state="working", warmth="warm", cache_age_min=3.0))["ok"])
        self.assertTrue(self.kw.enable(chat(ttl="1h"))["ok"])

    def test_warm_ping_is_sent_verified_and_counted(self):
        self.kw.enable(self.chat)
        self.tick(cache_age_min=40.0)
        self.assertEqual(self.sent, [])                                     # too early
        self.tick(cache_age_min=56.0)
        self.assertEqual(self.sent, [keepwarm.PING_TEXT])
        r = self.kw._row("a:s1")
        self.assertEqual((r["active"], r["pings"], r["fails"]), (1, 1, 0))
        self.assertGreater(r["units"], 3000)                                # 1% of the 399k read
        self.assertLess(r["units"], 5000)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.awake.calls[-1], True)

    def test_cold_ping_switches_itself_off_and_notifies(self):
        self.kw.enable(self.chat)
        self.reply = lambda: line("mc", 5_583, 394_000, 5)
        self.tick(cache_age_min=56.0)
        r = self.kw._row("a:s1")
        self.assertEqual((r["active"], r["status"]), (0, "cold"))
        self.assertIn("394,000", r["detail"])
        self.assertTrue(any("FAILED" in t for t, _ in self.notes))
        self.tick(cache_age_min=56.0)
        self.assertEqual(len(self.sent), 1)                                 # no second ping after that
        self.assertEqual(self.awake.calls[-1], False)

    def test_two_failed_sends_switch_it_off(self):
        self.kw.enable(self.chat)
        self.ok = False
        self.tick(cache_age_min=56.0)
        self.kw._set("a:s1", last_attempt=0.0)
        self.tick(cache_age_min=57.0)
        self.assertEqual(len(self.sent), 2)
        self.kw._set("a:s1", last_attempt=0.0)
        self.tick(cache_age_min=58.0)
        r = self.kw._row("a:s1")
        self.assertEqual((r["active"], r["status"]), (0, "stopped"))
        self.assertEqual(len(self.sent), 2)

    def test_no_ping_while_hold_says_a_compaction_is_pending(self):
        self.kw.enable(self.chat)
        held = [True]
        self.kw.hold = lambda c: held[0]
        self.tick(cache_age_min=56.0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.kw._row("a:s1")["active"], 1)
        held[0] = False
        self.tick(cache_age_min=56.5)
        self.assertEqual(self.sent, [keepwarm.PING_TEXT])

    def test_expired_cache_is_not_pinged(self):
        self.kw.enable(self.chat)
        self.tick(cache_age_min=61.0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.kw._row("a:s1")["status"], "expired")

    def test_view_and_disable(self):
        self.assertIsNone(self.kw.view(self.chat))
        self.kw.enable(self.chat)
        v = self.kw.view(dict(self.chat, cache_age_min=20.0))
        self.assertEqual((v["on"], v["next_min"], v["pings"]), (True, 35, 0))
        self.kw.disable("a:s1")
        self.assertFalse(self.kw.view(self.chat)["on"])


class TestPingHiddenFromTurns(unittest.TestCase):
    def test_keepalive_turn_never_becomes_the_final_message(self):
        def human(text, ts):
            return json.dumps({"type": "user", "origin": {"kind": "human"}, "timestamp": ts, "cwd": "/x",
                               "sessionId": "s1", "message": {"role": "user", "content": text}}) + "\n"

        text = (human("do the thing", "2026-09-29T01:00:00Z") + line("m1", 100, 100, 50, ts="2026-09-29T01:00:05Z")
                .replace('"ok"', '"Real answer"')
                + human(keepwarm.PING_TEXT, "2026-09-29T01:50:00Z") + line("m2", 400_000, 55, 5, ts="2026-09-29T01:50:04Z"))
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        t = extract.Transcript(f.name)
        t.update()
        self.assertEqual(len(t.turns), 1)
        self.assertEqual(t.last_turn()["final"], "Real answer")
        self.assertEqual(t.last_ts, "2026-09-29T01:50:04Z")               # the ping still renews warmth
        self.assertEqual(len(t._usage), 2)                                # and its cost is still counted


class TestAutomaticMode(unittest.TestCase):
    """The Keep warm button, pressed by itself for every chat with 5 minutes left."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.tp = os.path.join(d, "s.jsonl")
        open(self.tp, "w").write(line("m0", 5000, 100, 5))
        self.sent = []

        def sender(cfg, job, text, cwd, path):
            self.sent.append(text)
            with open(path, "a") as fh:
                fh.write(line("mp%d" % len(self.sent), 399_000, 55, 5))
            return {"ok": True}

        self.kw = KeepWarm(db=os.path.join(d, "kw.db"), sender=sender, awake=FakeAwake(), verify_s=5,
                           poll_s=0.05, settle_s=0.05)
        self.kw.set_auto(True)                                       # off by default: these tests turn it on
        self.now = time.time()
        iso = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - ago))
        self.chat = chat(path=self.tp, cache_age_min=20.0, ctx_tokens=200_000, last_prompt_at=iso(20 * 60),
                         final_at=iso(19 * 60), warmth="warm")
        self.iso = iso

    def go(self, now=None, **c):
        by = {"a:s1": dict(self.chat, **c)}
        self.kw.tick(by, now=now or self.now)
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=10)

    def test_a_warm_chat_is_picked_up_with_no_button_and_pinged_at_5_minutes_left(self):
        self.go()                                                    # 20 min old: enabled, not pinged
        self.assertEqual((self.kw._row("a:s1")["active"], self.sent), (1, []))
        self.go(cache_age_min=54.0)
        self.assertEqual(self.sent, [])
        self.go(cache_age_min=55.5)                                  # 5 min or less left: the ping
        self.assertEqual(self.sent, [keepwarm.PING_TEXT])

    def test_it_stops_when_you_stop_using_the_chat(self):
        old = self.iso(13 * 3600)
        self.go(last_prompt_at=old, final_at=old)                    # last real activity 13 h ago
        self.assertIsNone(self.kw._row("a:s1"))

    def test_small_cold_stopped_or_5m_chats_are_ignored(self):
        for c in (dict(ctx_tokens=5000), dict(cache_age_min=61.0), dict(state="stopped"), dict(ttl="5m"),
                  dict(kind="interactive", job_id=None)):
            self.go(**c)
            self.assertIsNone(self.kw._row("a:s1"), c)

    def test_it_is_off_until_switched_on(self):
        fresh = KeepWarm(db=os.path.join(tempfile.mkdtemp(), "fresh.db"), sender=lambda *a: {"ok": True}, awake=FakeAwake())
        self.assertFalse(fresh.auto_on())
        fresh.tick({"a:s1": self.chat}, now=self.now)
        self.assertIsNone(fresh._row("a:s1"))                        # nothing is picked up, so nothing is ever pinged
        self.assertEqual(self.sent, [])

    def test_it_can_be_switched_off(self):
        self.kw.set_auto(False)
        self.go()
        self.assertIsNone(self.kw._row("a:s1"))
        self.kw.set_auto(True)
        self.go()
        self.assertIsNotNone(self.kw._row("a:s1"))

    def test_switching_off_ends_chats_it_already_started_but_not_manual_ones(self):
        self.go()                                                    # automatic mode picks a:s1 up
        manual = dict(self.chat, key="a:s2")
        self.assertTrue(self.kw.enable(manual, 12)["ok"])            # a:s2 turned on by hand
        self.assertEqual((self.kw._row("a:s1")["active"], self.kw._row("a:s2")["active"]), (1, 1))
        self.kw.set_auto(False)
        self.assertEqual((self.kw._row("a:s1")["active"], self.kw._row("a:s2")["active"]), (0, 1))
        self.kw.tick({"a:s1": self.chat, "a:s2": manual}, now=self.now)
        self.assertEqual((self.kw._row("a:s1")["active"], self.kw._row("a:s2")["active"]), (0, 1))

    def test_after_a_failure_it_waits_for_a_new_real_call(self):
        self.go()
        self.kw.disable("a:s1", "the ping re-wrote 300,000 tokens", "cold")
        self.go(cache_age_min=20.0)                                  # last call is older than the end: leave it
        self.assertEqual(self.kw._row("a:s1")["active"], 0)
        self.go(now=self.now + 5, cache_age_min=0.0)                 # you used the chat again: back on
        self.assertEqual(self.kw._row("a:s1")["active"], 1)


class TestWriteTtl(unittest.TestCase):
    def test_last_cache_write_label_is_tracked(self):
        def call(mid, ts, w5, w1):
            return json.dumps({"type": "assistant", "timestamp": ts, "message": {
                "id": mid, "model": "claude-opus-5-5", "content": [],
                "usage": {"cache_read_input_tokens": 1000, "cache_creation_input_tokens": w5 + w1,
                          "input_tokens": 1, "output_tokens": 1,
                          "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}}}) + "\n"

        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(call("a", "2026-09-29T01:00:00Z", 0, 500) + call("b", "2026-09-29T01:01:00Z", 0, 0))
        f.close()
        self.addCleanup(os.unlink, f.name)
        t = extract.Transcript(f.name)
        t.update()
        self.assertEqual(t.write_ttl, "1h")                               # a read-only call keeps the last label
        with open(f.name, "a") as fh:
            fh.write(call("c", "2026-09-29T02:00:00Z", 700, 0))
        t.update()
        self.assertEqual(t.write_ttl, "5m")


if __name__ == "__main__":
    unittest.main()
