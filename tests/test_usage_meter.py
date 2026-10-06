"""Usage limits without setup: DHI Orbit's status line meter per account (turn on, keep the owner's own status line
running, restore it exactly, record the limits), and the account flows that use it."""
import io
import json
import os
import subprocess
import sys

import pytest

from dhi_orbit import accounts, config, statusline, usage_meter

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_claude.py")
DOC = {"session_id": "s1", "model": {"display_name": "Opus"},
       "rate_limits": {"five_hour": {"used_percentage": 12.4, "resets_at": 1791247800},
                       "seven_day": {"used_percentage": 40, "resets_at": 1791716400}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DHI_ORBIT_ACCOUNTS_ROOT", str(tmp_path / "home"))
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("DHI_ORBIT_TRASH", str(tmp_path / "trash"))
    monkeypatch.setenv("CLAUDE_BIN", FAKE)
    monkeypatch.delenv("CP_METER_LOG", raising=False)
    os.makedirs(tmp_path / "home")
    config._CACHE.clear()
    accounts._logins.clear()
    usage_meter._cache.clear()
    return tmp_path


def acct(env, name, settings=None):
    d = env / "home" / f".claude-{name}"
    os.makedirs(d / "projects")
    os.makedirs(d / "sessions")
    if settings is not None:
        (d / "settings.json").write_text(settings if isinstance(settings, str) else json.dumps(settings, indent=2))
    return str(d)


def settings(d):
    return json.load(open(os.path.join(d, "settings.json")))


def run_statusline(d, doc, env):
    """Run the command DHI Orbit wrote, through a shell, the way Claude Code runs a status line."""
    cmd = settings(d)["statusLine"]["command"]
    e = dict(os.environ, CLAUDE_CONFIG_DIR=d, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = subprocess.run(cmd, shell=True, input=json.dumps(doc).encode(), capture_output=True, env=e, timeout=20)
    return p


def test_turn_on_a_fresh_account_writes_settings_and_the_status_line_records_limits(env):
    d = acct(env, "work")
    assert usage_meter.status(d) == {"on": False, "wrapped": False, "other": False, "error": None}
    r = usage_meter.turn_on(d)
    assert r == {"ok": True, "on": True, "wrapped": False}
    sl = settings(d)["statusLine"]
    assert sl["type"] == "command" and usage_meter.MARK in sl["command"] and str(env / "data") in sl["command"]
    p = run_statusline(d, DOC, env)
    assert p.returncode == 0 and p.stdout.decode() == "Opus  5h 12%  7d 40%"
    line = open(env / "data" / "meter.log").read().strip().split("\t")
    assert line[1] == d and line[2] == "s1" and json.loads(line[3])["seven_day"]["used_percentage"] == 40
    run_statusline(d, DOC, env)                                    # unchanged numbers: no second line
    assert len(open(env / "data" / "meter.log").read().strip().splitlines()) == 1
    assert usage_meter.turn_on(d)["on"] and settings(d)["statusLine"] == sl     # idempotent


def test_an_existing_status_line_keeps_running_and_is_restored_exactly(env):
    own = {"type": "command", "command": "printf 'mine:%s' \"$(cat | head -c 13)\"", "padding": 2}
    d = acct(env, "dev", {"model": "opus", "statusLine": own, "permissions": {"allow": ["Bash(ls)"]}})
    before = settings(d)
    r = usage_meter.turn_on(d)
    assert r["wrapped"] is True and usage_meter.status(d)["wrapped"] is True
    sl = settings(d)["statusLine"]
    assert usage_meter.MARK in sl["command"] and sl["padding"] == 2
    assert settings(d)["permissions"] == before["permissions"] and settings(d)["model"] == "opus"
    p = run_statusline(d, DOC, env)
    assert p.stdout.decode() == 'mine:{"session_id"'               # the owner's output, from the same input
    assert os.path.exists(env / "data" / "meter.log")             # and the limits were still recorded
    assert usage_meter.turn_off(d) == {"ok": True, "on": False, "restored": True}
    assert settings(d) == before and not os.path.exists(os.path.join(d, usage_meter.SAVED))


def test_turn_off_leaves_a_status_line_the_owner_changed_and_refuses_unreadable_settings(env):
    d = acct(env, "a")
    usage_meter.turn_on(d)
    s = settings(d)
    s["statusLine"] = {"type": "command", "command": "echo changed-by-owner"}
    (env / "home" / ".claude-a" / "settings.json").write_text(json.dumps(s))
    r = usage_meter.turn_off(d)
    assert r["ok"] and "left as it is" in r["note"] and settings(d)["statusLine"]["command"] == "echo changed-by-owner"
    d2 = acct(env, "b", "{ not json")
    r = usage_meter.turn_on(d2)
    assert not r["ok"] and "left alone" in r["error"]
    assert open(os.path.join(d2, "settings.json")).read() == "{ not json"
    assert usage_meter.status(d2)["error"]


def test_statusline_never_fails_on_bad_input(env, monkeypatch):
    out = io.BytesIO()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env / "nowhere"))
    assert statusline.main(stdin=io.BytesIO(b"not json"), stdout=out) == 0
    assert out.getvalue() == b""


def test_connect_turns_the_meter_on_disconnect_restores_and_reconnect_turns_it_back(env):
    r = accounts.start_login("work")
    assert r["ok"] and r["created"]
    accounts.cancel("work")
    d = accounts.dir_of("work")
    assert usage_meter.status(d)["on"]
    row = {x["name"]: x for x in accounts.listing(with_status=False)}["work"]
    assert row["usage_meter"]["on"] is True
    assert accounts.disconnect("work")["ok"]
    assert not usage_meter.status(d)["on"] and "statusLine" not in settings(d)
    assert accounts.reconnect("work")["ok"] and usage_meter.status(d)["on"]
    assert accounts.usage("work", False)["ok"] and not usage_meter.status(d)["on"]
    assert accounts.disconnect("work")["ok"] and accounts.reconnect("work")["ok"]
    assert not usage_meter.status(d)["on"]                        # turned off by hand: reconnect leaves it off


def test_connect_never_wraps_a_status_line_by_itself(env):
    own = {"type": "command", "command": "echo mine"}
    d = acct(env, "dev", {"statusLine": own})
    accounts.start_login("dev")
    accounts.cancel("dev")
    assert settings(d)["statusLine"] == own and usage_meter.status(d)["other"]
    assert accounts.usage("dev", True)["wrapped"] is True


def test_seat_rows_say_whether_the_meter_is_on(env):
    from dhi_orbit.cp import sources
    d = acct(env, "work")
    assert usage_meter.meter_state(d) == "off"
    usage_meter.turn_on(d)
    assert usage_meter.meter_state(d) == "on"
    usage_meter.turn_off(d)
    assert usage_meter.meter_state(d) == "off"
    assert hasattr(sources, "usage_meter")


def test_names_are_dhi_orbit_and_the_command_pins_the_new_home_variable(env):
    assert usage_meter.SAVED == "dhi-orbit-statusline.json" and usage_meter.MARK == "dhi_orbit.statusline"
    assert usage_meter.command().startswith(f"DHI_ORBIT_HOME={env / 'data'} ")
    assert f"-m {usage_meter.MARK}" in usage_meter.command()


# a status line as chatdash (before the rename) wrote it: its command names chatdash.statusline and pins CHATDASH_HOME
LEGACY_CMD = "CHATDASH_HOME=/old/data /usr/bin/python3 -m chatdash.statusline"


def test_a_meter_installed_by_chatdash_is_detected_and_turn_on_rewrites_it_to_the_new_command(env):
    own = {"type": "command", "command": "echo mine", "padding": 1}
    d = acct(env, "old", {"statusLine": {"type": "command", "command": LEGACY_CMD, "padding": 3}})
    (env / "home" / ".claude-old" / "chatdash-statusline.json").write_text(json.dumps({"statusLine": own}))
    assert usage_meter.is_ours({"command": LEGACY_CMD}) and not usage_meter.is_ours({"command": "echo mine"})
    assert usage_meter.status(d) == {"on": True, "wrapped": True, "other": False, "error": None}
    assert usage_meter.meter_state(d) == "on"
    assert usage_meter.turn_on(d) == {"ok": True, "on": True, "wrapped": True}
    sl = settings(d)["statusLine"]
    assert sl["command"] == usage_meter.command() and "chatdash.statusline" not in sl["command"] and sl["padding"] == 3
    assert usage_meter.saved_command(d) == "echo mine"                 # the legacy saved file still feeds the wrapper
    p = run_statusline(d, DOC, env)
    assert p.returncode == 0 and p.stdout.decode().strip() == "mine"   # `echo mine` ran, the limits were recorded
    assert os.path.exists(env / "data" / "meter.log")


def test_turn_off_restores_the_status_line_from_the_legacy_saved_file_and_removes_it(env):
    own = {"type": "command", "command": "echo mine", "padding": 1}
    d = acct(env, "old", {"model": "opus", "statusLine": {"type": "command", "command": LEGACY_CMD}})
    legacy = env / "home" / ".claude-old" / "chatdash-statusline.json"
    legacy.write_text(json.dumps({"statusLine": own}))
    assert usage_meter.turn_off(d) == {"ok": True, "on": False, "restored": True}
    assert settings(d) == {"model": "opus", "statusLine": own} and not legacy.exists()


def test_the_new_saved_file_wins_over_a_stale_legacy_one_and_new_wraps_use_the_new_name(env):
    d = acct(env, "mix", {"statusLine": {"type": "command", "command": "echo mine"}})
    (env / "home" / ".claude-mix" / "chatdash-statusline.json").write_text(json.dumps({"statusLine": {"command": "stale"}}))
    usage_meter.turn_on(d)
    assert os.path.exists(os.path.join(d, usage_meter.SAVED)) and usage_meter.saved_command(d) == "echo mine"
    assert usage_meter.turn_off(d)["restored"] and settings(d)["statusLine"]["command"] == "echo mine"
    assert not os.path.exists(os.path.join(d, usage_meter.SAVED))
