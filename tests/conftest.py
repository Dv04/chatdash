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
