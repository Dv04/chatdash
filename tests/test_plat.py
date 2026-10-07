"""Windows and POSIX differences (dhi_orbit/_plat.py). The Windows branches run here against a fake pywinpty;
the real ConPTY path needs a Windows machine (see README, Windows)."""
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from dhi_orbit import _plat, actions, usage_meter
from dhi_orbit.cp import autolock, fileindex


# ---------------------------------------------------------------- paths
def test_is_abs_path_both_flavours():
    for p in ("/Users/x/a", "C:\\Users\\x\\a", "c:/Users/x/a", "\\\\srv\\share\\a"):
        assert _plat.is_abs_path(p), p
    for p in ("", None, "rel/x", "a:b", "~/x"):
        assert not _plat.is_abs_path(p), p


def test_to_posix_leaves_posix_backslashes_alone():
    assert _plat.to_posix("C:\\Users\\x") == "C:/Users/x"
    if not _plat.IS_WIN:
        assert _plat.to_posix("/home/a\\b") == "/home/a\\b"


def test_fileindex_normalizes_windows_paths(monkeypatch):
    monkeypatch.setattr(fileindex, "HOME", "C:/Users/dev")
    monkeypatch.setattr(fileindex, "_RE", __import__("re").I)
    assert fileindex.normalize("C:\\Users\\dev\\proj\\src\\a.py") == "C:/Users/dev/proj/src/a.py"
    assert fileindex.normalize("c:\\users\\dev\\.claude\\memory\\m.md") is None          # config folder, any drive-letter case
    assert fileindex.normalize("C:\\Users\\dev\\AppData\\Local\\Temp\\x.txt") is None
    assert fileindex.normalize("C:\\Users\\dev\\proj\\.claude\\worktrees\\w1\\a.py") == "C:/Users/dev/proj/a.py"
    assert fileindex.normalize("relative/a.py") is None


def test_repo_of_walks_windows_style_paths_up_to_home(tmp_path, monkeypatch):
    home = str(tmp_path).replace("\\", "/")
    monkeypatch.setattr(fileindex, "HOME", home)
    fileindex._repo_cache.clear()
    (tmp_path / "proj" / ".git").mkdir(parents=True)
    f = home + "/proj/src/deep/a.py"
    assert fileindex.repo_of(f) == home + "/proj"


# ---------------------------------------------------------------- liveness and lock
def test_pid_alive():
    assert _plat.pid_alive(os.getpid())
    assert not _plat.pid_alive(0) and not _plat.pid_alive(-5)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not _plat.pid_alive(p.pid)


def test_running_pids_subset():
    assert _plat.running_pids([os.getpid(), 2 ** 22 + 12345]) == {os.getpid()}
    assert _plat.running_pids([]) == set()


@pytest.mark.skipif(_plat.IS_WIN, reason="POSIX locks")
def test_try_lock_is_exclusive(tmp_path):
    a, b = open(tmp_path / "l", "a+"), open(tmp_path / "l", "a+")
    assert _plat.try_lock(a) is True
    assert _plat.try_lock(b) is False
    a.close()
    assert _plat.try_lock(b) is True


def test_autolock_second_owner_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(autolock, "_fh", None)
    p = str(tmp_path / "run" / "auto.lock")
    assert autolock.acquire(p) is True
    first = autolock._fh
    monkeypatch.setattr(autolock, "_fh", None)
    assert autolock.acquire(p) is False                 # the first handle still holds it
    first.close()


