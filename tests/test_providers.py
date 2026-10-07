"""Codex and Cursor on the board (dhi_orbit/providers): read from synthetic stores built in the same layout the real
tools write (verified 2026-10-06 against Codex 0.160.1 and Cursor 2026.09.18), reply argv, refusals and id safety."""
import json
import os
import sqlite3
import time

import pytest

from dhi_orbit import providers
from dhi_orbit.providers import codex, cursor


# ------------------------------------------------------------------ Codex
@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    h = tmp_path / "codex"
    h.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(h))
    monkeypatch.setattr(providers, "_CACHE", {})
    st = sqlite3.connect(h / "state_5.sqlite")
    st.execute("""CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, source TEXT,
        model_provider TEXT, cwd TEXT, title TEXT, archived INTEGER DEFAULT 0, first_user_message TEXT DEFAULT '', name TEXT,
        model TEXT, history_mode TEXT DEFAULT 'paginated', agent_nickname TEXT)""")
    hi = sqlite3.connect(h / "thread_history_1.sqlite")
    hi.execute("""CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT, error_json TEXT,
        started_at INTEGER, completed_at INTEGER, first_user_item_id TEXT, final_agent_item_id TEXT)""")
    hi.execute("""CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER,
        item_json TEXT, item_type TEXT)""")
    now = int(time.time())

    def thread(tid, source, upd, title, status, err=None, cwd="/w"):
        st.execute("INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,first_user_message) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (tid, "", upd - 60, upd, source, "openai", cwd, title, title))
        hi.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?,?)", (tid, "t1", 1, status, err, upd - 50, upd, f"{tid}-u", f"{tid}-a"))
        hi.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)", (tid, "t1", f"{tid}-u", 1, (upd - 50) * 1000,
                   json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "question " + tid}]}), "userMessage"))
        hi.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)", (tid, "t1", f"{tid}-a", 2, upd * 1000,
                   json.dumps({"type": "agentMessage", "text": "answer " + tid, "phase": "final_answer"}), "agentMessage"))
    thread("idle1", "cli", now - 3600, "An idle one", "completed")
    thread("work1", "vscode", now - 20, "Busy now", "inProgress")
    thread("dead1", "cli", now - 7200, "Dead record", "inProgress")
    thread("fail1", "cli", now - 100, "Out of credits", "failed", json.dumps({"message": "Your workspace is out of credits."}))
    thread("sub1", json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "idle1"}}}), now - 10, "Sub", "completed")
    st.commit()
    hi.commit()
    st.close()
    hi.close()
    return h


def test_codex_listing_states_and_text(codex_home):
    cs = {c["id"]: c for c in codex.chats(time.time() - 86400, 50)}
    assert set(cs) == {"idle1", "work1", "dead1", "fail1"}, "sub-agent threads are not chats"
    assert cs["idle1"]["state"] == "idle" and cs["idle1"]["final"] == "answer idle1" and cs["idle1"]["last_prompt"] == "question idle1"
    assert cs["work1"]["state"] == "working"
    assert cs["dead1"]["state"] == "stopped", "an inProgress turn nobody touched for 2 h is a dead record"
    assert cs["fail1"]["state"] == "failed" and cs["fail1"]["error"] == "Your workspace is out of credits."
    assert cs["idle1"]["provider"] == "codex" and cs["idle1"]["key"] == "codex:idle1"
    assert [c["id"] for c in codex.chats(time.time() - 86400, 50)][0] == "work1", "newest first"


def test_codex_turns(codex_home):
    t = codex.turns("idle1")
    assert [(x["role"], x["text"]) for x in t] == [("user", "question idle1"), ("assistant", "answer idle1")]


def test_codex_reply_runs_exec_resume_in_the_thread_folder(codex_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(codex, "exe", lambda: "/bin/codex")
    monkeypatch.setattr(codex, "run_detached", lambda argv, cwd, env, tag, settle_s=6.0: seen.update(argv=argv, cwd=cwd, tag=tag) or {"ok": True, "started": True})
    r = providers.reply("codex", "idle1", "  hello there  ")
    assert r["ok"] and r["route"] == "codex exec resume"
    assert seen["argv"] == ["/bin/codex", "exec", "resume", "--skip-git-repo-check", "idle1", "hello there"] and seen["cwd"] == "/w"


def test_codex_reply_refused_while_working_or_unknown(codex_home, monkeypatch):
    monkeypatch.setattr(codex, "exe", lambda: "/bin/codex")
    monkeypatch.setattr(codex, "run_detached", lambda *a, **k: pytest.fail("must not start a second writer"))
    r = providers.reply("codex", "work1", "x")
    assert not r["ok"] and r["route"] == "refused"
    assert not providers.reply("codex", "nope", "x")["ok"]
    assert not providers.reply("codex", "idle1", "   ")["ok"]
    assert not providers.reply("nobody", "idle1", "x")["ok"]


def test_codex_missing_store_is_unavailable_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))
    monkeypatch.setattr(providers, "_CACHE", {})
    assert codex.info()["available"] is False and codex.chats(0) == []


# ------------------------------------------------------------------ Cursor
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _root(ids):
    b = b""
    for i in ids:
        b += b"\x0a\x20" + bytes.fromhex(i)
    return b + b"\x50" + _varint(5)            # an unrelated varint field after the list, as in real roots


