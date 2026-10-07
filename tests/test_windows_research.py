"""Windows causes found in the Claude Code docs (2026-10-06): sessions/ is undocumented, `claude agents --json` is the stable
interface, npm installs go through a .cmd shim that cmd.exe garbles, and a chat open in a terminal cannot be typed into."""
import json
import os
import stat
import sys
import time

import pytest

from dhi_orbit import accounts, actions, collector, config, _plat


def test_account_with_only_projects_folder_is_discovered(tmp_path, monkeypatch):
    monkeypatch.setenv("DHI_ORBIT_ACCOUNTS_ROOT", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)                 # no sessions/: not in the docs
    (tmp_path / ".claude-w" / "sessions").mkdir(parents=True)               # no projects/: nothing to show
    (tmp_path / ".claude.json").write_text("{}")
    assert [os.path.basename(d) for d in accounts.discovered()] == [".claude"]


def _fake_claude(tmp_path, payload):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\nif [ \"$1\" = agents ] && [ \"$2\" = --json ]; then cat <<'EOF'\n" + json.dumps(payload) + "\nEOF\nfi\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


@pytest.mark.skipif(_plat.IS_WIN, reason="shell script stand-in for claude")
def test_live_sessions_fall_back_to_claude_agents_json(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude"
    (cfg / "projects" / "p").mkdir(parents=True)
    (cfg / "projects" / "p" / "s1.jsonl").write_text("{}\n")                # written just now
    monkeypatch.setenv("CLAUDE_BIN", _fake_claude(tmp_path, [
        {"pid": os.getpid(), "id": "ab12cd34", "cwd": "/w", "kind": "interactive", "startedAt": 1, "sessionId": "s1", "name": "n", "status": "busy"},
        {"id": "ff00ff00", "cwd": "/w", "kind": "background", "startedAt": 1, "sessionId": "s2", "state": "blocked"}]))
    collector._AGENTS.clear()
    live = collector.live_sessions(str(cfg))
    assert [(r["sessionId"], r["kind"], r["status"]) for r in live] == [("s1", "interactive", "busy")], \
        "a background row without a pid is a job record, not a running process"


@pytest.mark.skipif(_plat.IS_WIN, reason="shell script stand-in for claude")
def test_agents_json_is_not_asked_when_nothing_was_written_recently(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude"
    (cfg / "projects" / "p").mkdir(parents=True)
    t = cfg / "projects" / "p" / "old.jsonl"
    t.write_text("{}\n")
    os.utime(t, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "does-not-exist"))
    collector._AGENTS.clear()
    assert collector.live_sessions(str(cfg)) == []


def test_windows_prefers_the_native_exe_over_an_npm_shim(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    real = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_text("")
    shim = npm / "claude.cmd"
    shim.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    assert config.windows_native_claude(str(shim)) == str(real)
    assert config.windows_native_claude(str(real)) == str(real)
    native = tmp_path / "nohome" / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_text("")
    assert config.windows_native_claude(str(shim)) == str(native), "the native installer's exe wins"
    native.unlink()
    real.unlink()
    assert config.windows_native_claude(str(shim)) == str(shim), "nothing better: keep the shim, replies are then checked"


def test_cmd_shim_refuses_text_cmd_exe_would_garble(monkeypatch):
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setattr(actions, "CLAUDE", "C:\\\\npm\\\\claude.cmd")
    for t in ("two\nlines", 'say "hi"', "a & b", "100%", "x | y", "<tag>", "a^b"):
        assert actions.shim_problem(t) and "claude install" in actions.shim_problem(t), t
    assert actions.shim_problem("plain sentence, with punctuation.") is None
    monkeypatch.setattr(actions, "CLAUDE", "C:\\\\Users\\\\x\\\\.local\\\\bin\\\\claude.exe")
    assert actions.shim_problem("two\nlines") is None


def test_chat_open_in_a_terminal_is_refused_with_the_documented_way_out():
    r = actions.reply({"kind": "interactive", "live": True, "state": "idle", "config": "/c", "session_id": "s", "job_id": None}, "hi")
    assert not r["ok"] and r["route"] == "refused" and "/bg" in r["error"]
