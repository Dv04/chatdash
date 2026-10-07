"""Other local terminal agents described in agents.json: by files, by scripts, read-only, and a bad file never breaks the board."""
import json
import os
import sys
import time

import pytest

from dhi_orbit import providers
from dhi_orbit.providers import board, custom


def write_cfg(agents):
    os.makedirs(os.path.dirname(custom.config_path()), exist_ok=True)
    with open(custom.config_path(), "w", encoding="utf-8") as fh:
        json.dump({"agents": agents}, fh)
    custom._cache["sig"] = None
    custom._parsed.clear()
    providers._CACHE.clear()


def jsonl(path, recs):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")


@pytest.fixture
def sessions(tmp_path):
    d = tmp_path / "myagent" / "sessions"
    d.mkdir(parents=True)
    now = time.time()
    a = d / "alpha.jsonl"
    jsonl(a, [{"role": "user", "content": "Fix the login bug", "ts": "2026-10-06T10:00:00Z", "cwd": str(tmp_path)},
              {"role": "assistant", "content": [{"type": "text", "text": "Found it in auth.py"}], "ts": "2026-10-06T10:00:09Z"},
              {"role": "tool", "content": "ls"},
              {"role": "user", "content": "Thanks, now add a test", "ts": "2026-10-06T10:01:00Z"},
              {"role": "assistant", "content": "Added tests/test_auth.py", "ts": "2026-10-06T10:01:30Z"}])
    old = time.time() - 3600
    os.utime(a, (old, old))
    b = d / "beta.json"
    b.write_text(json.dumps({"messages": [{"role": "human", "text": "hello"}, {"role": "model", "text": "hi"}]}))
    os.utime(b, (old - 100, old - 100))
    return d


