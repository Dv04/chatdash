"""Accounts: sign in through the CLI's own prompt, disconnect, reconnect, delete; against a fake `claude`."""
import os
import time

import pytest

from dhi_orbit import accounts, config

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_claude.py")


@pytest.fixture
def env(tmp_path, monkeypatch, fake_claude_bin):
    monkeypatch.setenv("DHI_ORBIT_ACCOUNTS_ROOT", str(tmp_path / "home"))
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("DHI_ORBIT_TRASH", str(tmp_path / "trash"))
    monkeypatch.setenv("CLAUDE_BIN", fake_claude_bin)
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


def test_wrong_code_is_reported_then_a_right_code_or_start_over_works(env):
    assert not accounts.start_login("Bad Name!")["ok"]
    assert not accounts.start_login("../etc")["ok"]
    accounts.start_login("w2")
    wait("w2", ("waiting",))
    assert not accounts.submit_code("w2", "two\nlines")["ok"]
    accounts.submit_code("w2", "nope")
    end = time.time() + 5
    while time.time() < end and not accounts.login_view("w2")["login"]["notice"]:
        time.sleep(0.05)
    v = accounts.login_view("w2")["login"]
    assert v["state"] == "waiting" and v["notice"].lower().startswith("invalid code")
    assert accounts.listing()[0]["signed_in"] is False
    first = accounts._logins["w2"]
    assert accounts.start_login("w2", restart=True)["ok"] and accounts._logins["w2"] is not first
    wait("w2", ("waiting",))
    assert accounts.login_view("w2")["login"]["notice"] is None
    accounts.submit_code("w2", "good-code")
    assert wait("w2", ("done", "failed"))["state"] == "done"


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


def test_account_marker_is_the_new_name_and_the_legacy_chatdash_marker_is_still_recognised(env):
    assert accounts.MARK == ".dhi-orbit-account" and accounts.LEGACY_MARK == ".chatdash-account"
    for name, mark in (("old", accounts.LEGACY_MARK), ("new", accounts.MARK), ("plain", None)):
        d = accounts.dir_of(name)
        os.makedirs(os.path.join(d, "projects"))
        os.makedirs(os.path.join(d, "sessions"))
        if mark:
            open(os.path.join(d, mark), "w").close()
    rows = {x["name"]: x for x in accounts.listing(with_status=False)}
    assert rows["old"]["created_here"] and rows["new"]["created_here"] and not rows["plain"]["created_here"]
    accounts.start_login("fresh")
    accounts.cancel("fresh")
    assert os.path.exists(os.path.join(accounts.dir_of("fresh"), ".dhi-orbit-account"))
    assert not os.path.exists(os.path.join(accounts.dir_of("fresh"), ".chatdash-account"))
