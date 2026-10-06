"""The few places where Windows and POSIX differ: a pseudo-terminal for `claude attach` and `claude auth login`,
a single-owner file lock, "is this pid running", the list of running pids, and keeping the machine awake.

Everything else in DHI Orbit is portable stdlib. On POSIX this module uses openpty, select, fcntl and `ps`; on
Windows it uses ConPTY through the optional `pywinpty` package (installed automatically by pip on Windows),
msvcrt file locking and the Win32 process API through ctypes.
"""
from __future__ import annotations

import errno
import os
import queue
import re
import subprocess
import sys
import threading
import time

IS_WIN = sys.platform == "win32"
O_BIN = getattr(os, "O_BINARY", 0)          # os.open() on Windows translates newlines unless this is set

if IS_WIN:
    import msvcrt
else:
    import fcntl
    import select
    import struct
    import termios


# ------------------------------------------------------------------ paths
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def is_abs_path(p: str | None) -> bool:
    """An absolute path of either flavour: /x/y, C:\\x\\y, C:/x/y or \\\\server\\share. Transcripts and the meter log
    hold paths written by the machine that ran Claude Code, so this does not ask the local os.path."""
    return bool(p) and (p.startswith(("/", "\\\\")) or bool(_DRIVE.match(p)))


def to_posix(p: str) -> str:
    """A path with forward slashes only, so one set of regular expressions handles both flavours. A POSIX path is
    left alone (a backslash is a legal file name character there) unless it is plainly a Windows drive path."""
    return p.replace("\\", "/") if p and (IS_WIN or _DRIVE.match(p)) else p


