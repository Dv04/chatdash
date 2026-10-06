"""A5 work items: state of play, proposals you approve, spawning sessions, handoff.

A work item is a group of chats whose names match config work_item_pattern (none by default: no work items).
Nothing here writes a state doc or sends a prompt on its own:
  * state doc regeneration -> a proposal (diff) -> you press Apply -> file written (old copy kept)
  * "looks done" (verified receipt AND no open decisions AND the final says done or has a
    result: line) or your "Mark done" -> a handoff proposal -> you confirm -> mode "handoff":
    dry-run logs only; on sends the handoff prompt, verifies the note on disk, then stops the session
  * spawn is your own action; a near/blocked seat queues the start (never moves to another seat)
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
import threading
import time

from .. import config
from . import db, limits, sources, workitems

HOME = os.path.expanduser("~")
STATE_DIR: str | None = None      # state docs; None means <data dir>/work/state
HISTORY: str | None = None        # previous copies; None means <state dir>/.history


def state_dir() -> str:
    return STATE_DIR or os.path.join(config.work_dir(), "state")


def history_dir() -> str:
    return HISTORY or os.path.join(state_dir(), ".history")


AUTO_START, AUTO_END = "<!-- cp:auto start -->", "<!-- cp:auto end -->"
DONE_RE = re.compile(r"(^|\n)\s*result:|\b(done|finished|completed|all set)\b[.!]?\s*$", re.I)
REGEN_EVERY_S = 1800
INTERVIEW = ("Before any work: interview the user with the AskUserQuestion tool until the task is fully specified (one "
             "question per decision, recommended option first with \"(Recommended)\", at most 4 per call). Then write "
             "the spec into {path} under a heading '## spec {date}' and continue with the work.\n\n")
HANDOFF = ("The user confirmed this chat's work is done. Write a handoff note for the next chat: append to {path} a section "
           "headed exactly '## handoff {date} ({sid8})' with done / open / decisions needed / next 3 steps, facts only "
           "(file paths, PR links, verbatim test lines). Then reply with exactly: handoff written. Do not start new work.")

SCHEMA = """CREATE TABLE IF NOT EXISTS cp_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, work_item TEXT, session_id TEXT, target TEXT,
    old TEXT, new TEXT, why TEXT, sig TEXT, state TEXT DEFAULT 'proposed', created_at REAL, decided_at REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS cp_spawn_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, work_item TEXT, seat TEXT, brief TEXT, interview INTEGER, parent TEXT,
    state TEXT DEFAULT 'queued', created_at REAL, started_at REAL, job_id TEXT, detail TEXT);"""


def init() -> None:
    c = db.connect()
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(cp_spawn_queue)")}
    if "cwd" not in cols:
        c.execute("ALTER TABLE cp_spawn_queue ADD COLUMN cwd TEXT")
    c.commit()
    c.close()


def home_seat(wi: str, snap: dict | None = None) -> str | None:
    """Suggested seat for a new session of `wi`: the spawnable seat with the most spare weekly limit
    (100 minus its 7-day percentage); a seat with no reading ranks last. None without a snapshot."""
    if not wi or not snap:
        return None
    ranked = []
    for st in snap.get("seats") or []:
        if config.is_read_only(st["seat"]) or st.get("excluded") or st["state"] == "blocked":
            continue
        pct = (st.get("seven_day") or {}).get("pct")
        ranked.append((st["seat"], None if pct is None else 100.0 - pct))
    ranked.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
    return ranked[0][0] if ranked else None


def state_path(wi: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", wi)
    return os.path.join(state_dir(), f"{safe}.md")


def read_state(wi: str) -> str:
    try:
        return open(state_path(wi), encoding="utf-8").read()
    except OSError:
        return ""


def today_local(now: float | None = None) -> str:
    """'2026-10-02 4:40pm CDT' in the configured time zone (default: this computer's), never UTC."""
    from datetime import datetime
    now = now or time.time()
    t = datetime.fromtimestamp(now, config.tz())
    return t.strftime("%Y-%m-%d") + " " + limits.fmt_clock(now) + " " + (t.tzname() or "")


# ------------------------------------------------------------------ views
def summary(snap: dict) -> list[dict]:
    rows = db.rows("SELECT * FROM work_items ORDER BY id")
    out = []
    for w in rows:
        cs = [c for c in snap["chats"] if c.get("ws") == w["id"] and not is_test(c)]
        dec = db.rows("SELECT COUNT(*) AS n FROM decisions WHERE state='open' AND work_item=?", (w["id"],))[0]["n"]
        out.append({**w, "home_seat": home_seat(w["id"], snap), "sessions": [c["session_id"] for c in cs],
                    "open_decisions": dec + sum(1 for c in cs if c["state"] == "needs_you"),
                    "last_activity": max((c.get("activity") or 0 for c in cs), default=None) or None})
    return out


def detail(wi: str, snap: dict) -> dict | None:
    rows = db.rows("SELECT * FROM work_items WHERE id=?", (wi,))
    if not rows:
        return None
    from . import receipts
    w = rows[0]
    cs = [c for c in snap["chats"] if c.get("ws") == wi and not is_test(c)]
    sids = {c["session_id"] for c in cs}
    timeline = []
    for sid in sids:
        for r in receipts.latest(sid, 10):
            timeline.append({"kind": "receipt", "at": r["at"], "session_id": sid, "verified": r["verified"],
                             "files": (r["diff"] or {}).get("files"), "test": (r["tests"] or {}).get("last_line")})
    for d in db.rows("SELECT id, session_id, asked_at, question, state, answer, answered_at FROM decisions WHERE work_item=?"
                     " OR session_id IN (%s)" % ",".join("?" * len(sids) or ["''"]), (wi, *sids)):
        timeline.append({"kind": "decision", "at": d["asked_at"], **d})
    for c in cs:
        if c.get("final_at"):
            timeline.append({"kind": "final", "at": sources.iso_epoch(c["final_at"]), "session_id": c["session_id"],
                             "text": (c.get("final") or "")[-600:], "name": c["name"]})
    timeline.sort(key=lambda x: x.get("at") or 0, reverse=True)
    props = db.rows("SELECT id, kind, why, state, created_at FROM cp_proposals WHERE work_item=? ORDER BY created_at DESC LIMIT 20", (wi,))
    return {**w, "home_seat": home_seat(wi, snap), "state_text": read_state(wi), "state_path": state_path(wi),
            "sessions": [{"session_id": c["session_id"], "key": c["key"], "name": c["name"], "seat": c["account"],
                          "state": c["state"], "activity": c.get("activity")} for c in cs],
            "timeline": timeline[:80], "proposals": props}


def brief(wi: str) -> str:
    note = read_state(wi)
    return f"You are continuing work item {wi}. State of play:\n{note}" if note else f"You are starting work item {wi}."


# ------------------------------------------------------------------ proposals
def is_test(c: dict) -> bool:
    """Throwaway sessions started by cp's own live tests never shape a work item's state of play."""
    return bool(re.search(r"\b(throwaway|cp-e2e)\b", c.get("name") or "", re.I))


def regen_block(wi: str, snap: dict) -> str:
    """The cp-owned block of a state doc, TOON style, from facts only."""
    from . import receipts
    cs = [c for c in snap["chats"] if c.get("ws") == wi and c["kind"] != "headless" and not is_test(c)]
    lines = [AUTO_START, "cp_auto:", f"  as_of: {today_local()}"]
    lines.append(f"  sessions[{len(cs)}]{{name,seat,state,last_final}}:")
    for c in cs[:12]:
        last = " ".join((c.get("final") or "").split())[:140].replace(",", ";")
        lines.append(f"    {c['name'][:60].replace(',', ';')},{c['account']},{c['state']},{last}")
    rs = []
    for c in cs:
        rs += [(c["name"], r) for r in receipts.latest(c["session_id"], 3)]
    rs.sort(key=lambda x: x[1]["at"], reverse=True)
    lines.append(f"  receipts[{min(len(rs), 8)}]{{at,session,files,test,verified}}:")
    for name, r in rs[:8]:
        test = ((r["tests"] or {}).get("last_line") or "").replace(",", ";")[:120]
        lines.append(f"    {time.strftime('%m-%d %H:%M', time.localtime(r['at']))},{name[:40].replace(',', ';')},"
                     f"{(r['diff'] or {}).get('files')},{test},{'yes' if r['verified'] else 'no'}")
    dec = db.rows("SELECT id, question FROM decisions WHERE state='open' AND work_item=?", (wi,))
    lines.append(f"  decisions_open[{len(dec)}]{{id,question}}:")
    for d in dec:
        lines.append(f"    {d['id']},{d['question'][:140].replace(',', ';')}")
    lines.append(AUTO_END)
    return "\n".join(lines)


def merged_doc(old: str, block: str) -> str:
    if AUTO_START in old and AUTO_END in old:
        pre, rest = old.split(AUTO_START, 1)
        post = rest.split(AUTO_END, 1)[1]
        return pre + block + post
    return (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"


def _facts_sig(block: str) -> str:
    body = "\n".join(l for l in block.splitlines() if "as_of:" not in l)
    return hashlib.sha1(body.encode()).hexdigest()


def propose_state(wi: str, snap: dict, now: float | None = None, force: bool = False) -> dict | None:
    now = now or time.time()
    block = regen_block(wi, snap)
    sig = _facts_sig(block)
    last = db.rows("SELECT sig, created_at, state FROM cp_proposals WHERE kind='state_doc' AND work_item=? ORDER BY id DESC LIMIT 1", (wi,))
    if last and not force and (last[0]["sig"] == sig or (last[0]["state"] == "proposed" and now - last[0]["created_at"] < REGEN_EVERY_S)):
        return None
    old = read_state(wi)
    new = merged_doc(old, block)
    if new == old:
        return None
    db.execute("UPDATE cp_proposals SET state='superseded', decided_at=? WHERE kind='state_doc' AND work_item=? AND state='proposed'", (now, wi))
    db.execute("INSERT INTO cp_proposals(kind, work_item, target, old, new, why, sig, created_at) VALUES(?,?,?,?,?,?,?,?)",
               ("state_doc", wi, state_path(wi), old, new, "receipts, decisions or finals changed", sig, now))
    return db.rows("SELECT * FROM cp_proposals WHERE kind='state_doc' AND work_item=? ORDER BY id DESC LIMIT 1", (wi,))[0]


def diff_text(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "current", "proposed", n=2))


def write_state(wi: str, text: str, by: str) -> None:
    os.makedirs(history_dir(), exist_ok=True)
    p = state_path(wi)
    if os.path.exists(p):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(history_dir(), f"{os.path.basename(p)[:-3]}.{stamp}.md"), "w", encoding="utf-8") as fh:
            fh.write(open(p, encoding="utf-8").read())
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)
    db.execute("UPDATE work_items SET updated_at=? WHERE id=?", (time.time(), wi))
    db.log_auto("state_doc", "manual", None, None, "written", by, {"path": p, "chars": len(text)})


