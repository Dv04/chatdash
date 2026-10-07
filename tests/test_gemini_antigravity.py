"""Gemini CLI and Antigravity readers, against files laid out the way their sources describe (no real install was available:
these tests pin the documented shapes, not live data)."""
import json
import os
import sqlite3
import sys
import time

import pytest

from dhi_orbit import providers
from dhi_orbit.providers import antigravity, board, gemini


def jl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def ghome(tmp_path, monkeypatch):
    home = tmp_path / "ghome"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    gemini._cache.clear()
    providers._CACHE.clear()
    proj = tmp_path / "work" / "My App"
    proj.mkdir(parents=True)
    root = home / ".gemini"
    chats = root / "tmp" / "my-app" / "chats"
    (root / "projects.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "projects.json").write_text(json.dumps({"projects": {str(proj): "my-app"}}))
    p = chats / "session-2026-10-06T10-00-aaaa1111.jsonl"
    jl(p, [
        {"sessionId": "aaaa1111-0000-4000-8000-000000000001", "projectHash": "h", "startTime": "2026-10-06T10:00:00.000Z",
         "lastUpdated": "2026-10-06T10:05:00.000Z", "kind": "main"},
        {"id": "m1", "timestamp": "2026-10-06T10:00:01.000Z", "type": "user", "content": [{"text": "Explain the build"}]},
        {"id": "m2", "timestamp": "2026-10-06T10:00:09.000Z", "type": "gemini", "content": "It uses make.", "model": "gemini-3-pro",
         "toolCalls": [{"id": "t", "name": "ls"}], "thoughts": [], "tokens": {"input": 5, "output": 3, "total": 8}},
        {"id": "m3", "timestamp": "2026-10-06T10:00:10.000Z", "type": "info", "content": "Switched model"},
        {"id": "m4", "timestamp": "2026-10-06T10:01:00.000Z", "type": "user", "content": "Add a Dockerfile"},
        {"id": "m5", "timestamp": "2026-10-06T10:01:30.000Z", "type": "gemini", "content": "draft"},
        {"$patch": {"id": "m5", "content": "Here is a Dockerfile"}},
        {"$set": {"summary": "Build and Docker", "lastUpdated": "2026-10-06T10:06:00.000Z"}},
    ])
    age(p, 3600)
    sub = chats / "aaaa1111-0000-4000-8000-000000000001" / "sub1.jsonl"
    jl(sub, [{"sessionId": "sub1", "kind": "subagent"}, {"id": "s1", "type": "user", "content": "x"}])
    return home, proj, p


def test_gemini_chat_is_listed_replayed_and_named(ghome):
    home, proj, p = ghome
    assert gemini.info()["available"] is True
    cs = gemini.chats(0, 20)
    assert [c["id"] for c in cs] == ["aaaa1111-0000-4000-8000-000000000001"]          # the sub-agent file is skipped
    c = cs[0]
    assert c["name"] == "Build and Docker" and c["state"] == "idle" and c["model"] == "gemini-3-pro" and c["provider"] == "gemini"
    assert c["last_prompt"] == "Add a Dockerfile" and c["final"] == "Here is a Dockerfile"       # $patch applied
    assert c["cwd"] == str(proj)                                                              # from projects.json by the slug
    assert [(m["role"], m["text"]) for m in gemini.turns(c["id"])] == [
        ("user", "Explain the build"), ("assistant", "It uses make."), ("user", "Add a Dockerfile"), ("assistant", "Here is a Dockerfile")]


def test_gemini_rewind_drops_the_target_and_everything_after_it(ghome):
    """Gemini CLI's own replay: $rewindTo <id> removes that message and every later one (chatRecordingService.ts)."""
    home, proj, p = ghome
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"$rewindTo": "m2"}) + "\n")
        fh.write(json.dumps({"id": "m6", "type": "user", "content": "A different question", "timestamp": "2026-10-06T10:08:00.000Z"}) + "\n")
    age(p, 3500)
    gemini._cache.clear()
    assert [(m["role"], m["text"]) for m in gemini.turns("aaaa1111-0000-4000-8000-000000000001")] == [
        ("user", "Explain the build"), ("user", "A different question")]
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"$rewindTo": "no-such-id"}) + "\n")                       # an unknown id empties the chat
    gemini._cache.clear()
    assert gemini.turns("aaaa1111-0000-4000-8000-000000000001") == [] and gemini.chats(0, 5) == []