# ---------------------------------------------------------------- POSIX pty
@pytest.mark.skipif(_plat.IS_WIN, reason="POSIX pty")
def test_posix_pty_round_trip_and_size():
    code = ("import sys,os,termios,struct,fcntl;"
            "r,c=struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))[:2];"
            "print('SIZE',r,c,flush=True);"
            "l=sys.stdin.readline();print('GOT',l.strip().upper(),flush=True)")
    pty = _plat.Pty([sys.executable, "-c", code], dict(os.environ), os.getcwd(), 33, 111)
    try:
        buf = b""
        end = time.time() + 10
        while b"SIZE 33 111" not in buf and time.time() < end:
            buf += pty.read(0.2) or b""
        assert b"SIZE 33 111" in buf
        pty.write(b"hello\r")
        while b"GOT HELLO" not in buf and time.time() < end:
            buf += pty.read(0.2) or b""
        assert b"GOT HELLO" in buf
        while time.time() < end:                        # the child exits: EOF is b"", not an exception
            r = pty.read(0.2)
            if r == b"":
                break
        assert r == b""
        assert pty.read(0.01) == b""
    finally:
        _plat.stop_pty(pty)
    assert pty.proc.poll() is not None
    with pytest.raises(OSError):
        pty.write(b"x")


@pytest.mark.skipif(_plat.IS_WIN, reason="POSIX pty")
def test_posix_pty_missing_binary_raises_oserror():
    with pytest.raises(OSError):
        _plat.Pty(["/no/such/claude", "attach", "x"], dict(os.environ), os.getcwd())


# ---------------------------------------------------------------- Windows branch against a fake pywinpty
class FakeWinPty:
    instances = []

    def __init__(self, argv, cwd, env, dimensions):
        self.argv, self.cwd, self.env, self.dimensions = argv, cwd, env, dimensions
        self.pid, self.exitstatus, self._alive = 4242, None, True
        self.out, self.written, self.closed = [], [], False
        self.cv = threading.Condition()
        FakeWinPty.instances.append(self)

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80)):
        return cls(argv, cwd, env, dimensions)

    def feed(self, s):
        with self.cv:
            self.out.append(s)
            self.cv.notify_all()

    def finish(self):
        with self.cv:
            self._alive, self.exitstatus = False, 0
            self.cv.notify_all()

    def read(self, size=1024):
        with self.cv:
            while not self.out:
                if not self._alive:
                    raise EOFError()
                self.cv.wait(0.05)
            return self.out.pop(0)

    def write(self, s):
        if not self._alive:
            raise OSError("closed")
        self.written.append(s)

    def isalive(self):
        return self._alive

    def terminate(self, force=False):
        self.finish()

    def close(self, force=False):
        self.closed = True
        self.finish()


@pytest.fixture
def winpty(monkeypatch):
    mod = types.ModuleType("winpty")
    mod.PtyProcess = FakeWinPty
    monkeypatch.setitem(sys.modules, "winpty", mod)
    monkeypatch.setattr(_plat, "IS_WIN", True)
    FakeWinPty.instances.clear()
    return FakeWinPty


def test_windows_pty_flow(winpty):
    pty = _plat.Pty(["claude.exe", "attach", "abc"], {"A": "1"}, "C:\\w", 50, 220)
    fake = winpty.instances[0]
    assert fake.argv == ["claude.exe", "attach", "abc"] and fake.dimensions == (50, 220) and fake.env == {"A": "1"}
    assert pty.pid == 4242 and pty.proc.poll() is None
    assert pty.read(0.05) is None                                  # nothing yet
    fake.feed("\x1b[6n")                                           # ConPTY's cursor query is answered
    assert pty.read(1) == b"\x1b[6n"
    assert fake.written == ["\x1b[1;1R"]
    fake.feed("❯ ready")
    assert pty.read(1) == "❯ ready".encode()
    pty.write("héllo\r".encode())
    assert fake.written[-1] == "héllo\r"
    fake.finish()
    assert pty.read(1) == b""                                      # EOF
    assert pty.read(0.01) == b""
    with pytest.raises(OSError):
        pty.write(b"x")                                            # the child is gone
    _plat.stop_pty(pty)
    assert fake.closed and pty.proc.poll() == 0


