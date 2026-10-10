"""Docs reader: a chat's .md files (written or edited, subagents included) and the read gate (wrote it or mentioned it)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dhi_orbit.cp import docs  # noqa: E402

SID = "12345678-1234-4234-8234-123456789abc"


def _tool(name, path, ts="2026-10-10T01:00:00Z"):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"t-{name}-{path}", "name": name, "input": {"file_path": path, "content": "x"}}]}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = os.path.realpath(tmp_path / "home")
    os.makedirs(h)
    monkeypatch.setattr(docs, "HOME", h)
    monkeypatch.setattr(docs, "DENY", (os.path.join(h, ".secrets") + os.sep,))
    docs._cache.clear()
    return h


def _chat(home, rows, sub_rows=None):
    d = os.path.join(home, ".claude", "projects", "-x")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{SID}.jsonl")
    with open(p, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in [{"type": "user", "cwd": os.path.join(home, "repo"), "message": {"content": "hi"}}] + rows) + "\n")
    if sub_rows:
        sd = os.path.join(d, SID, "subagents")
        os.makedirs(sd)
        with open(os.path.join(sd, "agent-a.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in sub_rows) + "\n")
    return p


def _file(home, rel, text="# T\nbody"):
    p = os.path.join(home, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)
    return p


def test_lists_md_written_and_edited_newest_first_with_subagents(home):
    a, b, c = _file(home, "repo/A.md"), _file(home, "repo/docs/B.md"), _file(home, "repo/C.md")
    _file(home, "repo/x.py")
    p = _chat(home, [_tool("Write", a, "2026-10-10T01:00:00Z"), _tool("Edit", a, "2026-10-10T01:05:00Z"),
                     _tool("Write", os.path.join(home, "repo/x.py")), _tool("Read", c), _tool("Edit", b, "2026-10-10T01:02:00Z")],
              sub_rows=[_tool("Write", c, "2026-10-10T01:09:00Z")])
    got = docs.list_docs(p)["docs"]
    assert [d["name"] for d in got] == ["C.md", "A.md", "B.md"]          # x.py and the Read-only touch are left out
    assert got[0]["by_subagent"] and not got[1]["by_subagent"]
    assert (got[1]["writes"], got[1]["edits"]) == (1, 1)
    assert all(d["exists"] for d in got)


def test_missing_worktree_copy_falls_back_to_main_checkout(home):
    main = _file(home, "repo/PLAN.md")
    wt = os.path.join(home, "repo/.claude/worktrees/w1/PLAN.md")      # worktree removed: file only in the main checkout
    p = _chat(home, [_tool("Write", wt)])
    d = docs.list_docs(p)["docs"][0]
    assert d["exists"] and d["at_path"] == main
    st, body = docs.read_doc(p, wt)
    assert st == 200 and body["text"].startswith("# T")


def test_read_gate(home):
    w = _file(home, "repo/W.md", "written")
    m = _file(home, "repo/notes.md", "mentioned")
    _file(home, "repo/s.md", "never named")
    _file(home, "repo/other.md", "never named")
    _file(home, ".secrets/k.md", "secret")
    _file(home, "repo/code.py", "py")
    mention = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Full write-up:\nnotes.md and ~/.secrets/k.md and repo/code.py"}]}}
    p = _chat(home, [_tool("Write", w), mention])
    assert docs.read_doc(p, w)[0] == 200
    st, body = docs.read_doc(p, "notes.md")                          # relative: resolved against the chat's cwd
    assert st == 200 and body["text"] == "mentioned" and not body["written_here"]
    assert docs.read_doc(p, "notes.md:12")[0] == 200                 # path:line reference
    assert docs.read_doc(p, "s.md")[0] == 403                        # "notes.md" does not vouch for "s.md"
    assert docs.read_doc(p, os.path.join(home, "repo/other.md"))[0] == 403
    assert docs.read_doc(p, "~/.secrets/k.md")[0] == 403              # mentioned, but the folder is never served
    assert docs.read_doc(p, "code.py")[0] in (403, 404)
    assert docs.read_doc(p, "/etc/hosts")[0] in (403, 404)
    assert docs.read_doc(p, "missing.md")[0] == 404
    assert docs.read_doc(p, "")[0] == 400


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks on Windows needs Developer Mode or admin rights")
def test_symlink_out_of_home_is_refused(home, tmp_path):
    outside = tmp_path / "out.md"
    outside.write_text("outside")
    link = os.path.join(home, "repo", "L.md")
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(outside, link)
    p = _chat(home, [_tool("Write", link)])
    assert docs.read_doc(p, link)[0] == 403


def test_cache_refreshes_when_transcript_grows(home):
    a = _file(home, "repo/A.md")
    p = _chat(home, [_tool("Write", a)])
    assert len(docs.list_docs(p)["docs"]) == 1
    b = _file(home, "repo/B.md")
    with open(p, "a") as fh:
        fh.write(json.dumps(_tool("Write", b, "2026-10-10T02:00:00Z")) + "\n")
    assert [d["name"] for d in docs.list_docs(p)["docs"]] == ["B.md", "A.md"]


def test_secrets_never_listed_and_relative_name_finds_the_written_file(home):
    a = _file(home, "work/deep/folder/REPORT.md", "report")         # written outside the chat's cwd (home/repo)
    k = _file(home, ".secrets/INDEX.md", "secret")
    mention = {"type": "assistant", "message": {"content": [{"type": "text", "text": "See `REPORT.md` and `folder/REPORT.md`"}]}}
    p = _chat(home, [_tool("Write", a), _tool("Edit", k), mention])
    assert [d["name"] for d in docs.list_docs(p)["docs"]] == ["REPORT.md"]
    for rel in ("REPORT.md", "folder/REPORT.md"):
        st, body = docs.read_doc(p, rel)
        assert st == 200 and body["text"] == "report" and body["path"] == a


def test_incremental_reads_count_each_write_once(home):
    a = _file(home, "repo/A.md")
    p = _chat(home, [_tool("Write", a)])
    for _ in range(3):
        assert docs.list_docs(p)["docs"][0]["writes"] == 1          # repeated refreshes never re-count
    with open(p, "a") as fh:
        fh.write(json.dumps(_tool("Edit", a, "2026-10-10T03:00:00Z")) + "\n")
        fh.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "half a li')   # no newline yet
    d = docs.list_docs(p)["docs"][0]
    assert (d["writes"], d["edits"], d["last_at"]) == (1, 1, "2026-10-10T03:00:00Z")
    _file(home, "repo/later.md", "later")
    assert docs.read_doc(p, "later.md")[0] == 403                   # the half line is not read yet
    with open(p, "a") as fh:
        fh.write('ne later.md"}]}}\n')
    assert docs.read_doc(p, "later.md")[0] == 200                   # mentioned once the line completes


def _bash(cmd, ts="2026-10-10T01:00:00Z"):
    return {"type": "assistant", "timestamp": ts, "cwd": None, "message": {"content": [{"type": "tool_use", "id": "b" + cmd[:8], "name": "Bash", "input": {"command": cmd}}]}}


def _say(text, who="assistant", ts="2026-10-10T01:00:00Z"):
    return {"type": who, "timestamp": ts, "message": {"content": [{"type": "text", "text": text}] if who == "assistant" else text}}


def test_shell_made_and_mentioned_docs_are_listed(home):
    """Chats often make docs with shell heredocs, or name files written elsewhere: both must show."""
    pr = _file(home, "repo/tmp/pr_body.md", "# PR")
    tee = _file(home, "repo/notes/log.md", "log")
    idx = _file(home, "company/index.md", "# Company")
    rel = _file(home, "repo/docs/PLAN.md", "# Plan")
    _file(home, "repo/README.md", "readme")
    rows = [_bash(f"cat > {pr} <<'EOF'\n# PR\nEOF"), _bash("echo x | tee -a notes/log.md"),
            _bash("cat /tmp/nothing.md"),                                           # read, not made
            _say("The full write-up is in ~/company/index.md and docs/PLAN.md; see README.md too.", ts="2026-10-10T02:00:00Z"),
            _say("<task-notification> wrote ~/company/gone.md</task-notification>", who="user"),
            _say("missing: ~/nope/x.md")]
    for r in rows:
        r["cwd"] = os.path.join(home, "repo")
    p = _chat(home, rows)
    got = {d["path"]: d for d in docs.list_docs(p)["docs"]}
    assert set(got) == {pr, tee, idx, rel}                                           # bare README.md and missing files are left out
    assert got[pr]["kind"] == got[tee]["kind"] == "made" and got[pr]["shell"] == 1
    assert got[idx]["kind"] == got[rel]["kind"] == "mentioned"
    assert [d["kind"] for d in docs.list_docs(p)["docs"]] == ["made", "made", "mentioned", "mentioned"]
    assert docs.read_doc(p, rel)[0] == 200 and docs.read_doc(p, pr)[0] == 200


def test_link_inside_a_document_opens_one_hop(home):
    a = _file(home, "repo/docs/A.md", "# A\nSee [B](B.md) and [deep](../other/C.md#part).")
    b = _file(home, "repo/docs/B.md", "# B\n[D](D.md)")
    c = _file(home, "repo/other/C.md", "# C")
    d = _file(home, "repo/docs/D.md", "# D")
    _file(home, "repo/docs/E.md", "# E")
    p = _chat(home, [_tool("Write", a)])
    assert docs.read_doc(p, "B.md", frm=a)[0] == 200
    assert docs.read_doc(p, "../other/C.md", frm=a)[0] == 200
    assert docs.read_doc(p, "E.md", frm=a)[0] == 403                                 # not linked from A
    assert docs.read_doc(p, "D.md", frm=b)[0] == 403                                 # B is only linked, not the chat's: no chains
    assert docs.read_doc(p, "B.md", frm=os.path.join(home, "repo/docs/E.md"))[0] == 403


def test_windows_style_paths_are_recognised():
    """Windows transcripts carry C:\\ paths and backslashes; the parsers must find them (the resolving itself is os.path's)."""
    text = r"Wrote C:\Users\me\repo\docs\PLAN.md and ~\notes\log.md; also docs\specs\API.md"
    got = docs.TEXT_MD.findall(text)
    assert r"C:\Users\me\repo\docs\PLAN.md" in got and r"~\notes\log.md" in got and r"docs\specs\API.md" in got
    assert docs.SHELL_MD.findall(r"Get-Content x | Out-File > C:\Users\me\out\report.md") == [r"C:\Users\me\out\report.md"]
    assert docs._isabs(r"C:\Users\me\a.md") and docs._isabs("/home/me/a.md") and not docs._isabs(r"docs\a.md")
    assert docs.WT.sub("/", r"C:\r\.claude\worktrees\w1\docs\A.md") == r"C:\r/docs\A.md"