def test_gemini_patch_updates_remove_order_legacy_checkpoint_and_ignored_user_text(ghome):
    home, proj, p = ghome
    cid = "aaaa1111-0000-4000-8000-000000000001"
    rows = [
        {"$patch": {"updates": [{"id": "m2", "content": "It uses make, via a Makefile."}], "orderIds": ["m2"]}},     # m2 moves last
        {"id": "m7", "type": "user", "content": "/memory show", "timestamp": "2026-10-06T10:09:00.000Z"},            # a slash command
        {"id": "m8", "type": "user", "content": "<session_context>x</session_context>"},
        {"id": "m9", "type": "user", "content": "?", "timestamp": "2026-10-06T10:09:30.000Z"},
    ]
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    gemini._cache.clear()
    assert [m["text"] for m in gemini.turns(cid)] == ["Explain the build", "Add a Dockerfile", "Here is a Dockerfile", "It uses make, via a Makefile."]
    with open(p, "a", encoding="utf-8") as fh:                                          # a legacy full-history checkpoint replaces all
        fh.write(json.dumps({"$set": {"messages": [{"id": "z1", "type": "user", "content": "from a checkpoint"},
                                                    {"id": "z2", "type": "gemini", "content": "ok"}]}}) + "\n")
        fh.write(json.dumps({"id": "z2", "type": "gemini", "content": "ok, replaced in place"}) + "\n")       # same id: replaced, same place
    gemini._cache.clear()
    assert [(m["role"], m["text"]) for m in gemini.turns(cid)] == [("user", "from a checkpoint"), ("assistant", "ok, replaced in place")]


def test_gemini_chat_with_only_commands_is_not_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "h"))
    p = tmp_path / "h" / ".gemini" / "tmp" / "proj" / "chats" / "session-2026-10-06T10-00-cccc3333.jsonl"
    jl(str(p), [{"sessionId": "cccc3333", "projectHash": "h", "startTime": "2026-10-06T10:00:00Z"},
                {"id": "a", "type": "user", "content": "/help", "timestamp": "2026-10-06T10:00:01Z"}])
    gemini._cache.clear()
    assert gemini.info()["available"] is True and gemini.chats(0, 5) == []


def test_gemini_survives_a_half_written_line_and_a_legacy_json_file(ghome):
    home, proj, p = ghome
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"id": "m9", "type": "us')                          # the CLI is mid-write
    legacy = os.path.join(os.path.dirname(p), "session-2026-10-01T09-00-bbbb2222.json")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"sessionId": "bbbb2222", "startTime": "2026-10-01T09:00:00Z", "lastUpdated": "2026-10-01T09:30:00Z",
                   "messages": [{"id": "a", "type": "user", "content": "old chat"}, {"id": "b", "type": "gemini", "content": "old answer"}]}, fh)
    age(legacy, 7200)
    gemini._cache.clear()
    ids = {c["id"] for c in gemini.chats(0, 20)}
    assert ids == {"aaaa1111-0000-4000-8000-000000000001", "bbbb2222"}
    assert gemini.turns("bbbb2222")[-1]["text"] == "old answer"


def test_gemini_reply_runs_resume_in_the_project_folder(ghome, monkeypatch):
    home, proj, p = ghome
    seen = {}
    monkeypatch.setattr(gemini, "exe", lambda: "/bin/gemini")
    monkeypatch.setattr(gemini, "run_detached", lambda argv, cwd, env, tag, settle_s=6.0: seen.update(argv=argv, cwd=cwd) or {"ok": True, "started": True})
    r = gemini.reply("aaaa1111-0000-4000-8000-000000000001", "go on")
    assert r["ok"] and seen["argv"] == ["/bin/gemini", "--resume", "aaaa1111-0000-4000-8000-000000000001", "-p", "go on"] and seen["cwd"] == str(proj)


