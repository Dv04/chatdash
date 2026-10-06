"""Subagent liveness for the graph: a finished worker is not a node (notification, ended last turn, dead parent)."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import api, db, subagents  # noqa: E402

NOW = 2_000_000_000.0


def note(tu, status):
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "content":
                       f"<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>{tu}</tool-use-id>\n"
                       f"<output-file>/tmp/x.output</output-file>\n<status>{status}</status>\n<summary>Agent finished</summary>\n</task-notification>"})


def dump(o):
    return json.dumps(o, separators=(",", ":"))      # transcripts are compact JSON: the tail reader prefilters on the bytes


def asst(stop, *blocks):
    return dump({"type": "assistant", "message": {"stop_reason": stop, "content": list(blocks)}})


TEXT = {"type": "text", "text": "done"}
TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
RESULT = dump({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})


@pytest.fixture(autouse=True)
def fresh():
    subagents._seen.clear()


def mk(tmp_path, name, tu, lines, age):
    parent = tmp_path / "proj" / "sess1.jsonl"
    parent.parent.mkdir(exist_ok=True)
    parent.touch()
    d = tmp_path / "proj" / "sess1" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"agent-{name}.jsonl"
    f.write_text("\n".join(lines) + "\n")
    (d / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": "mailbox-worker", "description": f"job {name}", "toolUseId": tu}))
    os.utime(f, (NOW - age, NOW - age))
    return str(parent)


def chats(parent, live=True):
    return {parent: {"live": live, "key": "isro:sess1", "account": "isro", "session_id": "sess1"}}


def ids(res):
    return {s["id"]: s["state"] for s in res}


def test_running_worker_is_working_then_unknown_then_dropped(tmp_path):
    parent = mk(tmp_path, "run", "toolu_run", [asst(None, TOOL)], age=30)
    assert ids(subagents.live(chats(parent), NOW)) == {"agent-run": "working"}
    os.utime(str(tmp_path / "proj/sess1/subagents/agent-run.jsonl"), (NOW - 600, NOW - 600))     # tool in flight 10 min
    assert ids(subagents.live(chats(parent), NOW)) == {"agent-run": "working"}
    os.utime(str(tmp_path / "proj/sess1/subagents/agent-run.jsonl"), (NOW - 1500, NOW - 1500))   # silent 25 min
    assert ids(subagents.live(chats(parent), NOW)) == {"agent-run": "unknown"}
    os.utime(str(tmp_path / "proj/sess1/subagents/agent-run.jsonl"), (NOW - 2000, NOW - 2000))   # silent 33 min
    assert subagents.live(chats(parent), NOW) == []


def test_terminal_notification_in_parent_ends_it_and_is_read_incrementally(tmp_path):
    parent = mk(tmp_path, "a", "toolu_a", [asst(None, TOOL)], age=10)
    assert ids(subagents.live(chats(parent), NOW)) == {"agent-a": "working"}
    with open(parent, "a") as fh:
        fh.write(note("toolu_a", "failed") + "\n")           # only the new bytes are scanned
    assert subagents.live(chats(parent), NOW) == []
    assert subagents._seen[parent]["ended"] == {"toolu_a": "failed"}


def test_non_terminal_notification_text_does_not_end_it(tmp_path):
    parent = mk(tmp_path, "a", "toolu_a", [asst(None, TOOL)], age=10)
    with open(parent, "a") as fh:
        fh.write(note("toolu_a", "running") + "\n")
    assert ids(subagents.live(chats(parent), NOW)) == {"agent-a": "working"}


def test_ended_last_turn_means_finished_but_a_tool_result_means_still_thinking(tmp_path):
    p1 = mk(tmp_path, "done", "toolu_d", [asst(None, TOOL), asst("end_turn", TEXT)], age=5)
    assert subagents.live(chats(p1), NOW) == []
    p2 = mk(tmp_path, "think", "toolu_t", [asst("tool_use", TOOL), RESULT], age=5)
    assert "agent-think" in ids(subagents.live(chats(p2), NOW))
    p3 = mk(tmp_path, "mid", "toolu_m", [asst("tool_use", TOOL)], age=5)      # a turn that ends with a tool call is not over
    assert "agent-mid" in ids(subagents.live(chats(p3), NOW))


def test_a_stopped_chat_has_no_running_subagents(tmp_path):
    parent = mk(tmp_path, "a", "toolu_a", [asst(None, TOOL)], age=5)
    assert subagents.live(chats(parent, live=False), NOW) == []


def test_notification_split_across_two_reads_is_still_found(tmp_path, monkeypatch):
    monkeypatch.setattr(subagents, "CHUNK", 2048)
    monkeypatch.setattr(subagents, "OVERLAP", 1024)
    parent = mk(tmp_path, "a", "toolu_a", [asst(None, TOOL)], age=5)
    with open(parent, "a") as fh:
        fh.write("x" * 1900 + "\n" + note("toolu_a", "completed") + "\n" + "y" * 3000 + "\n")     # the note straddles a chunk edge
    assert subagents.ended_in_parent(parent) == {"toolu_a": "completed"}


def test_a_replaced_parent_transcript_is_rescanned(tmp_path):
    parent = mk(tmp_path, "a", "toolu_a", [asst(None, TOOL)], age=5)
    with open(parent, "a") as fh:
        fh.write(note("toolu_a", "killed") + "\n")
    assert subagents.ended_in_parent(parent) == {"toolu_a": "killed"}
    open(parent, "w").write("")
    assert subagents.ended_in_parent(parent) == {}


def test_graph_lists_only_running_subagents_with_their_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()
    pa = mk(tmp_path, "run", "toolu_run", [asst(None, TOOL)], age=5)
    mk(tmp_path, "fin", "toolu_fin", [asst("end_turn", TEXT)], age=5)
    chat = {"key": "isro:sess1", "session_id": "sess1", "account": "isro", "config": "/x/.claude-isro", "name": "chat", "ws": None,
            "state": "working", "live": True, "excluded": False, "kind": "bg", "path": pa, "cwd": None, "activity": NOW, "banner": None,
            "units_today": 0, "final": "", "waiting_for": None, "pending_tool": None}
    seats = [{"seat": "isro", "label": "Isro", "state": "ok", "five_hour": {"pct": 1}, "excluded": False}]
    g = api.graph({"chats": [chat], "jobs": [], "seats": seats, "fleet": [], "errors": {}, "at": NOW - 7}, NOW)
    subs = [n for n in g["nodes"] if n["id"].startswith("job:sub:")]
    assert [n["id"] for n in subs] == ["job:sub:agent-run"]
    assert subs[0]["state"] == "working" and subs[0]["tool"] == "Bash" and subs[0]["parent_session_id"] == "sess1"
    assert subs[0]["label"] == "mailbox-worker: job run" and g["snapshot_at"] == NOW - 7
    assert {"from": "job:sub:agent-run", "to": "session:isro:sess1", "kind": "spawned_by"} in g["edges"]


class Src:
    def __init__(self, at):
        self.snap = {"chats": [], "jobs": [], "seats": [], "fleet": [], "errors": {}, "at": at}

    def get(self):
        return self.snap


def test_history_compact_expands_to_the_plain_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()
    now = time.time()
    for at, fr in ((now - 120, [["a", "idle", 0, "x", "A", "session"], ["b", "working", 1, "x", "B", "session"]]),
                   (now - 60, [["b", "idle", 0, "x", "B", "session"]])):
        db.execute("INSERT INTO cp_graph_history(at, frame) VALUES(?,?)", (at, json.dumps(fr)))
    code, plain = api.handle("GET", "graph/history", {"hours": ["1"]}, None, Src(now))
    code2, comp = api.handle("GET", "graph/history", {"hours": ["1"], "compact": ["1"]}, None, Src(now))
    assert code == code2 == 200
    expanded = [{"at": f["at"], "nodes": [[comp["ids"][n[0]], n[1], n[2]] for n in f["nodes"]]} for f in comp["frames"]]
    assert expanded == [{"at": f["at"], "nodes": [n[:3] for n in f["nodes"]]} for f in plain["frames"]]
    assert comp["ids"] == ["a", "b"] and len(json.dumps(comp)) < len(json.dumps(plain))
