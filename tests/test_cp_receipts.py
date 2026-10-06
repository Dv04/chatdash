"""Receipts (facts from the turn and git) and the Stop-hook evidence gate."""
import io
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import db, receipts  # noqa: E402
from dhi_orbit.cp.hooks import stop_hook  # noqa: E402

HOME = os.path.expanduser("~")


def rec(kind, content, ts="2026-10-02T10:00:00Z", **kw):
    r = {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}
    r.update(kw)
    return r


def transcript(tmp_path, final="Done. pytest: 10 passed in 0.50s", test_out="....\n10 passed in 0.50s", edit=True, check=True):
    recs = [rec("user", "earlier prompt", ts="2026-10-02T09:00:00Z"),
            rec("assistant", [{"type": "text", "text": "old answer"}]),
            rec("user", "fix the parser and test it")]
    if edit:
        recs += [rec("assistant", [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/r/parser.py"}}]),
                 rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])]
    recs += [rec("assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "echo hi > out/notes.txt"}}]),
             rec("user", [{"type": "tool_result", "tool_use_id": "t2", "content": ""}])]
    if check:
        recs += [rec("assistant", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "python3 -m pytest -q"}}]),
                 rec("user", [{"type": "tool_result", "tool_use_id": "t3", "content": test_out}])]
    recs.append(rec("assistant", [{"type": "text", "text": final}], message_id="m9"))
    recs[-1]["message"].update(id="m9", model="claude-sonnet-5-5",
                               usage={"input_tokens": 10, "output_tokens": 100, "cache_creation_input_tokens": 1000})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(p)


def conf(**kw):
    """The config file content: work items look like PROJ-10, the gate is on by default for PROJ-10 only,
    the account "main" (~/.claude) is read-only."""
    return {"work_item_pattern": r"PROJ-\d+", "evidence_gate_work_items": ["PROJ-10"],
            "read_only_accounts": ["main"], **kw}


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "cp.db"))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(conf(stop_gate="on")))
    monkeypatch.setattr(db, "CONFIG", str(cfg))
    monkeypatch.setenv("DHI_ORBIT_CONFIG", str(cfg))
    db.init()
    return cfg


def test_facts_from_the_current_turn_only(tmp_path):
    rc = receipts.build(transcript(tmp_path), None)
    paths = {f["path"]: f["source"] for f in rc["files"]}
    assert paths == {"/r/parser.py": "tool", "out/notes.txt": "bash redirect"}
    assert rc["tests"] == {"cmd": "python3 -m pytest -q", "last_line": "10 passed in 0.50s", "exit": None}
    assert rc["turn"] == 2 and rc["diff"]["source"].startswith("tool calls")
    assert rc["cost_units"] == round(0.72 * (1000 + 10 + 500))


def test_verdict_rules(tmp_path):
    rc = receipts.build(transcript(tmp_path), None)
    assert receipts.verdict(rc, "Done. 10 passed in 0.50s")[0] is True            # quotes the output line
    assert receipts.verdict(rc, "Done, ran pytest")[0] is True                     # names a distinctive check
    assert receipts.verdict(rc, "Done, all good")[0] is False                       # claim without evidence
    assert receipts.verdict(rc, "Done. Checked with python3 -m pytest -q")[0] is True  # names the exact command
    assert receipts.verdict(rc, "Done; the parser is not verified yet")[0] is True  # honest label
    rc2 = receipts.build(transcript(tmp_path, check=False), None)
    assert receipts.verdict(rc2, "Done") == (False, "commands ran but the final message neither names one nor quotes its output")
    rc3 = receipts.build(transcript(tmp_path, edit=False, check=False), None)
    rc3["diff"]["files"] = 0
    assert receipts.verdict(rc3, "Here is the answer")[0] is True                  # nothing changed


def test_make_sure_is_not_a_check(tmp_path):
    rc = receipts.build(transcript(tmp_path), None)
    rc["checks"] = [dict(rc["checks"][-1], cmd="make build", last_line="ok")]
    assert receipts.verdict(rc, "Done, make sure to restart")[0] is False
    assert receipts.verdict(rc, "Done, make build passed")[0] is True