@pytest.fixture
def cursor_home(tmp_path, monkeypatch):
    h = tmp_path / "cursor"
    monkeypatch.setenv("DHI_ORBIT_CURSOR_HOME", str(h))
    monkeypatch.setattr(providers, "_CACHE", {})
    cursor._PARSED.clear()
    now = time.time()

    def chat(cid, title, upd, msgs, convo=True):
        d = h / "ws1" / cid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"schemaVersion": 1, "createdAtMs": int((upd - 100) * 1000), "hasConversation": convo,
                                                 "title": title, "updatedAtMs": int(upd * 1000), "cwd": "/proj"}))
        db = sqlite3.connect(d / "store.db")
        db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        ids = []
        for n, m in enumerate(msgs):
            bid = f"{n + 1:064x}"
            ids.append(bid)
            db.execute("INSERT INTO blobs VALUES (?,?)", (bid, json.dumps(m).encode()))
        db.execute("INSERT INTO blobs VALUES (?,?)", ("ab" * 32, _root(ids)))
        db.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps({"agentId": cid, "latestRootBlobId": "ab" * 32}).encode().hex(),))
        db.commit()
        db.close()
    msgs = [{"role": "system", "content": "rules"},
            {"role": "user", "content": "<user_info>\nOS\n</user_info>"},
            {"role": "user", "content": [{"type": "text", "text": "<user_query>\nwhat is 2+2\n</user_query>"}]},
            {"role": "assistant", "content": [{"type": "reasoning", "text": "", "signature": "x"}, {"type": "tool-call", "toolName": "Shell"}]},
            {"role": "tool", "content": [{"type": "tool-result", "result": "4"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "It is 4."}]}]
    chat("aaaaaaaa-1111-2222-3333-444444444444", "Math", now - 3600, msgs)
    chat("bbbbbbbb-1111-2222-3333-444444444444", "Fresh", now - 5, msgs)
    chat("cccccccc-1111-2222-3333-444444444444", "Empty", now - 10, [], convo=False)
    return h


def test_cursor_root_ids_skip_other_fields():
    ids = [f"{i:064x}" for i in (1, 2, 3)]
    assert cursor._root_ids(_root(ids)) == ids


def test_cursor_listing_and_text(cursor_home):
    cs = {c["name"]: c for c in cursor.chats(time.time() - 86400, 50)}
    assert set(cs) == {"Math", "Fresh"}, "a chat with no conversation is not listed"
    m = cs["Math"]
    assert m["final"] == "It is 4." and m["last_prompt"] == "what is 2+2" and m["cwd"] == "/proj" and m["state"] == "idle"
    assert cs["Fresh"]["state"] == "working"


def test_cursor_turns_drop_context_tools_and_reasoning(cursor_home):
    t = cursor.turns("aaaaaaaa-1111-2222-3333-444444444444")
    assert [(x["role"], x["text"]) for x in t] == [("user", "what is 2+2"), ("assistant", "It is 4.")]


def test_cursor_ids_cannot_escape_the_chats_folder(cursor_home):
    assert cursor.find("../../etc") is None and cursor.turns("../../x") == [] and cursor.find("a/b") is None
    assert not providers.reply("cursor", "../../etc", "x")["ok"]


def test_cursor_reply_argv(cursor_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(cursor, "exe", lambda: "/bin/cursor-agent")
    monkeypatch.setattr(cursor, "run_detached", lambda argv, cwd, env, tag, settle_s=6.0: seen.update(argv=argv, cwd=cwd) or {"ok": True, "started": True})
    r = providers.reply("cursor", "aaaaaaaa-1111-2222-3333-444444444444", "next step")
    assert r["ok"] and seen["argv"] == ["/bin/cursor-agent", "-p", "--trust", "--resume", "aaaaaaaa-1111-2222-3333-444444444444", "next step"]
    assert seen["cwd"] == "/proj"


# ------------------------------------------------------------------ shared
def test_overview_survives_one_provider_failing(codex_home, cursor_home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("corrupt store")
    monkeypatch.setattr(codex, "chats", boom)
    monkeypatch.setattr(providers, "_CACHE", {})
    ov = {p["id"]: p for p in providers.overview(86400)}
    assert "corrupt store" in ov["codex"]["error"] and ov["codex"]["chats"] == []
    assert {c["name"] for c in ov["cursor"]["chats"]} == {"Math", "Fresh"}


def test_run_detached_reports_early_failure_and_start(tmp_path, monkeypatch):
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "home"))
    import sys
    bad = providers.run_detached([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], str(tmp_path), None, "t", settle_s=10)
    assert bad["ok"] is False and "code 3" in bad["error"] and "boom" in bad["error"]
    ok = providers.run_detached([sys.executable, "-c", "print('done')"], str(tmp_path), None, "t", settle_s=10)
    assert ok["ok"] and ok.get("finished") and "done" in ok["tail"]
    run = providers.run_detached([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path), None, "t", settle_s=0.3)
    assert run["ok"] and run["started"]
    os.kill(run["pid"], 15)
    assert not providers.run_detached(["/no/such/binary"], str(tmp_path), None, "t")["ok"]
