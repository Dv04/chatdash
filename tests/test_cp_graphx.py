"""Graph extensions: file index normalisation and every gx view, on a temp db and fake transcripts."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import api, db, fileindex, graphx  # noqa: E402

H = os.path.expanduser("~")
NOW = 2_000_000_000.0


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    db.init()
    fileindex.init()
    return tmp_path


def chat(sid, **kw):
    c = {"key": f"work:{sid}", "session_id": sid, "account": "work", "config": H + "/.claude-work", "name": f"chat {sid}",
         "ws": "PROJ-10", "state": "idle", "live": True, "excluded": False, "kind": "bg", "path": f"/t/{sid}.jsonl",
         "cwd": H + "/code/platform/app-server", "activity": NOW - 60}
    c.update(kw)
    return c


def snap(*chats, seats=()):
    return {"chats": list(chats), "seats": list(seats), "jobs": [], "fleet": [], "errors": {}, "at": NOW}


def touch(norm, sid, kind="w", n=1, at=NOW - 600, repo=None):
    db.execute("INSERT INTO cp_touch(norm, session_id, kind, seat, repo, n, first_at, last_at) VALUES(?,?,?,?,?,?,?,?)",
               (norm, sid, kind, "work", repo or H + "/code/platform/app-server", n, at, at))


def test_normalize_drops_scratch_and_folds_worktrees():
    assert fileindex.normalize("/tmp/x.py") is None
    assert fileindex.normalize("/private/var/x") is None
    assert fileindex.normalize(H + "/.claude-work/projects/x/memory.md") is None
    assert fileindex.normalize(H + "/code/web/site/.claude/worktrees/batch1/src/a.ts") == H + "/code/web/site/src/a.ts"
    assert fileindex.normalize("relative/path") is None


def test_scan_indexes_writes_reads_prs_and_is_incremental(tmp, monkeypatch):
    t = tmp / "s1.jsonl"
    recs = [{"type": "assistant", "timestamp": "2026-10-03T00:00:00Z", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": H + "/code/x/.claude/worktrees/w/a.py"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": H + "/code/x/b.py"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/scratch.txt"}}]}},
        {"type": "user", "message": {"content": "see https://github.com/acme/app/pull/42"}}]
    t.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(fileindex, "transcripts", lambda: [(str(t), "s1", "work")])
    assert fileindex.scan()["touches"] == 2
    rows = {(r["norm"], r["kind"]) for r in db.rows("SELECT norm, kind FROM cp_touch")}
    assert rows == {(H + "/code/x/a.py", "w"), (H + "/code/x/b.py", "r")}
    assert db.rows("SELECT num FROM cp_prlink")[0]["num"] == 42
    assert fileindex.scan()["files"] == 0                                   # nothing new
    with open(t, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-03T01:00:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": H + "/code/x/a.py"}}]}}) + "\n")
    fileindex.scan()
    assert db.rows("SELECT n FROM cp_touch WHERE kind='w'")[0]["n"] == 2       # appended bytes only, counts add up


def test_files_one_level_at_a_time(tmp):
    root = H + "/code/platform/app-server"
    touch(root + "/src/a.ts", "s1", n=3)
    touch(root + "/src/lib/b.ts", "s1")
    touch(root + "/README.md", "s2")
    top = graphx.files(snap(chat("s1"), chat("s2")))
    assert [i["path"] for i in top["items"]] == [root] and top["items"][0]["files"] == 3
    lvl = graphx.files(snap(chat("s1"), chat("s2")), prefix=root)
    got = {i["label"]: (i["dir"], i["files"]) for i in lvl["items"]}
    assert got == {"src": (True, 2), "README.md": (False, 1)}
    only_s2 = graphx.files(snap(chat("s1"), chat("s2")), prefix=root, session="s2")
    assert [i["label"] for i in only_s2["items"]] == ["README.md"]


def test_hot_and_overlaps(tmp):
    f = H + "/code/platform/app-server/src/a.ts"
    touch(f, "s1", at=NOW - 600)
    touch(f, "s2", at=NOW - 300)
    touch(H + "/code/platform/app-server/src/only.ts", "s1")
    hot = graphx.hot(window=3600, now=NOW)
    assert len(hot) == 1 and hot[0]["n_sessions"] == 2
    assert graphx.hot(window=60, now=NOW) == []
    ov = graphx.overlaps(snap(chat("s1", state="working"), chat("s2")), window=3600, now=NOW)
    assert len(ov) == 1 and ov[0]["shared"] == 1 and ov[0]["risk"] == "high"
    ov2 = graphx.overlaps(snap(chat("s1", live=False, state="stopped"), chat("s2")), window=3600, now=NOW)
    assert ov2[0]["risk"] == "low"


def test_search_files_sessions_and_pr(tmp):
    touch(H + "/code/platform/app-server/src/webhooks.ts", "s1")
    db.execute("INSERT INTO cp_prlink(session_id, url, repo, num, at) VALUES('s2','https://github.com/o/r/pull/480','o/r',480,1)")
    s = snap(chat("s1"), chat("s2"), chat("s3"))
    r = graphx.search(s, "webhooks", lambda q: [{"key": "work:s3", "name": "chat s3", "final": "about [webhooks]"}])
    assert r["matched_sessions"] == ["s1", "s3"]
    assert graphx.search(s, "#480")["matched_sessions"] == ["s2"]


class Col:
    def cost_days(self, days):
        return [{"path": "/t/s1.jsonl", "by_day": {"d1": 100, "d2": 900}}, {"path": "/t/s2.jsonl", "by_day": {"d1": 0, "d2": 300}}]

    def pr_states(self, urls):
        return {u: "merged" for u in urls if u.endswith("/1")}


def test_spend_outcomes_and_prs(tmp):
    s = snap(chat("s1"), chat("s2", ws=None))
    sp = graphx.spend(s, Col(), days=1)
    assert sp["total"] == 1200 and [r["group"] for r in sp["rows"]] == ["PROJ-10", "no work item"]
    db.execute("INSERT INTO cp_prlink(session_id, url, repo, num, at) VALUES('s1','https://github.com/o/r/pull/1','o/r',1,1)")
    db.execute("INSERT INTO cp_prlink(session_id, url, repo, num, at) VALUES('s1','https://github.com/o/r/pull/2','o/r',2,1)")
    assert sorted(p["state"] for p in graphx.prs(s, Col())) == ["merged", "unknown"]
    out = graphx.outcomes(s, Col(), days=2)
    wi = next(g for g in out["groups"] if g["dim"] == "work_item" and g["key"] == "PROJ-10")
    assert wi["units"] == 1000 and wi["merged"] == 1 and wi["per_merged"] == 1000 and out["pr_states_pending"] == 1


def test_waiting_chain_and_routes(tmp):
    banner = {"resets_at": NOW + 600, "shown_at": NOW - 1200, "text": "limit"}
    s = snap(chat("s1", banner=banner), seats=[{"seat": "work", "config": H + "/.claude-work", "five_hour": {"pct": 100}, "seven_day": {"pct": 4}}])
    w = graphx.waiting(s, [{"session_id": "s2", "title": "Q", "kind": "decision", "seconds": 50}],
                       {"s3": {"text": "go", "at": NOW - 30}}, NOW)
    assert [c["waits_on"] for c in w["chains"]] == ["you", "seat:work", "turn"]
    assert w["longest"]["seconds"] == 1200
    src = type("S", (), {"get": lambda self: snap(chat("s1")), "col": Col()})()
    for path in ("gx/files", "gx/hot", "gx/hotspots", "gx/search?q=x", "gx/spend", "gx/prs", "gx/outcomes", "gx/waiting", "gx/overlaps", "gx/index"):
        p, _, qs = path.partition("?")
        q = {k: [v] for k, v in (x.split("=") for x in qs.split("&") if x)}
        code, out = api.handle("GET", p, q, {}, src)
        assert code == 200, (path, out)
    assert api.handle("GET", "gx/nope", {}, {}, src)[0] == 404


def test_waiting_drops_dead_chats_whose_reset_passed_long_ago(tmp):
    seat = {"seat": "work", "config": H + "/.claude-work", "five_hour": {"pct": 100}, "seven_day": {"pct": 4}}
    old = {"resets_at": NOW - 48 * 3600, "shown_at": NOW - 49 * 3600, "text": "limit"}
    recent = {"resets_at": NOW - 3600, "shown_at": NOW - 7200, "text": "limit"}
    s = snap(chat("dead_old", banner=old, live=False, state="stopped"), chat("dead_recent", banner=recent, live=False, state="stopped"),
             chat("live_old", banner=old, live=True), seats=[seat])
    w = graphx.waiting(s, [], None, NOW)
    items = [i["session_id"] for ch in w["chains"] for i in ch["items"]]
    assert sorted(items) == ["dead_recent", "live_old"]       # live idle chat still waits to be resumed; the dead 48 h one is history