PROPOSAL_TTL_S = 24 * 3600
MARKED_DONE = "You marked it done"


def sweep_proposals(now: float | None = None) -> int:
    """Nothing waits on you for days. An open proposal older than a day expires: a state doc
    is proposed again when its facts change (propose_state keys on the facts signature), a rule card comes back
    with the next day's corrections. A newer rule card for the same correction kind replaces the older one."""
    now = now or time.time()
    n = db.execute("UPDATE cp_proposals SET state='expired', decided_at=? WHERE state='proposed' AND created_at < ?",
                   (now, now - PROPOSAL_TTL_S))
    seen = set()
    for r in db.rows("SELECT p.id, r.pattern FROM cp_proposals p JOIN rules_proposed r ON r.id = CAST(p.target AS INTEGER)"
                     " WHERE p.kind='rule' AND p.state='proposed' ORDER BY p.created_at DESC, p.id DESC"):
        if r["pattern"] in seen:
            n += db.execute("UPDATE cp_proposals SET state='superseded', decided_at=? WHERE id=?", (now, r["id"]))
        seen.add(r["pattern"])
    return n


def proposals_open() -> list[dict]:
    sweep_proposals()
    return db.rows("SELECT id, kind, work_item, session_id, target, why, created_at, detail FROM cp_proposals WHERE state='proposed' ORDER BY created_at")


