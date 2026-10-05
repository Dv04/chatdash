"""Accounts: sign in through the CLI's own prompt, disconnect, reconnect, delete; against a fake `claude`."""
import os
import time

import pytest

from chatdash import accounts, config

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_claude.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATDASH_ACCOUNTS_ROOT", str(tmp_path / "home"))
    monkeypatch.setenv("CHATDASH_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CHATDASH_TRASH", str(tmp_path / "trash"))
    monkeypatch.setenv("CLAUDE_BIN", FAKE)
    os.makedirs(tmp_path / "home")
    config._CACHE.clear()
    accounts._logins.clear()
    return tmp_path


def wait(name, states, t=10):
    end = time.time() + t
    while time.time() < end:
        v = accounts.login_view(name)["login"]
        if v and v["state"] in states:
            return v
        time.sleep(0.05)
    raise AssertionError(f"login stuck: {accounts.login_view(name)}")


def test_sign_in_with_the_code_prompt_creates_a_signed_in_account(env):
    r = accounts.start_login("work")
    assert r["ok"] and r["created"]
    v = wait("work", ("waiting",))
    assert v["wants_code"] and v["link"].startswith("https://claude.com/") and "localhost" not in v["link"]
    assert accounts.submit_code("work", "good-code")["ok"]
    v = wait("work", ("done", "failed"))
    assert v["state"] == "done" and v["result"]["signed_in"] is True
    rows = {r["name"]: r for r in accounts.listing()}
    assert rows["work"]["signed_in"] and rows["work"]["created_here"] and not rows["work"]["hidden"]


def test_wrong_code_fails_and_bad_names_are_refused(env):
    assert not accounts.start_login("Bad Name!")["ok"]
    assert not accounts.start_login("../etc")["ok"]
    accounts.start_login("w2")
    wait("w2", ("waiting",))
    assert not accounts.submit_code("w2", "two\nlines")["ok"]
    accounts.submit_code("w2", "nope")
    v = wait("w2", ("done", "failed"))
    assert v["state"] == "failed" and accounts.listing()[0]["signed_in"] is False


def test_disconnect_hides_and_reconnect_shows(env):
    accounts.start_login("w3"); wait("w3", ("waiting",)); accounts.submit_code("w3", "good-code"); wait("w3", ("done",))
    assert accounts.disconnect("w3")["ok"]
    assert [r["hidden"] for r in accounts.listing(False)] == [True]
    assert "w3" in accounts.hidden()
    assert accounts.reconnect("w3")["ok"] and accounts.hidden() == set()
    assert accounts.disconnect("ghost")["ok"] is False


def test_delete_needs_the_name_signs_out_and_moves_to_trash(env):
    accounts.start_login("w4"); wait("w4", ("waiting",)); accounts.submit_code("w4", "good-code"); wait("w4", ("done",))
    d = accounts.dir_of("w4")
    assert accounts.delete("w4", "wrong")["ok"] is False and os.path.isdir(d)
    r = accounts.delete("w4", "w4")
    assert r["ok"] and r["signed_out"] and not os.path.exists(d) and os.path.isdir(r["moved_to"])
    assert accounts.listing() == []


def test_delete_refuses_while_a_chat_runs_and_never_moves_main(env):
    accounts.start_login("main"); wait("main", ("waiting",)); accounts.submit_code("main", "good-code"); wait("main", ("done",))
    d = accounts.dir_of("main")
    open(os.path.join(d, "sessions", f"{os.getpid()}.json"), "w").write("{}")       # a live pid: this test process
    assert "running" in accounts.delete("main", "main")["error"]
    os.remove(os.path.join(d, "sessions", f"{os.getpid()}.json"))
    r = accounts.delete("main", "main")
    assert r["ok"] and r["moved_to"] is None and os.path.isdir(d) and "main" in accounts.hidden()


def test_cancel_stops_a_pending_sign_in(env):
    accounts.start_login("w5"); wait("w5", ("waiting",))
    accounts.cancel("w5")
    v = wait("w5", ("cancelled",))
    assert v["state"] == "cancelled"