def test_windows_pty_without_pywinpty_is_a_clear_oserror(monkeypatch):
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setitem(sys.modules, "winpty", None)               # import raises ImportError
    with pytest.raises(OSError) as e:
        _plat.Pty(["claude.exe"], {}, "C:\\")
    assert "pywinpty" in e.value.strerror


def test_actions_type_into_attach_over_windows_pty(winpty, tmp_path, monkeypatch):
    """The reply route end to end on the fake ConPTY: wait for the prompt box, paste, Enter, see it in the transcript."""
    monkeypatch.setattr(actions, "CLAUDE", "claude.exe")
    tr = tmp_path / "t.jsonl"
    tr.write_text("")

    def session():
        while not winpty.instances:
            time.sleep(0.01)
        fake = winpty.instances[0]
        fake.feed("welcome ❯ ")                                    # no bracketed-paste switch: ConPTY may drop it
        while not any(w == "\r" for w in fake.written):
            time.sleep(0.01)
        with open(tr, "a", encoding="utf-8") as fh:
            fh.write('{"type":"user","message":{"content":"ping please"}}\n')
    t = threading.Thread(target=session, daemon=True)
    t.start()
    r = actions.type_into_attach(str(tmp_path), "job12345", "ping please", str(tmp_path), str(tr), confirm_s=5)
    t.join(5)
    assert r["ok"] and r["confirmed"], r
    sent = winpty.instances[0].written
    assert sent[0] == actions.PASTE_START.decode() + "ping please" + actions.PASTE_END.decode() and sent[-1] == "\r"
    assert winpty.instances[0].closed


# ---------------------------------------------------------------- the status line command on Windows
def test_statusline_command_windows_forms(monkeypatch, tmp_path):
    """Claude Code runs the command through Git Bash when installed, else PowerShell (docs: status line > Windows configuration)."""
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setattr(sys, "executable", "C:\\Python312\\python.exe")
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path))
    monkeypatch.setattr(usage_meter.shutil, "which", lambda n: None)
    monkeypatch.setattr(usage_meter, "_has_git_bash", lambda: True)
    bash = usage_meter.command()
    assert bash.startswith('"C:/Python312/python.exe" -m dhi_orbit.statusline --home "') and "=" not in bash.split(" ")[0]
    monkeypatch.setattr(usage_meter, "_has_git_bash", lambda: False)
    ps = usage_meter.command()
    assert ps.startswith('& "C:/Python312/python.exe" -m dhi_orbit.statusline --home "'), "a quoted first token is only a string in PowerShell"
    monkeypatch.setattr(usage_meter.shutil, "which", lambda n: "C:/Users/x/.local/bin/dhi-orbit-statusline.exe")
    bare = usage_meter.command()
    assert bare.startswith('dhi-orbit-statusline --home "')
    for c in (bash, ps, bare):
        assert usage_meter.is_ours({"command": c}), c
        assert usage_meter.missing_interpreter(c) is None or os.path.exists(usage_meter.missing_interpreter(c)) is False


def test_statusline_home_argument(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DHI_ORBIT_HOME"}
    r = subprocess.run([sys.executable, "-m", "dhi_orbit.statusline", "--home", str(tmp_path)],
                       input=b'{"rate_limits":{"five_hour":{"used_percentage":12}},"session_id":"s"}',
                       capture_output=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)), timeout=20)
    assert r.returncode == 0 and b"5h 12%" in r.stdout
    assert (tmp_path / "meter.log").exists()


def test_open_terminal_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(_plat, "IS_WIN", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    r = actions.open_terminal({"config": "C:\\Users\\d\\.claude-work", "cwd": "C:\\proj", "job_id": "ab12cd34"})
    assert r["ok"]
    assert calls[0][0][0][:2] == ["cmd.exe", "/k"]
    assert 'cd /d "C:\\proj"' in r["cmd"] and 'set "CLAUDE_CONFIG_DIR=C:\\Users\\d\\.claude-work"' in r["cmd"]
    assert r["cmd"].endswith("claude attach ab12cd34")
