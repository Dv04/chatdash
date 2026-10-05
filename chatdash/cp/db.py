"""cp tables in chatdash.db. Additive only: CREATE TABLE IF NOT EXISTS, never ALTER or DROP an
existing chatdash table. Every connection is short-lived so a hook process and the server can
share the file (WAL is not switched on: chatdash.db is also opened by the server)."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .. import config

HERE = os.path.dirname(os.path.abspath(__file__))
DB: str | None = None          # tests point this at a temp file; None means the data dir's chatdash.db

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY, title TEXT, workstream TEXT, state_path TEXT,
    state TEXT DEFAULT 'active', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, session_id TEXT, config TEXT, seat TEXT, work_item TEXT,
    source TEXT, asked_at REAL, question TEXT, options TEXT, recommended TEXT,
    risk TEXT DEFAULT 'med', on_timeout TEXT DEFAULT 'dialog', timeout_at REAL,
    state TEXT DEFAULT 'open', answer TEXT, answered_at REAL, answered_by TEXT, evidence TEXT);
CREATE INDEX IF NOT EXISTS decisions_state ON decisions(state, asked_at);
CREATE TABLE IF NOT EXISTS receipts (
    session_id TEXT, turn INTEGER, at REAL, config TEXT, cwd TEXT,
    diff TEXT, tests TEXT, prs TEXT, files TEXT, cost_units REAL, verified INTEGER,
    PRIMARY KEY (session_id, turn));
CREATE TABLE IF NOT EXISTS capacity (
    seat TEXT, at REAL, five REAL, five_resets REAL, seven REAL, seven_resets REAL,
    PRIMARY KEY (seat, at));
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INTEGER, at REAL,
    draft TEXT, class TEXT, basis TEXT, used INTEGER);
CREATE TABLE IF NOT EXISTS auto_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, action TEXT, mode TEXT, session_id TEXT,
    seat TEXT, decision TEXT, reason TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, fact TEXT, source TEXT, as_of TEXT,
    added_at REAL);
CREATE TABLE IF NOT EXISTS rules_proposed (
    id INTEGER PRIMARY KEY AUTOINCREMENT, week TEXT, pattern TEXT, examples TEXT,
    draft_rule TEXT, check_cmd TEXT, state TEXT DEFAULT 'proposed', created_at REAL);
CREATE TABLE IF NOT EXISTS cp_gate (session_id TEXT PRIMARY KEY, on_ INTEGER, set_by TEXT, at REAL);
CREATE TABLE IF NOT EXISTS cp_gate_blocks (session_id TEXT, turn INTEGER, n INTEGER, at REAL, PRIMARY KEY (session_id, turn));
CREATE TABLE IF NOT EXISTS cp_graph_history (at REAL PRIMARY KEY, frame TEXT);
CREATE TABLE IF NOT EXISTS cp_settings (key TEXT PRIMARY KEY, val TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS cp_metrics (
    name TEXT, at REAL, value REAL, detail TEXT, PRIMARY KEY (name, at));
CREATE TABLE IF NOT EXISTS cp_dismissed (item TEXT PRIMARY KEY, at REAL, title TEXT);
"""


def db_path() -> str:
    return DB or config.db_path()


def config_path() -> str:
    return CONFIG or config.path()


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


ADDED_COLUMNS = {   # cp's own tables only; never an existing chatdash table
    "decisions": ["parts TEXT", "tool_use_id TEXT", "held INTEGER DEFAULT 0", "hold_until REAL", "job_id TEXT",
                  "mode TEXT", "delivered INTEGER DEFAULT 0", "delivery TEXT", "risk_why TEXT", "key TEXT"],
}


def init(path: str | None = None) -> None:
    c = connect(path)
    c.executescript(SCHEMA)
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    c.commit()
    c.close()


def rows(sql: str, args=(), path: str | None = None) -> list[dict]:
    c = connect(path)
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def execute(sql: str, args=(), path: str | None = None) -> int:
    c = connect(path)
    try:
        n = c.execute(sql, args).rowcount
        c.commit()
        return n
    finally:
        c.close()


def log_auto(action: str, mode: str, session_id: str | None, seat: str | None, decision: str,
             reason: str, evidence: dict | None = None, path: str | None = None) -> None:
    """Every auto-action decision, taken or not, with the transcript evidence it was based on."""
    execute("INSERT INTO auto_log(at, action, mode, session_id, seat, decision, reason, evidence)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), action, mode, session_id, seat, decision, reason,
             json.dumps(evidence or {}, default=str)[:8000]), path)


CONFIG: str | None = None      # tests point this at a temp file; None means <data dir>/config.json
MODES = ("off", "dry-run", "on")


def mode(name: str) -> str:
    """off / dry-run / on for one auto-action. Unreadable or unknown means dry-run."""
    m = config.load(config_path()).get(name)
    return m if m in MODES else "dry-run"


DEFAULT_SETTINGS = {"focus": {"on": False, "min_age_min": 15, "windows": ["10:00", "14:00"]},
                    "pace": {"pct_per_day": round(100 / 7, 1)}}   # even weekly spend: 100% of the 7d limit over 7 days


def settings() -> dict:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for r in rows("SELECT key, val FROM cp_settings"):
        try:
            out[r["key"]] = json.loads(r["val"])
        except ValueError:
            pass
    return out


def put_setting(key: str, val) -> None:
    execute("INSERT OR REPLACE INTO cp_settings(key, val, updated_at) VALUES(?,?,?)",
            (key, json.dumps(val), time.time()))
