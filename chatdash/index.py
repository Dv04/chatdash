"""Local SQLite: full-text search over prompts and final answers, plus read/unread marks."""
from __future__ import annotations

import sqlite3
import threading

from . import config


class Index:
    def __init__(self, path: str | None = None):
        if path is None:
            config.ensure_home()
            path = config.db_path()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    key UNINDEXED, name UNINDEXED, account UNINDEXED, idx UNINDEXED, ts UNINDEXED,
                    prompt, final, tokenize='porter unicode61');
                CREATE TABLE IF NOT EXISTS indexed (key TEXT PRIMARY KEY, version INTEGER);
                CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, final_at TEXT);
            """)
            self.conn.commit()
        self.versions = dict(self.conn.execute("SELECT key, version FROM indexed"))

    def sync(self, chat: dict, turns: list[dict]) -> None:
        if self.versions.get(chat["key"]) == chat["version"]:
            return
        with self.lock:
            self.conn.execute("DELETE FROM turns_fts WHERE key=?", (chat["key"],))
            self.conn.executemany(
                "INSERT INTO turns_fts(key,name,account,idx,ts,prompt,final) VALUES(?,?,?,?,?,?,?)",
                [(chat["key"], chat["name"], chat["account"], i, t.get("fts") or t.get("pts"),
                  t.get("prompt", ""), t.get("final", "")) for i, t in enumerate(turns)])
            self.conn.execute("INSERT OR REPLACE INTO indexed(key,version) VALUES(?,?)",
                              (chat["key"], chat["version"]))
            self.conn.commit()
        self.versions[chat["key"]] = chat["version"]

    def search(self, q: str, limit: int = 40) -> list[dict]:
        q = " ".join(f'"{w}"' for w in q.replace('"', " ").split())
        if not q:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, name, account, idx, ts, snippet(turns_fts, 5, '[', ']', ' ... ', 12),"
                " snippet(turns_fts, 6, '[', ']', ' ... ', 24) FROM turns_fts WHERE turns_fts MATCH ?"
                " ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        return [{"key": r[0], "name": r[1], "account": r[2], "turn": r[3], "ts": r[4],
                 "prompt": r[5], "final": r[6]} for r in rows]

    def seen_map(self) -> dict[str, str]:
        with self.lock:
            return dict(self.conn.execute("SELECT key, final_at FROM seen"))

    def mark_seen(self, key: str, final_at: str | None) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO seen(key, final_at) VALUES(?,?)", (key, final_at or ""))
            self.conn.commit()