# ------------------------------------------------------------------ process liveness
def pid_alive(pid: int) -> bool:
    """True when a process with this pid is running. Never signals it: on Windows os.kill(pid, 0) would terminate it."""
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5           # access denied: it exists, we may not look inside
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def all_running_pids() -> set[int]:
    """Every running pid (not stopped T, not zombie Z) from one `ps -A`. POSIX only: Windows has no cheap equivalent,
    ask pid_alive about the pids you care about instead."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,stat="], capture_output=True, text=True, timeout=5).stdout
    run = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[1].startswith(("T", "Z")):
            try:
                run.add(int(parts[0]))
            except ValueError:
                pass
    return run


def running_pids(pids) -> set[int]:
    """The subset of pids that are running."""
    pids = set(pids)
    if not pids:
        return set()
    if IS_WIN:
        return {p for p in pids if pid_alive(p)}
    return pids & all_running_pids()


# ------------------------------------------------------------------ single-owner lock
def try_lock(fh) -> bool:
    """Take an exclusive, non-blocking lock on an open file; True if this process now owns it. The lock is released
    when the file is closed or the process exits."""
    try:
        if IS_WIN:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ pseudo-terminal
def _no_pty(why: str) -> OSError:
    return OSError(errno.ENOSYS, why)


class _WinProc:
    """The slice of subprocess.Popen the callers use (pid, poll, terminate, kill, wait), over a winpty process."""

    def __init__(self, p):
        self._p, self.pid = p, getattr(p, "pid", 0)

    def poll(self):
        try:
            if self._p.isalive():
                return None
            return getattr(self._p, "exitstatus", None) or 0
        except Exception:
            return 0

    def terminate(self):
        try:
            self._p.terminate(force=True)
        except Exception:
            pass

    kill = terminate

    def wait(self, timeout=None):
        end = None if timeout is None else time.time() + timeout
        while self.poll() is None:
            if end is not None and time.time() > end:
                raise subprocess.TimeoutExpired("pty child", timeout)
            time.sleep(0.05)
        return self.poll()


class Pty:
    """A child process on a pseudo-terminal of a fixed size.

    read(timeout) -> bytes (b"" once the child closed the terminal) or None when nothing arrived in time.
    write(data) raises OSError when the terminal is gone. close() is idempotent. `proc` offers poll, terminate,
    kill, wait and pid on both platforms. A multi-threaded server must not pty.fork(), so POSIX uses openpty and
    Popen (a new session, so the child owns the terminal)."""

    def __init__(self, argv: list[str], env: dict, cwd: str, rows: int | None = None, cols: int | None = None):
        self._closed, self._eof = False, False
        if IS_WIN:
            self._start_win(argv, env, cwd, rows, cols)
        else:
            self._start_posix(argv, env, cwd, rows, cols)

    @property
    def pid(self) -> int:
        return self.proc.pid

    # --- POSIX
    def _start_posix(self, argv, env, cwd, rows, cols):
        self.fd, slave = os.openpty()
        try:
            if rows and cols:                                    # an unsized pty is 0x0; the caller may want exactly that
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                         start_new_session=True, close_fds=True)
        except OSError:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)

    # --- Windows (ConPTY through pywinpty)
    def _start_win(self, argv, env, cwd, rows, cols):
        try:
            from winpty import PtyProcess
        except ImportError:
            raise _no_pty("the pywinpty package is missing: pip install pywinpty") from None
        try:
            p = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows or 50, cols or 200))
        except Exception as e:                                  # winpty raises its own error types
            raise OSError(errno.ENOENT, f"could not start {os.path.basename(argv[0])} in a pseudo-terminal: {e}") from None
        self._p, self.proc = p, _WinProc(p)
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump_win, daemon=True).start()

    def _pump_win(self):
        """ConPTY reads block, so one thread moves the output into a queue that read() can wait on."""
        while True:
            try:
                s = self._p.read(65536)
            except EOFError:
                break
            except Exception:
                break
            if not s:
                if not self._p.isalive():
                    break
                time.sleep(0.02)
                continue
            b = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
            if b"\x1b[6n" in b:                                  # ConPTY asks where the cursor is and waits for an answer
                try:
                    self._p.write("\x1b[1;1R")
                except Exception:
                    pass
            self._q.put(b)
        self._q.put(None)

    # --- both
    def read(self, timeout: float):
        if self._eof or self._closed:
            return b""
        if IS_WIN:
            try:
                b = self._q.get(timeout=max(timeout, 0))
            except queue.Empty:
                return None
            if b is None:
                self._eof = True
                return b""
            return b
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            self._eof = True
            return b""
        if not r:
            return None
        try:
            b = os.read(self.fd, 65536)
        except OSError:                                          # Linux: EIO once the child side closed
            b = b""
        if not b:
            self._eof = True
        return b

    def write(self, data: bytes) -> None:
        if self._closed:
            raise OSError(errno.EBADF, "the terminal is closed")
        if IS_WIN:
            if not self._p.isalive():
                raise OSError(errno.EPIPE, "the terminal is closed")
            try:
                self._p.write(data.decode("utf-8", "replace"))
            except Exception as e:
                raise OSError(errno.EPIPE, str(e) or "the terminal is closed") from None
            return
        os.write(self.fd, data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if IS_WIN:
            try:
                self._p.close(force=True)
            except Exception:
                pass
        else:
            try:
                os.close(self.fd)
            except OSError:
                pass


def stop_pty(pty: Pty) -> None:
    """Close the terminal and end its child: close, then terminate, then kill, each given 2 s."""
    pty.close()
    for stop in (None, pty.proc.terminate, pty.proc.kill):
        if stop:
            try:
                stop()
            except OSError:
                pass
        try:
            pty.proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


# ------------------------------------------------------------------ keep awake
def keep_awake(parent_pid: int) -> subprocess.Popen | None:
    """Start a helper that stops the machine from idle-sleeping until `parent_pid` exits; None when this OS has none.
    macOS: caffeinate. Windows: a tiny child process holding SetThreadExecutionState. Elsewhere: nothing."""
    try:
        if sys.platform == "darwin":
            # -w: caffeinate exits when the server exits, so a restart or crash can never orphan it
            return subprocess.Popen(["caffeinate", "-i", "-m", "-s", "-w", str(parent_pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if IS_WIN:
            return subprocess.Popen([sys.executable, "-m", "dhi_orbit._plat", "--awake", str(parent_pid)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=0x08000000)      # CREATE_NO_WINDOW
    except OSError:
        return None
    return None


def _awake_main(parent_pid: int) -> None:
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    while pid_alive(parent_pid):
        time.sleep(5)


if __name__ == "__main__" and IS_WIN and len(sys.argv) == 3 and sys.argv[1] == "--awake":
    _awake_main(int(sys.argv[2]))
