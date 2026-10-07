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


# ------------------------------------------------------------------ edge cases (second pass)
def test_older_chats_are_listed_when_the_account_has_few_recent_ones(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects" / "p").mkdir(parents=True)
    now = time.time()
    for i in range(14):
        f = cfg / "projects" / "p" / f"old{i:02d}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (now - 86400 * (3 + i), now - 86400 * (3 + i)))
    recent = cfg / "projects" / "p" / "new.jsonl"
    recent.write_text("{}\n")
    got = collector.transcripts(str(cfg), now - 86400, collector.MIN_RECENT)
    assert "new" in got and len(got) == collector.MIN_RECENT, "the newest older chats top the list up to the floor"
    assert {"old00", "old01", "old08"} <= set(got) and "old13" not in got
    assert len(collector.transcripts(str(cfg), now - 86400)) == 1, "without a floor only the window counts"


def test_windows_replace_waits_out_a_sharing_violation(monkeypatch, tmp_path):
    calls = []
    real = os.replace

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        real(a, b)
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setattr(os, "replace", flaky)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("x")
    _plat.replace(str(a), str(b))
    assert b.read_text() == "x" and len(calls) == 3
    monkeypatch.setattr(_plat, "IS_WIN", False)
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("real denial")))
    with pytest.raises(PermissionError):
        _plat.replace(str(a), str(b))


def test_pid_reuse_is_detected_only_when_the_process_is_younger_than_the_record(monkeypatch):
    monkeypatch.setattr(_plat, "process_start", lambda pid: 2_000_000_000.0)
    assert _plat.reused_pid(1, 1_000_000_000_000) is True                 # started 1e9 s after the session record: another program
    assert _plat.reused_pid(1, 1_999_999_950_000) is False                # within the slack: the same Claude Code
    assert _plat.reused_pid(1, None) is False
    monkeypatch.setattr(_plat, "process_start", lambda pid: None)
    assert _plat.reused_pid(1, 1) is False


def test_bom_in_settings_json_is_read(tmp_path):
    from dhi_orbit import usage_meter
    p = tmp_path / "settings.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"statusLine": {"type": "command", "command": "x"}}).encode())
    assert usage_meter._load(str(p))["statusLine"]["command"] == "x"


def test_resume_bg_survives_a_missing_folder_and_explains_known_failures(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode, stdout, stderr = 1, "", "Error: Workspace not trusted"

    def fake_run(argv, **kw):
        seen.update(kw)
        return P()
    monkeypatch.setattr(actions, "CLAUDE", "claude")
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    r = actions.resume_bg(str(tmp_path), "sid", "hi", str(tmp_path / "gone"))
    assert os.path.isdir(seen["cwd"]) and seen["stdin"] is not None and seen["encoding"] == "utf-8"
    assert not r["ok"] and "does not trust this folder" in r["error"]
    P.stderr = "No conversation found with session ID: x"
    assert "no longer has this conversation" in actions.resume_bg(str(tmp_path), "sid", "hi", None)["error"]

    def timeout(argv, **kw):
        raise actions.subprocess.TimeoutExpired(argv, 60)
    monkeypatch.setattr(actions.subprocess, "run", timeout)
    assert "did not answer" in actions.resume_bg(str(tmp_path), "sid", "hi", None)["error"]

    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file")
    monkeypatch.setattr(actions.subprocess, "run", missing)
    assert "could not run claude" in actions.resume_bg(str(tmp_path), "sid", "hi", None)["error"]


def test_attach_retypes_plainly_when_conpty_echoes_the_paste_markers(monkeypatch, tmp_path):
    import threading
    from tests.test_plat import FakeWinPty
    mod = __import__("types").ModuleType("winpty")
    mod.PtyProcess = FakeWinPty
    monkeypatch.setitem(sys.modules, "winpty", mod)
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setattr(actions, "CLAUDE", "claude.exe")
    FakeWinPty.instances.clear()
    tr = tmp_path / "t.jsonl"
    tr.write_text("")

    def session():
        while not FakeWinPty.instances:
            time.sleep(0.01)
        f = FakeWinPty.instances[0]
        f.feed("> ")
        while not any("\x1b[200~" in w for w in f.written):
            time.sleep(0.01)
        f.feed("[200~line one[201~")                      # the markers arrived as typed text
        while not any(w == "\r" for w in f.written):
            time.sleep(0.01)
        with open(tr, "a", encoding="utf-8") as fh:
            fh.write('{"type":"user","message":{"content":"line one line two"}}\n')
    threading.Thread(target=session, daemon=True).start()
    monkeypatch.setattr(actions, "_input_ready", lambda b: True)
    r = actions.type_into_attach(str(tmp_path), "job12345", "line one\nline two", str(tmp_path), str(tr), confirm_s=5)
    w = FakeWinPty.instances[0].written
    assert r["ok"] and r["confirmed"], r
    assert "\x15" in w and "line one line two" in w and w[-1] == "\r", w