def proposal(pid: int) -> dict | None:
    r = db.rows("SELECT * FROM cp_proposals WHERE id=?", (pid,))
    if not r:
        return None
    p = r[0]
    p["diff"] = diff_text(p["old"] or "", p["new"] or "") if p["kind"] == "state_doc" else None
    return p


def decide(pid: int, action: str, snap: dict, sender=None, stopper=None) -> tuple[int, dict]:
    p = proposal(pid)
    if not p:
        return 404, {"error": "no such proposal"}
    if p["state"] != "proposed":
        return 409, {"error": f"already {p['state']}"}
    now = time.time()
    if action == "skip":
        db.execute("UPDATE cp_proposals SET state='skipped', decided_at=? WHERE id=?", (now, pid))
        return 200, {"ok": True, "state": "skipped"}
    if action != "apply":
        return 400, {"error": "action must be apply or skip"}
    if p["kind"] == "state_doc":
        cur = open(p["target"], encoding="utf-8").read() if os.path.exists(p["target"]) else ""
        if cur != (p["old"] or ""):
            db.execute("UPDATE cp_proposals SET state='stale', decided_at=?, detail=? WHERE id=?",
                       (now, "the file changed after this was proposed; a fresh proposal follows", pid))
            return 409, {"error": "the file changed since this was proposed; not written (a fresh proposal will follow)"}
        write_state(p["work_item"], p["new"], f"proposal {pid} applied by you")
        db.execute("UPDATE cp_proposals SET state='applied', decided_at=? WHERE id=?", (now, pid))
        return 200, {"ok": True, "state": "applied"}
    if p["kind"] == "handoff":
        return confirm_handoff(p, snap, sender, stopper)
    if p["kind"] == "rule":
        db.execute("UPDATE cp_proposals SET state='applied', decided_at=? WHERE id=?", (now, pid))
        db.execute("UPDATE rules_proposed SET state='accepted' WHERE id=?", (int(p["target"] or 0),))
        return 200, {"ok": True, "state": "accepted", "note": "recorded only; no CLAUDE.md was edited"}
    return 400, {"error": "unknown proposal kind"}