def test_gemini_reply_refused_without_a_project_folder_or_while_working(ghome, monkeypatch):
    home, proj, p = ghome
    monkeypatch.setattr(gemini, "exe", lambda: "/bin/gemini")
    os.utime(p, None)
    gemini._cache.clear()
    assert gemini.reply("aaaa1111-0000-4000-8000-000000000001", "x")["route"] == "refused"
    age(p, 3600)
    gemini._cache.clear()
    (home / ".gemini" / "projects.json").write_text("{}")
    r = gemini.reply("aaaa1111-0000-4000-8000-000000000001", "x")
    assert r["ok"] is False and "project folder" in r["error"]


def test_gemini_is_on_the_board_with_unknown_limits(ghome):
    rows = [r for r in providers.rows(10 ** 9) if r["provider"] == "gemini"]
    assert len(rows) == 1 and rows[0]["provider_label"] == "Gemini" and board.is_path(rows[0]["path"])
    seat = {s["seat"]: s for s in providers.seats(rows)}["gemini"]
    assert seat["state"] == "unknown" and seat["five_hour"]["pct"] is None


def test_gemini_without_any_chats_is_unavailable_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "empty"))
    assert gemini.info()["available"] is False and gemini.chats(0, 5) == []


# ---------------------------------------------------------------- Antigravity
@pytest.fixture
def agy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    antigravity._cache.clear()
    providers._CACHE.clear()
    root = tmp_path / ".gemini" / "antigravity-cli"
    cid = "11111111-2222-4333-8444-555555555555"
    logs = root / "brain" / cid / ".system_generated" / "logs"
    jl(str(logs / "transcript.jsonl"), [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-10-06T11:00:00Z",
         "content": "<USER_REQUEST>\nRename the module\n</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-10-06T11:00:05Z", "content": "Renamed it.",
         "thinking": "..."},
        {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND", "created_at": "2026-10-06T11:00:06Z", "content": "git mv a b"},
        {"step_index": 3, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-10-06T11:02:00Z", "content": "Now run the tests"},
        {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-10-06T11:02:30Z", "content": "All green."},
    ])
    jl(str(root / "history.jsonl"), [{"conversationId": cid, "title": "Rename work", "workspace": str(tmp_path)}])
    age(str(logs / "transcript.jsonl"), 3600)
    return root, cid


def test_antigravity_chat_is_read_from_the_transcript(agy_home, tmp_path):
    root, cid = agy_home
    assert antigravity.info()["available"] is True
    cs = antigravity.chats(0, 10)
    assert [c["id"] for c in cs] == [cid]
    c = cs[0]
    assert c["name"] == "Rename work" and c["cwd"] == str(tmp_path) and c["final"] == "All green." and c["last_prompt"] == "Now run the tests"
    assert [(m["role"], m["text"]) for m in antigravity.turns(cid)] == [
        ("user", "Rename the module"), ("assistant", "Renamed it."), ("user", "Now run the tests"), ("assistant", "All green.")]


def test_antigravity_prefers_the_full_transcript_and_names_a_chat_without_history(agy_home):
    root, cid = agy_home
    os.remove(root / "history.jsonl")
    jl(str(root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"), [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-10-06T11:00:00Z", "content": "Only the full one"}])
    antigravity._cache.clear()
    c = antigravity.chats(0, 5)[0]
    assert c["name"] == "Only the full one" and c["cwd"] is None


def test_antigravity_reply_uses_agy_print_mode(agy_home, monkeypatch):
    root, cid = agy_home
    seen = {}
    monkeypatch.setattr(antigravity, "exe", lambda: "/bin/agy")
    monkeypatch.setattr(antigravity, "run_detached", lambda argv, cwd, env, tag, settle_s=6.0: seen.update(argv=argv) or {"ok": True})
    assert antigravity.reply(cid, "ship it")["ok"] and seen["argv"] == ["/bin/agy", "-p", "ship it", "--conversation", cid]


def test_antigravity_without_transcripts_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    i = antigravity.info()
    assert i["available"] is False and "no Antigravity conversations" in i["detail"]


def test_both_are_built_in_ids_and_cannot_be_redefined_in_agents_json(tmp_path):
    assert {"gemini", "antigravity", "codex", "cursor"} <= set(providers._modules())
    from dhi_orbit.providers import custom
    assert {"gemini", "antigravity"} <= custom.RESERVED


# the exact table agy 1.3.1 creates (read from a real install)
AGY_SUMMARIES_SQL = ("CREATE TABLE `conversation_summaries` (`conversation_id` text,`title` text NOT NULL DEFAULT \"\",`preview` text NOT NULL DEFAULT \"\","
                     "`step_count` integer NOT NULL DEFAULT 0,`last_modified_time` datetime NOT NULL,`workspace_uris` text NOT NULL,"
                     "`status` text NOT NULL DEFAULT \"\",`source` text NOT NULL DEFAULT \"\",`project_id` text NOT NULL DEFAULT \"\","
                     "`agent_name` text NOT NULL DEFAULT \"\",`parent_conversation_id` text NOT NULL DEFAULT \"\",`nesting_depth` integer NOT NULL DEFAULT 0,"
                     "`battle_id` text NOT NULL DEFAULT \"\",`winning_conversation_id` text NOT NULL DEFAULT \"\",`not_fully_idle` numeric NOT NULL DEFAULT false,"
                     "`killed` numeric NOT NULL DEFAULT false,`last_user_input_time` datetime NOT NULL,`last_user_input_step_index` integer NOT NULL DEFAULT -1,"
                     "`app_data_dir` text NOT NULL DEFAULT \"\",`raw_summary` blob,`group_id` text NOT NULL DEFAULT \"\",PRIMARY KEY (`conversation_id`))")


def summaries_db(root, rows):
    os.makedirs(root, exist_ok=True)
    con = sqlite3.connect(os.path.join(root, "conversation_summaries.db"))
    con.execute(AGY_SUMMARIES_SQL)
    for r in rows:
        con.execute("INSERT INTO conversation_summaries(conversation_id,title,preview,step_count,last_modified_time,workspace_uris,status,"
                    "parent_conversation_id,nesting_depth,not_fully_idle,killed,last_user_input_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


def test_antigravity_lists_from_the_real_summaries_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    antigravity._cache.clear()
    root = tmp_path / ".gemini" / "antigravity-cli"
    old = "2026-10-06 11:00:00+00:00"
    summaries_db(str(root), [
        ("c-idle", "Refactor auth", "make it simpler", 12, old, "[\"file:///Users/me/proj%20one\"]", "", "", 0, 0, 0, old),
        ("c-busy", "Running now", "go", 3, old, "file:///work/b", "", "", 0, 1, 0, old),
        ("c-dead", "Killed one", "x", 3, old, "", "", "", 0, 0, 1, old),
        ("c-child", "A sub-agent", "x", 3, old, "", "", "c-idle", 1, 0, 0, old),
    ])
    cs = {c["id"]: c for c in antigravity.chats(0, 20)}
    assert set(cs) == {"c-idle", "c-busy", "c-dead"}                      # the nested sub-agent run is not a chat
    assert cs["c-idle"]["name"] == "Refactor auth" and cs["c-idle"]["cwd"] == "/Users/me/proj one" and cs["c-idle"]["state"] == "idle"
    assert cs["c-busy"]["state"] == "working" and cs["c-dead"]["state"] == "stopped"
    assert cs["c-idle"]["last_prompt"] == "make it simpler" and antigravity.turns("c-idle") == []     # no transcript: nothing to show
    assert antigravity.info()["available"] is True


def test_antigravity_summary_title_and_state_win_and_the_transcript_adds_the_text(agy_home, tmp_path):
    root, cid = agy_home
    summaries_db(str(root), [(cid, "Summary title", "p", 5, "2026-10-06 11:03:00+00:00", "file:///w", "", "", 0, 0, 0, "2026-10-06 11:02:00+00:00")])
    antigravity._cache.clear()
    c = antigravity.chats(0, 5)[0]
    assert c["name"] == "Summary title" and c["cwd"] == "/w" and c["final"] == "All green."
    assert antigravity.turns(cid)[-1]["text"] == "All green."


def test_a_corrupt_or_unfamiliar_summaries_database_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".gemini" / "antigravity-cli"
    os.makedirs(root)
    (root / "conversation_summaries.db").write_bytes(b"not a database")
    assert antigravity.chats(0, 5) == [] and antigravity.info()["available"] is False