def test_git_facts_in_a_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", ".")
    run("commit", "-qm", "first")
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    g = receipts.git_facts(str(repo), "2000-01-01T00:00:00Z")
    assert g["add"] >= 3 and "a.txt" in g["files"] and g["commits"][0].endswith("first")
    assert receipts.git_facts(str(tmp_path), None) is None


def hook(tmp_path, final, name="PROJ-10 Repos", active=False, env=None):
    jobdir = tmp_path / "job"
    jobdir.mkdir(exist_ok=True)
    (jobdir / "state.json").write_text(json.dumps({"name": name}))
    env = env or {"CLAUDE_CONFIG_DIR": HOME + "/.claude-work", "CLAUDE_JOB_DIR": str(jobdir)}
    p = {"session_id": "s1", "transcript_path": transcript(tmp_path), "cwd": str(tmp_path),
         "last_assistant_message": final, "stop_hook_active": active}
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        stop_hook.main(stdin=io.StringIO(json.dumps(p)), env=env)
    finally:
        sys.stdout = old
    return out.getvalue()


def test_gate_blocks_at_most_three_times(tmp, tmp_path):
    outs = [hook(tmp_path, "Done, all good", active=i > 0) for i in range(5)]
    assert [bool(o) for o in outs] == [True, True, True, False, False]
    first = json.loads(outs[0])
    assert first["decision"] == "block" and "block 1 of 3" in first["reason"]
    assert len(receipts.latest("s1")) == 1                      # one receipt per turn (replaced)
    assert [r["decision"] for r in db.rows("SELECT decision FROM auto_log ORDER BY id")][:4] == ["blocked"] * 3 + ["allowed"]


def test_gate_passes_with_evidence_and_off_for_other_work_items(tmp, tmp_path):
    assert hook(tmp_path, "Done: 10 passed in 0.50s") == ""
    assert receipts.latest("s1")[0]["verified"] is True
    assert hook(tmp_path, "Done, all good", name="PROJ-02 Outreach campaign") == ""   # not in evidence_gate_work_items: off


def test_gate_dry_run_logs_once(tmp, tmp_path):
    tmp.write_text(json.dumps(conf(stop_gate="dry-run")))
    assert hook(tmp_path, "Done, all good") == "" and hook(tmp_path, "Done, all good") == ""
    assert [r["decision"] for r in db.rows("SELECT decision FROM auto_log")] == ["would block"]


def test_stop_hook_skips_read_only_account_and_terminal(tmp, tmp_path):
    assert hook(tmp_path, "x", env={"CLAUDE_JOB_DIR": "/j"}) == ""                              # unset dir = ~/.claude = main
    assert hook(tmp_path, "x", env={"CLAUDE_CONFIG_DIR": HOME + "/.claude-work"}) == ""
    assert receipts.latest("s1") == []


def test_read_back_with_cat_counts_as_a_check(tmp_path):
    """Live false block found 2026-10-02: the chat ran `cat note.txt`, quoted the command and its output
    `hello`, and the gate still blocked because only test runners counted. Rule: any command named
    or quoted with its output."""
    rc = receipts.build(transcript(tmp_path, check=False), None)
    rc["checks"].append({"cmd": "cat /w/note.txt", "last_line": "hello", "exit": None, "ran": True, "test": False})
    assert receipts.verdict(rc, "Verified with: `cat /w/note.txt` Output: `hello`")[0] is True
    assert receipts.verdict(rc, "Verified, looks right")[0] is False


def test_plain_no_checks_statement_counts_as_honest():
    rc = {"diff": {"files": 1}, "checks": []}
    assert receipts.verdict(rc, "No additional checks run per your test instruction.")[0] is True
    assert receipts.verdict(rc, "Saved it without running any check.")[0] is True
    assert receipts.verdict(rc, "Saved it; everything works.")[0] is False
