"""Files attached to a reply from the dashboard. The browser POSTs the raw bytes to /api/cp/uploads (name in the X-Filename header); the file
is saved under uploads/<day>/<id>-<name> and the absolute path goes back, so the reply text can name it and the agent reads it with its own
file tools. Local only (the server's host and token guard runs first), a type allowlist, a size cap, 0700 dirs and 0600 files, and anything
older than KEEP_DAYS is removed on the next upload."""
from __future__ import annotations

import os
import re
import time
import uuid

DIR = os.environ.get("CP_UPLOAD_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
MAX_BYTES = 25 * 1024 * 1024
KEEP_DAYS = 30
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
ALLOWED = IMAGES | {".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".docx", ".xlsx", ".pptx"}


def clean_name(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    return stem[:80] + ext.lower()


def save(name: str, data: bytes, now: float | None = None) -> tuple[int, dict]:
    now = now or time.time()
    if not data:
        return 400, {"error": "empty file"}
    if len(data) > MAX_BYTES:
        return 413, {"error": f"file too large (limit {MAX_BYTES // (1024 * 1024)} MB)"}
    safe = clean_name(name)
    ext = os.path.splitext(safe)[1]
    if ext not in ALLOWED:
        return 415, {"error": f"{ext or 'this'} files are not accepted (images, PDF, text, CSV, JSON, Office documents)"}
    day = os.path.join(DIR, time.strftime("%Y%m%d", time.localtime(now)))
    os.makedirs(day, mode=0o700, exist_ok=True)
    path = os.path.join(day, f"{uuid.uuid4().hex[:8]}-{safe}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    prune(now)
    return 200, {"ok": True, "path": path, "name": os.path.basename(name or safe) or safe, "size": len(data), "image": ext in IMAGES}


def prune(now: float | None = None) -> int:
    cut = (now or time.time()) - KEEP_DAYS * 86400
    gone = 0
    for root, _dirs, files in os.walk(DIR):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                if os.path.getmtime(p) < cut:
                    os.remove(p)
                    gone += 1
            except OSError:
                pass
    return gone
