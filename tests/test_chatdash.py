import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from chatdash import extract  # noqa: E402
from chatdash.index import Index  # noqa: E402
from chatdash import server  # noqa: E402


def rec(**k):
    return json.dumps(k) + "\n"


def human(text, ts="2026-09-25T10:00:00Z"):
    return rec(type="user", origin={"kind": "human"}, timestamp=ts, cwd="/x", sessionId="s1",
               message={"role": "user", "content": text})


def asst(mid, blocks, ts="2026-09-25T10:00:05Z", out=10, cw=100, rd=1000, model="claude-opus-5-5"):
    return rec(type="assistant", timestamp=ts, message={"id": mid, "model": model, "content": blocks,
               "usage": {"cache_creation_input_tokens": cw, "cache_read_input_tokens": rd,
                         "input_tokens": 2, "output_tokens": out}})


class TestExtract(unittest.TestCase):
    def _t(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(text); f.close()
        t = extract.Transcript(f.name); t.update()
        return t, f.name

    def test_final_is_last_text_message_joined_across_lines(self):
        t, _ = self._t(
            human("do it")
            + asst("m1", [{"type": "text", "text": "Now I will look."}])
            + asst("m1", [{"type": "tool_use", "id": "u1", "name": "Bash", "input": {}}])
            + rec(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "u1"}]})
            + asst("m2", [{"type": "thinking", "thinking": "hmm"}])
            + asst("m2", [{"type": "text", "text": "Part one."}])
            + asst("m2", [{"type": "text", "text": "Part two."}], out=50))
        self.assertEqual(len(t.turns), 1)
        self.assertEqual(t.turns[0]["final"], "Part one.\n\nPart two.")
        self.assertIsNone(t.pending_tool)

    def test_texts_keep_every_message_in_order_without_tools(self):
        t, _ = self._t(
            human("first")
            + asst("m1", [{"type": "text", "text": "Looking."}])
            + asst("m1", [{"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "ls"}}])
            + rec(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "u1",
                                                     "content": "secret tool output"}]})
            + asst("m2", [{"type": "thinking", "thinking": "hidden"}])
            + asst("m2", [{"type": "text", "text": "Found it."}])
            + asst("m3", [{"type": "text", "text": "Done, part A."}])
            + asst("m3", [{"type": "text", "text": "Part B."}])
            + human("second", ts="2026-09-25T10:01:00Z")
            + asst("m4", [{"type": "text", "text": "Only answer."}]))
        self.assertEqual(t.turns[0]["texts"], ["Looking.", "Found it.", "Done, part A.\n\nPart B."])
        self.assertEqual(t.turns[0]["final"], t.turns[0]["texts"][-1])
        self.assertEqual(t.turns[1]["texts"], ["Only answer."])
        blob = json.dumps(t.turns)
        self.assertNotIn("secret tool output", blob)
        self.assertNotIn("hidden", blob)
        self.assertNotIn("ls", json.dumps([x["texts"] for x in t.turns]))

    def test_noise_and_tool_results_are_not_prompts(self):
        t, _ = self._t(
            human("real prompt")
            + rec(type="user", message={"content": "<task-notification>x</task-notification>"})
            + rec(type="user", origin={"kind": "task-notification"}, message={"content": "done"})
            + rec(type="user", isMeta=True, message={"content": "meta"})
            + asst("m1", [{"type": "text", "text": "answer"}]))
        self.assertEqual([x["prompt"] for x in t.turns], ["real prompt"])

    def test_incremental_partial_line(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        line = asst("m1", [{"type": "text", "text": "hello"}])
        f.write(human("q") + line[:20]); f.close()
        t = extract.Transcript(f.name); t.update()
        self.assertEqual(t.turns[0]["final"], "")
        with open(f.name, "a") as fh:
            fh.write(line[20:])
        self.assertTrue(t.update())
        self.assertEqual(t.turns[0]["final"], "hello")
        self.assertFalse(t.update())

    def test_units_count_each_message_once_with_final_output(self):
        t, _ = self._t(human("q") + asst("m1", [{"type": "text", "text": "a"}], out=10, cw=100)
                       + asst("m1", [{"type": "text", "text": "b"}], out=40, cw=100))
        self.assertEqual(t.units_on("2026-09-25"), 100 + 2 + 5 * 40)

    def test_pending_tool(self):
        t, _ = self._t(human("q") + asst("m1", [{"type": "tool_use", "id": "u9", "name": "Edit", "input": {}}]))
        self.assertEqual(t.pending_tool, "Edit")
        t, _ = self._t(human("q") + asst("m1", [{"type": "tool_use", "id": "u8", "name": "Bash",
                                                  "input": {"command": "git push"}}]))
        self.assertEqual(t.pending_tool, "Bash: git push")


class TestSendConfirm(unittest.TestCase):
    """A send typed while the chat is mid-turn is taken by the session's own queue (an enqueue record) and
    typed in when the turn ends; it must count as queued, not as a failed send (measured 2026-10-05)."""

    def _write(self, recs):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_enqueue_record_counts_as_queued_and_user_record_as_landed(self):
        from chatdash import actions
        text = "reply with only the word OK, sent while the chat was working"
        p = self._write([{"type": "queue-operation", "operation": "enqueue", "content": text}])
        self.assertEqual(actions._transcript_has(p, text, 0, 0.3), "queued")
        p = self._write([{"type": "queue-operation", "operation": "enqueue", "content": text},
                         {"type": "queue-operation", "operation": "dequeue"},
                         {"type": "user", "message": {"role": "user", "content": text}}])
        self.assertIs(actions._transcript_has(p, text, 0, 0.3), True)
        p = self._write([{"type": "queue-operation", "operation": "enqueue", "content": "some other message"}])
        self.assertIs(actions._transcript_has(p, text, 0, 0.3), False)

class TestDialogParse(unittest.TestCase):
    def test_permission_dialog(self):
        from chatdash import actions
        d = actions.parse_dialog("$ git push\r Do you want to create chatdash-perm-test2.txt ?\r ❯ 1. Yes\r"
                                 " 2. Yes, and switch to accept edits for this session\r 3. No\r Esc to cancel · Tab to amend")
        self.assertEqual(d["kind"], "permission")
        self.assertEqual([o["n"] for o in d["options"]], [1, 2, 3])
        self.assertEqual(d["cursor"], 1)
        self.assertTrue(d["options"][1]["label"].startswith("Yes, and switch"))
        self.assertIsNone(actions.parse_dialog("nothing here"))

    def test_question_dialog_with_tabs_and_descriptions(self):
        from chatdash import actions
        s = ("older text\r ← ☐ Rollout order ☐ Fix violations too ✔ Submit →\r"
             " How do you want it rolled out?\r ❯ 1. Cluster by cluster (Recommended)\r Homepage first.\r"
             " 2. One giant pass\r Faster to start.\r 3. Type something.\r \r 4. Chat about this\r"
             " Enter to select · Tab/Arrow keys to navigate · Esc to cancel")
        d = actions.parse_dialog(s)
        self.assertEqual(d["kind"], "question")
        self.assertEqual(d["question"], "How do you want it rolled out?")
        self.assertTrue(d["tabs"].startswith("←"))
        self.assertEqual(d["options"][0]["desc"], "Homepage first.")
        self.assertTrue(d["options"][2]["free"])
        self.assertEqual(len(d["options"]), 4)


class TestIndexAndPanels(unittest.TestCase):
    def test_search(self):
        ix = Index(os.path.join(tempfile.mkdtemp(), "t.db"))
        chat = {"key": "a:1", "name": "chat one", "account": "a", "version": 1}
        ix.sync(chat, [{"prompt": "stop the scheduled jobs", "final": "Stopped both morning jobs", "fts": "t"}])
        hits = ix.search("morning")
        self.assertEqual(len(hits), 1)
        self.assertIn("[morning]", hits[0]["final"])
        ix.mark_seen("a:1", "t")
        self.assertEqual(ix.seen_map()["a:1"], "t")

    def test_best_seat_is_the_one_with_most_headroom(self):
        seats = {"alpha": {"five": 10, "seven": 70, "ok": True}, "beta": {"five": 1, "seven": 5, "ok": True},
                 "delta": {"five": None, "seven": None, "ok": False}}
        self.assertEqual(server.best_seat(seats), "beta")
        seats["zero"] = {"five": 0, "seven": 0, "ok": True}                  # 0% used is the most headroom, not "full"
        self.assertEqual(server.best_seat(seats), "zero")


if __name__ == "__main__":
    unittest.main()