# ------------------------------------------------------------------ handoff
def looks_done(chat: dict) -> tuple[bool, str]:
    from . import receipts
    if chat.get("state") not in ("idle", "stopped") or chat.get("kind") != "bg" or chat.get("excluded"):
        return False, "not an idle background chat"
    r = receipts.latest(chat["session_id"], 1)
    if not r or not r[0]["verified"]:
        return False, "no verified receipt for the last turn"
    if db.rows("SELECT id FROM decisions WHERE state='open' AND session_id=?", (chat["session_id"],)):
        return False, "open decision"
    if not DONE_RE.search((chat.get("final") or "").strip()[-400:]):
        return False, "the final does not say done"
    return True, "verified receipt, no open decisions, final says done"


def propose_handoff(chat: dict, why: str) -> dict | None:
    # A handoff writes into the work item's state doc; a chat with no work item has no state doc to hand off to.
    if chat.get("excluded") or not chat.get("ws"):
        return None
    if db.rows("SELECT id FROM cp_proposals WHERE kind='handoff' AND session_id=? AND state IN ('proposed','sending','applied')",
               (chat["session_id"],)):
        return None
    wi = chat["ws"]
    # sig = the final this proposal was based on; a newer turn makes it stale (expire_handoffs)
    db.execute("INSERT INTO cp_proposals(kind, work_item, session_id, target, why, created_at, detail, sig) VALUES(?,?,?,?,?,?,?,?)",
               ("handoff", wi, chat["session_id"], state_path(wi), why, time.time(), chat["name"][:120], chat.get("final_at") or ""))
    return db.rows("SELECT * FROM cp_proposals WHERE kind='handoff' AND session_id=? ORDER BY id DESC LIMIT 1", (chat["session_id"],))[0]


