"""PR state cache: gh answers are classified (missing PRs are not nodes), final states are kept, restarts keep them."""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit import collector  # noqa: E402
from dhi_orbit.cp import db, fileindex, graphx  # noqa: E402


class Done:
    def __init__(self, out="", err=""):
        self.stdout, self.stderr = out, err


@pytest.fixture
def col():
    return collector.Collector()          # conftest points DHI_ORBIT_HOME at a temp dir


def fake_gh(answers, calls):
    def run(cmd, **kw):
        u = cmd[3]
        calls.append(u)
        a = answers[u]
        if isinstance(a, Exception):
            raise a
        return a
    return run


def test_gh_answers_are_classified(col, monkeypatch):
    calls = []
    monkeypatch.setattr(collector.subprocess, "run", fake_gh({
        "u/merged": Done("MERGED\n"), "u/open": Done("OPEN\n"),
        "u/gone": Done("", "GraphQL: Could not resolve to a Repository with the name 'o/r'. (repository)"),
        "u/nopr": Done("", "GraphQL: Could not resolve to a PullRequest with the number of 27. (repository.pullRequest)"),
        "u/net": Done("", "error connecting to api.github.com"),
        "u/slow": subprocess.TimeoutExpired("gh", 20)}, calls))
    col._fetch_prs(["u/merged", "u/open", "u/gone", "u/nopr", "u/net", "u/slow"])
    got = {u: v[1] for u, v in col._pr_cache.items()}
    assert got == {"u/merged": "merged", "u/open": "open", "u/gone": "missing", "u/nopr": "missing", "u/net": None, "u/slow": None}
    assert not col._pr_busy


def test_final_states_are_not_asked_again_but_a_failure_is_retried_soon(col, monkeypatch):
    now = time.time()
    col._pr_cache = {"m": (now - 3600, "merged"), "o": (now - 3600, "open"), "e": (now - 300, None), "e2": (now - 30, None)}
    started = []
    monkeypatch.setattr(collector.threading.Thread, "start", lambda self: started.append(self._args[0]))
    col.pr_states(["m", "o", "e", "e2", "new"])
    assert sorted(started[0]) == ["e", "new", "o"]       # merged (1 h old) is final; open (1 h) and a failure (5 min) are stale; 30 s is too soon


def test_cache_survives_a_restart(col, monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run", fake_gh({"u/a": Done("MERGED\n")}, []))
    col._fetch_prs(["u/a"])
    again = collector.Collector()
    assert again._pr_cache["u/a"][1] == "merged"
    open(collector.Collector._pr_file(), "w").write("not json")
    assert collector.Collector()._pr_cache == {}


def test_missing_prs_are_not_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()
    fileindex.init()
    db.execute("INSERT INTO cp_prlink(session_id, url, repo, num, at) VALUES('s1','https://github.com/o/r/pull/1','o/r',1,1)")
    db.execute("INSERT INTO cp_prlink(session_id, url, repo, num, at) VALUES('s1','https://github.com/dhi/real/pull/2','dhi/real',2,1)")

    class Col:
        def pr_states(self, urls):
            return {"https://github.com/o/r/pull/1": "missing", "https://github.com/dhi/real/pull/2": "merged"}
    snap = {"chats": [{"session_id": "s1"}]}
    assert [p["num"] for p in graphx.prs(snap, Col())] == [2]
