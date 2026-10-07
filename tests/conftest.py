"""Isolation for every test: nothing reads or writes the real data directory or settings file."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "orbit-home"))
    for k in ("CHATDASH_HOME", "CHATDASH_CONFIG", "DHI_ORBIT_CONFIG", "CP_CONFIG", "CP_DB", "CP_METER_LOG"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def fake_claude_bin(tmp_path):
    """The stand-in `claude` (fixtures/fake_claude.py). Windows cannot start a .py file as a program, so it gets a .cmd launcher
    that runs this interpreter on it; CLAUDE_BIN is used as given, never swapped for a real claude.exe."""
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_claude.py")
    if sys.platform != "win32":
        return fake
    launcher = tmp_path / "fake_claude.cmd"
    launcher.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    return str(launcher)