def handoff_stale(p: dict, chat: dict | None) -> str | None:
    """Why a proposed handoff no longer holds, or None. Read from the chat as it is now."""
    if chat is None:
        return None                                   # not visible right now: keep, decide later
    if not chat.get("ws"):
        return "the chat has no work item, so there is no state doc to hand off to"
    if p.get("sig") and chat.get("final_at") and chat["final_at"] != p["sig"]:
        return "the chat has had a newer turn since this was proposed"
    if not p.get("sig") and chat.get("final_at"):    # proposals made before sig was recorded
        from datetime import datetime
        try:
            fa = datetime.fromisoformat(chat["final_at"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            fa = None
        if fa and fa > (p.get("created_at") or 0) + 5:
            return "the chat has had a newer turn since this was proposed"
    if (p.get("why") or "").startswith(MARKED_DONE):
        return None                                   # your own call; only a newer turn withdraws it
    ok, why = looks_done(chat)
    if not ok and chat.get("state") != "stopped":
        return f"no longer looks done: {why}"
    return None


def expire_handoffs(snap: dict, now: float | None = None) -> int:
    now = now or time.time()
    by_sid = {c["session_id"]: c for c in snap["chats"]}
    n = 0
    for p in db.rows("SELECT * FROM cp_proposals WHERE kind='handoff' AND state='proposed'"):
        why = handoff_stale(p, by_sid.get(p["session_id"]))
        if why:
            n += db.execute("UPDATE cp_proposals SET state='stale', decided_at=?, detail=? WHERE id=? AND state='proposed'",
                            (now, why, p["id"]))
    return n


def confirm_handoff(p: dict, snap: dict, sender, stopper) -> tuple[int, dict]:
    chat = next((c for c in snap["chats"] if c["session_id"] == p["session_id"]), None)
    if not chat:
        return 409, {"error": "the chat is not visible right now"}
    why = handoff_stale(p, chat)
    if why:
        db.execute("UPDATE cp_proposals SET state='stale', decided_at=?, detail=? WHERE id=?", (time.time(), why, p["id"]))
        return 409, {"error": f"not handed off: {why}"}
    mode = db.mode("handoff")
    now = time.time()
    text = HANDOFF.format(path=p["target"], date=today_local(), sid8=chat["session_id"][:8])
    if mode != "on":
        db.execute("UPDATE cp_proposals SET state='applied', decided_at=?, detail=? WHERE id=?",
                   (now, f"{mode}: confirmed by you, nothing sent", p["id"]))
        db.log_auto("handoff", mode, chat["session_id"], chat["account"], "would send", "user confirmed done", {"text": text})
        return 200, {"ok": True, "mode": mode, "sent": False, "note": "handoff mode is dry-run: confirmed and logged, nothing sent"}
    if not sender or not stopper:
        return 501, {"error": "read-only server"}
    db.execute("UPDATE cp_proposals SET state='sending', decided_at=? WHERE id=?", (now, p["id"]))
    threading.Thread(target=_run_handoff, args=(p, chat, text, sender, stopper), daemon=True).start()
    return 200, {"ok": True, "mode": mode, "sent": True, "note": "sending; the chat is stopped once the note is on disk"}


def _run_handoff(p, chat, text, sender, stopper, wait_s=900):
    path, marker = p["target"], f"({chat['session_id'][:8]})"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    before = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    res = sender(chat, text)
    if not res.get("ok"):
        db.execute("UPDATE cp_proposals SET state='failed', detail=? WHERE id=?", (str(res.get("error"))[:300], p["id"]))
        db.log_auto("handoff", "on", chat["session_id"], chat["account"], "failed", "send failed", {"result": res})
        return
    end = time.time() + wait_s
    while time.time() < end:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if cur != before and "## handoff" in cur and marker in cur:
            st = stopper(chat)
            db.execute("UPDATE cp_proposals SET state='applied', detail=? WHERE id=?",
                       (f"note verified on disk; stop: {'ok' if st.get('ok') else st.get('out') or st.get('error')}", p["id"]))
            db.log_auto("handoff", "on", chat["session_id"], chat["account"], "done", "note on disk, session stopped",
                        {"path": path, "stop": st})
            return
        time.sleep(5)
    db.execute("UPDATE cp_proposals SET state='failed', detail=? WHERE id=?", ("no note appeared within 15 min; session left running", p["id"]))
    db.log_auto("handoff", "on", chat["session_id"], chat["account"], "failed", "note not verified on disk", {"path": path})


# ------------------------------------------------------------------ spawn
CLAUDE = config.claude_bin()
SCRUB = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
         "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_JOB_DIR")


def safe_cwd(cwd: str | None) -> str | None:
    """A folder you picked on the graph (a repo or a file's folder): must exist and sit under the home dir."""
    if not cwd:
        return None
    p = os.path.realpath(os.path.expanduser(cwd))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    return p if os.path.isdir(p) and p.startswith(HOME + os.sep) and "/." not in p[len(HOME):] else None


def launch_cmd(wi: str | None, seat: str, brief_text: str, cwd: str | None = None) -> tuple[list, str, dict]:
    title = f"{wi} session" if wi else "DHI Orbit session"
    cwd = safe_cwd(cwd) or config.default_cwd()
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    if seat == "main":
        env.pop("CLAUDE_CONFIG_DIR", None)           # ~/.claude is the default dir: unset, not set
    else:
        env["CLAUDE_CONFIG_DIR"] = os.path.join(HOME, f".claude-{seat}")
    cmd = [CLAUDE, "--bg", "--add-dir", state_dir(), "-n", title, "--model", "sonnet", "--effort", "medium",
           "--permission-mode", "auto", brief_text]
    return cmd, cwd, env


def holds(seat: dict) -> bool:
    """A new job waits in the queue while the seat is near its limit or blocked. A seat with no reading yet (a fresh
    account: the meter writes its first line during its first chat) starts at once, or nothing would ever start it;
    only a partial reading that is already near the limit still waits."""
    if seat["state"] in ("near", "blocked"):
        return True
    known = [(seat.get(k) or {}).get("pct") for k in ("five_hour", "seven_day")]
    return seat["state"] == "unknown" and any(p is not None and p >= sources.NEAR_PCT for p in known)


def spawn(body: dict, snap: dict, runner=subprocess.run) -> tuple[int, dict]:
    wi, seat = body.get("work_item"), body.get("seat")
    text = body.get("brief")
    text = text.strip() if isinstance(text, str) else ""
    if not seat:
        return 400, {"error": "seat required (you pick it)"}
    if config.is_read_only(seat):
        return 403, {"error": "this account is read-only here"}
    st = next((s for s in snap["seats"] if s["seat"] == seat), None)
    if not st:
        return 400, {"error": f"unknown seat {seat}"}
    if not text:
        text = brief(wi) if wi else ""
    if not text:
        return 400, {"error": "empty brief"}
    if body.get("interview"):
        text = INTERVIEW.format(path=state_path(wi or "misc"), date=today_local()) + text
    if body.get("parent"):
        text += f"\n\n(Started from the DHI Orbit board as a child of session {body['parent']}.)"
    now = time.time()
    note = None
    if body.get("cwd") and not safe_cwd(body["cwd"]):
        note = (f"folder {body['cwd']} was not used (it must be an existing folder under your home folder, not a "
                f"hidden one); the chat starts in {config.default_cwd()}")
    if holds(st):
        db.execute("INSERT INTO cp_spawn_queue(work_item, seat, brief, interview, parent, created_at, cwd) VALUES(?,?,?,?,?,?,?)",
                   (wi, seat, text, 1 if body.get("interview") else 0, body.get("parent"), now, safe_cwd(body.get("cwd"))))
        db.log_auto("spawn", "manual", None, seat, "queued", f"seat {st['state']}", {"work_item": wi})
        return 200, {"ok": True, "queued": True, "seat": seat, **({"note": note} if note else {})}
    code, res = _start(wi, seat, text, runner, body.get("cwd"))
    return code, ({**res, "note": note} if note and code == 200 else res)


def _start(wi, seat, text, runner, cwd=None):
    cmd, cwd, env = launch_cmd(wi, seat, text, cwd)
    try:
        r = runner(cmd, cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 409, {"error": f"{type(e).__name__}: {e}"}
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"attach ([0-9a-f]{8})", out) or re.search(r"backgrounded\W+(?:\x1b\[[0-9;]*m)?([0-9a-f]{8})", out)
    if r.returncode != 0 or not m:
        db.log_auto("spawn", "manual", None, seat, "failed", out[-300:], {"work_item": wi})
        return 409, {"error": out[-300:] or "claude --bg did not start"}
    db.log_auto("spawn", "manual", None, seat, "started", f"job {m.group(1)}", {"work_item": wi})
    return 200, {"ok": True, "queued": False, "job_id": m.group(1), "seat": seat}


def drain_queue(snap: dict, runner=subprocess.run) -> list:
    out = []
    seats = {s["seat"]: s for s in snap["seats"]}
    for q in db.rows("SELECT * FROM cp_spawn_queue WHERE state='queued' ORDER BY id"):
        if q["seat"] not in seats or holds(seats[q["seat"]]):
            continue
        code, res = _start(q["work_item"], q["seat"], q["brief"], runner, q.get("cwd"))
        db.execute("UPDATE cp_spawn_queue SET state=?, started_at=?, job_id=?, detail=? WHERE id=?",
                   ("started" if code == 200 else "failed", time.time(), res.get("job_id"), res.get("error"), q["id"]))
        out.append(res)
    return out


# ------------------------------------------------------------------ the loop
class WorkLoop:
    def __init__(self):
        init()
        self.last_regen: dict[str, float] = {}

    def tick(self, snap: dict, now: float | None = None) -> None:
        now = now or time.time()
        workitems.seed(snap["chats"])
        for c in snap["chats"]:
            if is_test(c):
                continue
            ok, why = looks_done(c)
            if ok:
                propose_handoff(c, why)
        expire_handoffs(snap, now)
        for w in {c.get("ws") for c in snap["chats"] if c.get("ws")}:
            if now - self.last_regen.get(w, 0) >= REGEN_EVERY_S:
                self.last_regen[w] = now
                propose_state(w, snap, now)
        drain_queue(snap)


def collisions(snap: dict, window_s: float = 7200) -> list[tuple]:
    """Two different visible sessions whose receipts in the window touched the same file."""
    from . import receipts
    now, by_path = time.time(), {}
    for c in snap["chats"]:
        for r in receipts.latest(c["session_id"], 5):
            if now - r["at"] > window_s:
                continue
            for f in r["files"] or []:
                by_path.setdefault(f["path"], set()).add(c["session_id"])
    out = []
    for path, sids in by_path.items():
        s = sorted(sids)
        for i in range(len(s)):
            for j in range(i + 1, len(s)):
                out.append((path, s[i], s[j]))
    return out
