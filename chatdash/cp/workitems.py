"""Work items: groups of chats whose names match config work_item_pattern (none by default), each with a
state-of-play doc under <data dir>/work/state/<id>.md."""
from __future__ import annotations

import time

from . import db


def seed(chats: list[dict] | None, path: str | None = None) -> int:
    """Insert every work item that appears in `chats` and is not in work_items yet (the title is the id;
    an existing row, its title and its state are never overwritten). Returns the number added."""
    from . import work
    n, now = 0, time.time()
    for wi in sorted({c.get("ws") for c in chats or [] if c.get("ws")}):
        n += db.execute(
            "INSERT OR IGNORE INTO work_items(id, title, workstream, state_path, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)", (wi, wi, wi, work.state_path(wi), now, now), path)
    return n
