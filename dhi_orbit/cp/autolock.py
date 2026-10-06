"""Only one process runs cp's auto-actions (limit-resume, handoff, notifications): whoever holds this
exclusive lock. Two DHI Orbit servers (say a dev server beside the main one) can run without double-sending."""
from __future__ import annotations

import os

from .. import _plat, config

_fh = None


def acquire(path: str | None = None) -> bool:
    """Non-blocking; True if this process now owns auto-actions. Released when the process exits."""
    global _fh
    if _fh is not None:
        return True
    path = path or os.path.join(config.run_dir(), "auto.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    if not _plat.try_lock(fh):
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _fh = fh
    return True
