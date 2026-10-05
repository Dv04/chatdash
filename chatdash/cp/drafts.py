"""A7 shadow drafts: a suggested reply for a chat that waits on the user. Never sent; the board can only put it
into the reply box. Mode "shadow_drafts":
  dry-run  retrieval only, zero model calls: the user's own replies to the 4 most similar OLDER finals in OTHER
           chats (turns_fts), shown as "you replied like this before" (class "retrieved")
  on       one Sonnet call per seat batch (chat's own seat, $2/day cap via shadow.py) with those 4 pairs plus
           a closed-world facts table (every fact has source and date); no fact that answers it -> ESCALATE
Whether the user used it is recorded when the next real reply in that chat is close to the draft.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from . import db, shadow

STOP = set("the a an and or but if then this that it is are was were be to of in on for with as at by from we you i "
           "me my our your not no yes do does did done can could should would will just so all any some there here "
           "what which who when where why how up out about into over than also only very more most".split())


def terms(text: str, k: int = 10) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text or "")]
    seen, out = set(), []
    for w in reversed(words):                    # the end of a final is where the question is
        if w not in STOP and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= k:
            break
    return out


def similar_pairs(final: str, key: str, before_ts: str | None, limit: int = 4, path: str | None = None) -> list[dict]:
    q = " OR ".join(f'"{t}"' for t in terms(final))
    if not q:
        return []
    c = sqlite3.connect(f"file:{path or db.db_path()}?mode=ro", uri=True, timeout=10)
    try:
        hits = c.execute("SELECT key, idx, ts, final FROM turns_fts WHERE turns_fts MATCH ? AND key != ? AND final != ''"
                         " ORDER BY rank LIMIT 60", (f"final:({q})", key)).fetchall()
        out = []
        for k, idx, ts, fin in hits:
            if before_ts and ts and ts >= before_ts:
                continue
            nxt = c.execute("SELECT prompt FROM turns_fts WHERE key=? AND CAST(idx AS INTEGER)=?", (k, int(idx) + 1)).fetchone()
            if not nxt or not nxt[0] or nxt[0].lstrip().startswith(("<", "[keepalive")):
                continue
            out.append({"key": k, "idx": int(idx), "ts": ts, "final": fin[-500:], "reply": nxt[0][:500]})
            if len(out) >= limit:
                break
        return out
    finally:
        c.close()


def facts() -> list[dict]:
    return db.rows("SELECT subject, fact, source, as_of FROM facts ORDER BY added_at DESC LIMIT 200")


def overlap(a: str, b: str) -> float:
    x, y = set(terms(a, 40)), set(terms(b, 40))
    return len(x & y) / max(1, len(x | y))


DRAFT_PROMPT = """You draft the next reply an operator (the user) would send to a coding agent. Closed world: use ONLY
the agent's final message, the user's own past replies to similar messages ("past"), and the facts list (each has a
source and date). If the reply needs a fact, preference or decision that is not in those, output escalate=true.
Never invent facts. Output JSON only: [{"id": "<item id>", "draft": "<reply or empty>", "escalate": true|false,
"basis": ["past:<n>" or "fact:<subject>", ...]}]
Items:
"""


class Drafter:
    def __init__(self, mode_fn=db.mode, model=None):
        self.mode_fn, self.model = mode_fn, model or shadow.call_model
        self.last = 0.0
        self.pending: dict[str, list] = {}

    def tick(self, snap: dict, now: float | None = None, every_s: float = 120) -> int:
        now = now or time.time()
        if self.mode_fn("shadow_drafts") == "off" or now - self.last < every_s:
            return 0
        self.last = now
        made = 0
        for c in shadow.candidates(snap, set()):
            basis = c["id"]
            if db.rows("SELECT id FROM suggestions WHERE class='retrieved' AND basis=?", (basis,)):
                continue
            chat = next((x for x in snap["chats"] if x["session_id"] == c["session_id"]), {})
            pairs = similar_pairs(c["final"], chat.get("key", ""), chat.get("final_at"))
            if not pairs:
                continue
            draft = {"kind": "retrieved", "pairs": pairs, "text": pairs[0]["reply"]}
            db.execute("INSERT INTO suggestions(session_id, turn, at, draft, class, basis, used) VALUES(?,?,?,?,?,?,0)",
                       (c["session_id"], None, now, json.dumps(draft)[:12000], "retrieved", basis))
            made += 1
            if self.mode_fn("shadow_drafts") == "on":
                self.pending.setdefault(c["config"], []).append(dict(c, past=[{"final": p["final"], "reply": p["reply"]} for p in pairs]))
        if self.mode_fn("shadow_drafts") == "on":
            made += self.llm(snap, now)
        self.mark_used(snap)
        return made

    def llm(self, snap: dict, now: float) -> int:
        """Mode on only: one call per seat batch on that seat, $2/day total (shared with prose splitting)."""
        seats = {s["config"]: s for s in snap.get("seats") or []}
        made, fs = 0, [{"subject": f["subject"], "fact": f["fact"], "source": f["source"], "as_of": f["as_of"]} for f in facts()]
        for cfg, items in list(self.pending.items()):
            self.pending.pop(cfg, None)
            if shadow.spent_today() >= shadow.DAY_CAP_USD or (seats.get(cfg) or {}).get("state") in ("near", "blocked", "unknown"):
                continue
            batch = [dict(i, facts=fs) for i in items[:shadow.BATCH]]
            try:
                parsed, cost = self.model(cfg, batch, prompt=DRAFT_PROMPT)
            except Exception as e:
                db.log_auto("shadow_call", "on", None, None, "failed", f"drafts: {type(e).__name__}: {e}"[:300], {"cost_usd": 0})
                continue
            db.log_auto("shadow_call", "on", None, None, "called", f"drafts: {len(batch)} items", {"cost_usd": cost})
            by = {b["id"]: b for b in batch}
            for r in parsed:
                b = by.get(r.get("id"))
                if not b:
                    continue
                cls = "escalate" if r.get("escalate") or not r.get("draft") else "llm"
                db.execute("INSERT INTO suggestions(session_id, turn, at, draft, class, basis, used) VALUES(?,?,?,?,?,?,0)",
                           (b["session_id"], None, now, json.dumps({"kind": cls, "text": r.get("draft") or "", "basis": r.get("basis")}),
                            cls, b["id"]))
                made += 1
        return made

    def mark_used(self, snap: dict) -> None:
        """A draft counts as used when the user's next real reply in that chat overlaps it (Jaccard >= 0.5)."""
        for s in db.rows("SELECT id, session_id, at, draft FROM suggestions WHERE class='retrieved' AND used=0 AND at > ?",
                         (time.time() - 3 * 86400,)):
            chat = next((x for x in snap["chats"] if x["session_id"] == s["session_id"]), None)
            if not chat or not chat.get("last_prompt_at"):
                continue
            from ..extract import iso_epoch
            if (iso_epoch(chat["last_prompt_at"]) or 0) <= s["at"]:
                continue
            d = json.loads(s["draft"])
            score = overlap(d.get("text", ""), chat.get("last_prompt") or "")
            db.execute("UPDATE suggestions SET used=? WHERE id=?", (1 if score >= 0.5 else -1, s["id"]))


def latest_draft(session_id: str) -> dict | None:
    r = db.rows("SELECT * FROM suggestions WHERE session_id=? AND class IN ('retrieved','llm','escalate') ORDER BY at DESC LIMIT 1",
                (session_id,))
    if not r:
        return None
    r = r[0]
    r["draft"] = json.loads(r["draft"] or "{}")
    return r


def precision() -> dict:
    rows = db.rows("SELECT used, COUNT(*) AS n FROM suggestions WHERE class='retrieved' GROUP BY used")
    m = {r["used"]: r["n"] for r in rows}
    judged = m.get(1, 0) + m.get(-1, 0)
    return {"drafts": sum(m.values()), "judged": judged, "matched": m.get(1, 0),
            "match_rate": round(m.get(1, 0) / judged, 3) if judged else None}