FILES_SPEC = lambda d: {"id": "myagent", "label": "My agent",
                        "files": {"glob": str(d / "*.jsonl"), "format": "jsonl", "role": "role", "text": "content",
                                  "time": "ts", "cwd": "cwd"},
                        "reply": [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.argv[2])", "{id}.out", "{text}"]}


def test_a_files_agent_lists_chats_and_reads_the_messages(sessions):
    write_cfg([FILES_SPEC(sessions)])
    mod = providers._modules()["myagent"]
    assert mod.LABEL == "My agent" and mod.info()["available"] is True
    cs = mod.chats(0, 50)
    assert [c["id"] for c in cs] == ["alpha"]
    c = cs[0]
    assert c["name"] == "Fix the login bug" and c["state"] == "idle" and c["provider"] == "myagent" and c["key"] == "myagent:alpha"
    assert c["last_prompt"] == "Thanks, now add a test" and c["final"] == "Added tests/test_auth.py"
    assert c["cwd"] and c["created_at"] < c["updated_at"] + 1
    t = mod.turns("alpha")
    assert [(m["role"], m["text"]) for m in t] == [("user", "Fix the login bug"), ("assistant", "Found it in auth.py"),
                                                    ("user", "Thanks, now add a test"), ("assistant", "Added tests/test_auth.py")]


def test_a_json_file_with_other_role_names_and_a_nested_messages_key(sessions):
    spec = {"id": "other", "files": {"glob": str(sessions / "*.json"), "format": "json", "messages": "messages", "text": "text"}}
    write_cfg([spec])
    mod = providers._modules()["other"]
    assert mod.LABEL == "other"
    assert [(m["role"], m["text"]) for m in mod.turns("beta")] == [("user", "hello"), ("assistant", "hi")]


def test_the_agent_appears_on_the_board_like_codex_and_cursor(sessions, monkeypatch):
    write_cfg([FILES_SPEC(sessions)])
    rows = [r for r in providers.rows(10 ** 9) if r["provider"] == "myagent"]
    assert len(rows) == 1 and rows[0]["provider_label"] == "My agent" and rows[0]["path"] == "myagent:alpha"
    assert board.is_path("myagent:alpha") and not board.is_path("C:\\x\\y") and not board.is_path("/tmp/a:b") and not board.is_path("nope:alpha")
    assert providers.turns_for_path("myagent:alpha")[0]["prompt"] == "Fix the login bug"
    assert any(s["seat"] == "myagent" for s in providers.seats(rows))


def test_a_recently_written_file_reads_as_working_and_refuses_a_reply(sessions):
    write_cfg([FILES_SPEC(sessions)])
    os.utime(sessions / "alpha.jsonl", None)
    mod = providers._modules()["myagent"]
    assert mod.find("alpha")["state"] == "working"
    r = mod.reply("alpha", "hello")
    assert r["ok"] is False and r["route"] == "refused"


def test_reply_runs_the_command_with_the_text_as_one_argument(sessions, tmp_path):
    spec = FILES_SPEC(sessions)
    out = tmp_path / "argv.json"
    spec["reply"] = [sys.executable, "-c", "import sys, json; json.dump(sys.argv[1:], open(sys.argv[1], 'w'))", str(out), "{id}", "{text}"]
    write_cfg([spec])
    text = 'say "hi" && echo {id} $(whoami) | cat\nsecond line'
    r = providers.reply("myagent", "alpha", text)
    assert r["ok"], r
    deadline = time.time() + 15
    while time.time() < deadline and not out.exists():
        time.sleep(0.1)
    assert json.loads(out.read_text())[1:] == ["alpha", text]       # no shell: nothing was expanded or split


def test_no_reply_command_means_read_only(sessions):
    spec = FILES_SPEC(sessions)
    del spec["reply"]
    write_cfg([spec])
    assert providers._modules()["myagent"].info()["can_reply"] is False
    r = providers.reply("myagent", "alpha", "hi")
    assert r["ok"] is False and "read-only" in r["error"]


def test_a_script_agent_lists_and_reads_through_commands(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text("import sys, json, time\n"
                      "if sys.argv[1] == 'list':\n"
                      "    print(json.dumps([{'id': 'c1', 'title': 'Refactor', 'cwd': '/w', 'updated_at': time.time() - 99, 'state': 'idle',\n"
                      "                       'last_prompt': 'p', 'final': 'done', 'model': 'm1'}, {'nope': 1}]))\n"
                      "elif sys.argv[1] == 'turns':\n"
                      "    print(json.dumps([{'role': 'user', 'text': 'hi', 'at': 1}, {'role': 'system', 'text': 'x'}, {'role': 'assistant', 'text': 'yo'}]))\n")
    write_cfg([{"id": "bridge", "label": "Bridge", "list": [sys.executable, str(script), "list"],
                "turns": [sys.executable, str(script), "turns", "{id}"]}])
    mod = providers._modules()["bridge"]
    cs = mod.chats(0, 10)
    assert [(c["id"], c["name"], c["model"], c["final"]) for c in cs] == [("c1", "Refactor", "m1", "done")]
    assert [(m["role"], m["text"]) for m in mod.turns("c1")] == [("user", "hi"), ("assistant", "yo")]


def test_a_failing_script_is_an_error_for_that_agent_only(tmp_path):
    write_cfg([{"id": "bad", "list": [sys.executable, "-c", "import sys; sys.exit(3)"], "turns": [sys.executable, "-c", "pass"]}])
    with pytest.raises(RuntimeError):
        providers._modules()["bad"].chats(0, 5)
    assert [r for r in providers.rows(10 ** 9) if r["provider"] == "bad"] == []          # the board still builds


def test_problems_are_reported_and_good_entries_still_load(sessions):
    write_cfg([FILES_SPEC(sessions), {"id": "codex", "files": {"glob": "x"}}, {"id": "Bad Id", "files": {"glob": "x"}},
               {"id": "noway"}, {"id": "badcmd", "list": "not a list", "turns": ["x"]}, {"id": "myagent", "files": {"glob": "y"}}])
    assert "myagent" in providers._modules()
    probs = providers.problems()
    assert len(probs) == 5 and any("reserved" in p for p in probs) and any("lowercase" in p for p in probs)


def test_invalid_json_is_one_problem_and_the_builtins_survive(tmp_path):
    os.makedirs(os.path.dirname(custom.config_path()), exist_ok=True)
    with open(custom.config_path(), "w") as fh:
        fh.write("{not json")
    custom._cache["sig"] = None
    assert {"codex", "cursor"} <= set(providers._modules()) and providers.problems()[0].startswith("agents.json is not valid")


def test_no_agents_file_is_not_a_problem():
    custom._cache["sig"] = None
    assert providers.problems() == [] and set(providers._modules()) == {"codex", "cursor", "gemini", "antigravity"}


def test_terminal_command_comes_from_the_config(sessions):
    spec = FILES_SPEC(sessions)
    spec["terminal"] = ["myagent", "--resume", "{id}"]
    write_cfg([spec])
    assert providers.terminal_command("myagent", "alpha") == "myagent --resume alpha"
    assert providers.terminal_command("codex", "x1") == "codex resume x1"
    assert providers.terminal_command("nope", "x") is None
