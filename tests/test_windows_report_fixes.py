"""Fixes from the Windows root-cause report (2026-10-07): where a new chat starts, dialogs on the attach screen, Open terminal on a live
terminal chat, closed chats in the list."""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit import _plat, actions  # noqa: E402
from dhi_orbit.cp import api, db, work  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(work, "HOME", str(h))
    monkeypatch.setenv("DHI_ORBIT_CONFIG", str(tmp_path / "orbit.json"))
    return h


def trust(home, *paths, accepted=True):
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {p.replace("\\", "/"): {"hasTrustDialogAccepted": accepted} for p in paths}}))


def test_a_trusted_folder_off_the_home_drive_is_accepted_and_an_untrusted_one_is_not(home, tmp_path):
    other = tmp_path / "drive-e" / "SWE & Edge AI Intern"
    (other / "sub").mkdir(parents=True)
    plain = tmp_path / "drive-e" / "plain"
    plain.mkdir()
    assert work.safe_cwd(str(other)) is None and work.safe_cwd(str(plain)) is None            # nothing trusted yet
    trust(home, str(other))
    assert work.safe_cwd(str(other)) == os.path.realpath(other)
    assert work.safe_cwd(str(other / "sub")) == os.path.realpath(other / "sub")               # trust covers the folders below
    assert work.safe_cwd(str(plain)) is None
    trust(home, str(other), accepted=False)
    assert work.safe_cwd(str(other)) is None                                                  # a refused question is not trust


def test_hidden_folders_and_the_home_folder_are_never_trusted_starts(home, tmp_path):
    (home / "proj" / ".git").mkdir(parents=True)
    hid = tmp_path / "x" / ".secret"
    hid.mkdir(parents=True)
    trust(home, str(home), str(tmp_path / "x"))
    assert work.safe_cwd(str(home / "proj")) == os.path.realpath(home / "proj")
    assert work.safe_cwd(str(home / "proj" / ".git")) is None and work.safe_cwd(str(hid)) is None
    assert work.is_trusted(str(home)) is False                                                # the home folder is trusted one session at a time


def test_a_new_chat_starts_in_the_configured_folder_else_the_newest_trusted_chat_folder_else_home(home, tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    trust(home, str(old), str(new))
    snap = {"chats": [{"cwd": str(old), "activity": 10.0}, {"cwd": str(new), "activity": 99.0}, {"cwd": str(home), "activity": 500.0},
                      {"cwd": str(tmp_path / "gone"), "activity": 900.0}]}
    assert work.start_folder(snap, "main") == os.path.realpath(new)                           # not the home folder, not a deleted one
    assert work.start_folder(snap, "main", str(old)) == os.path.realpath(old)                 # a picked folder wins
    assert work.start_folder({"chats": []}, "main") == os.path.expanduser("~")                # nothing known: home (claude then explains)
    (tmp_path / "orbit.json").write_text(json.dumps({"default_cwd": str(old)}))
    assert work.start_folder(snap, "main") == str(old)                                        # the owner's setting is never second-guessed
    f = work.folders(snap, "main")
    assert f["configured"] is True and [x["path"] for x in f["folders"]][:2] == [os.path.realpath(new), os.path.realpath(old)]
    assert all(x["trusted"] for x in f["folders"])


class Runner:
    def __init__(self, out, code):
        self.out, self.code, self.calls = out, code, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=self.code, stdout=self.out, stderr="")


def test_spawn_in_an_untrusted_folder_says_what_to_do(home, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(db, "CONFIG", str(tmp_path / "c.json"))
    (tmp_path / "c.json").write_text("{}")
    db.init()
    work.init()
    r = Runner("Workspace not trusted. The home directory is trusted one session at a time", 1)
    code, res = work.spawn({"seat": "work", "brief": "x"}, {"chats": [], "jobs": [], "seats": [{"seat": "work", "state": "ok"}]}, runner=r)
    assert code == 409 and "not trusted" in res["error"] and "default_cwd" in res["error"]
    assert r.calls[0][1]["cwd"] == os.path.expanduser("~")


def test_open_terminal_will_not_start_a_second_writer_on_a_live_terminal_chat():
    live = {"kind": "interactive", "live": True, "job_id": None, "session_id": "s1", "config": os.path.expanduser("~/.claude"), "cwd": "/x"}
    r = actions.open_terminal(live)
    assert r["ok"] is False and "/bg" in r["error"]


def test_open_terminal_still_attaches_a_background_chat(monkeypatch):
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stderr=""))
    bg = {"kind": "bg", "live": True, "job_id": "ab12cd34", "session_id": "s1", "config": os.path.expanduser("~/.claude"), "cwd": "/x"}
    r = actions.open_terminal(bg)
    assert "claude attach ab12cd34" in r["cmd"] and "terminal chat" not in (r.get("error") or "")


TRUST_SCREEN = "Security guide\r\n Do you trust the files in this folder?\r\n ❯ No, exit\r\n   Yes, I trust this folder\r\n"
PERMISSION_SCREEN = "Do you want to proceed?\r\n ❯ 1. Yes\r\n   2. No\r\nEsc to cancel\r\n"


def test_on_windows_a_dialog_cursor_is_not_the_input_box(monkeypatch):
    monkeypatch.setattr(_plat, "IS_WIN", True)
    assert actions._input_ready("welcome ❯ ".encode()) is True
    assert actions._input_ready(TRUST_SCREEN.encode()) is False
    assert actions._input_ready(PERMISSION_SCREEN.encode()) is False
    assert actions._ready_or_dialog(TRUST_SCREEN.encode()) is True                           # stop waiting: the dialog is the answer


class DialogPty:
    def __init__(self, screen):
        self.screen, self.written, self.closed = screen, [], False


@pytest.mark.parametrize("win,screen,word", [(False, TRUST_SCREEN, "not trusted"), (True, TRUST_SCREEN, "not trusted"),
                                              (True, PERMISSION_SCREEN, "dialog")])
def test_nothing_is_typed_into_a_dialog(monkeypatch, tmp_path, win, screen, word):
    monkeypatch.setattr(_plat, "IS_WIN", win)
    pty = DialogPty(screen)
    pty.write = lambda b: pty.written.append(b)
    monkeypatch.setattr(actions, "_spawn_attach", lambda *a, **k: pty)
    monkeypatch.setattr(actions, "_drain_until", lambda p, ready, t: screen.encode())
    monkeypatch.setattr(actions, "_close_attach", lambda p: setattr(pty, "closed", True))
    r = actions.type_into_attach(str(tmp_path), "job12345", "hello", str(tmp_path), None)
    assert r["ok"] is False and word in r["error"] and pty.written == [] and pty.closed


def test_graph_lists_closed_chats_apart_from_the_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "g.db"))
    db.init()
    seats = [{"seat": "work", "label": "Work", "state": "ok", "five_hour": {"pct": 10}, "excluded": False}]
    from test_cp import chat, snap
    closed = [chat(key=f"work:c{i}", session_id=f"c{i}", kind="closed", state="stopped", activity=float(i), name=f"old {i}") for i in range(3)]
    g = api.graph(snap(chats=[chat()] + closed, seats=seats), 1000.0)
    assert [c["session_id"] for c in g["closed"]] == ["c2", "c1", "c0"] and g["closed_total"] == 3        # newest first
    assert not any(n["id"].startswith("session:work:c") for n in g["nodes"])
