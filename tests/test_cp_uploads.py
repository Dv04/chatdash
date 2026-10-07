"""Dashboard uploads: the saved file, the allowlist, the size cap, the route."""
import io
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhi_orbit.cp import mount, uploads  # noqa: E402


@pytest.fixture
def updir(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "DIR", str(tmp_path / "up"))
    monkeypatch.setattr(mount, "_ATTACHED", object())   # route() only serves /api/cp/ once a server is attached
    return tmp_path / "up"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes: Windows protects the upload folder with the user profile ACL")
def test_save_writes_a_private_file_and_returns_its_path(updir):
    code, out = uploads.save("My Screen Shot (1).PNG", b"\x89PNG data")
    assert code == 200 and out["image"] is True and out["size"] == 9
    assert os.path.isabs(out["path"]) and out["path"].startswith(str(updir))
    assert out["path"].endswith("-My_Screen_Shot_1.png")
    assert open(out["path"], "rb").read() == b"\x89PNG data"
    assert stat.S_IMODE(os.stat(out["path"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(out["path"])).st_mode) == 0o700


def test_save_refuses_traversal_types_empty_and_big(updir, monkeypatch):
    code, out = uploads.save("../../../etc/passwd.txt", b"x")
    assert code == 200 and out["path"].startswith(str(updir)) and ".." not in out["path"]
    assert uploads.save("run.sh", b"x")[0] == 415
    assert uploads.save("noext", b"x")[0] == 415
    assert uploads.save("a.pdf", b"")[0] == 400
    monkeypatch.setattr(uploads, "MAX_BYTES", 10)
    assert uploads.save("a.pdf", b"x" * 11)[0] == 413


def test_two_uploads_of_the_same_name_do_not_collide(updir):
    a = uploads.save("a.txt", b"1")[1]["path"]
    b = uploads.save("a.txt", b"2")[1]["path"]
    assert a != b and open(a, "rb").read() == b"1" and open(b, "rb").read() == b"2"


def test_prune_removes_only_old_files(updir):
    old = uploads.save("old.txt", b"o")[1]["path"]
    new = uploads.save("new.txt", b"n")[1]["path"]
    t = time.time() - (uploads.KEEP_DAYS + 1) * 86400
    os.utime(old, (t, t))
    assert uploads.prune() == 1 and not os.path.exists(old) and os.path.exists(new)


class FakeHandler:
    def __init__(self, path, headers, body):
        self.path, self.headers, self.rfile = path, headers, io.BytesIO(body)
        self.sent = None
        self.close_connection = False

    def _send(self, code, obj, ctype=None):
        self.sent = (code, obj)


def test_route_saves_raw_body_and_decodes_the_filename(updir):
    body = b"%PDF-1.4 fake"
    h = FakeHandler("/api/cp/uploads", {"Content-Length": str(len(body)), "X-Filename": "Q3%20report.pdf"}, body)
    assert mount.route(h, "POST", "tok") is True
    code, out = h.sent
    assert code == 200 and out["path"].endswith("-Q3_report.pdf") and open(out["path"], "rb").read() == body


def test_route_refuses_an_oversize_body_without_reading_it(updir, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_BYTES", 5)
    h = FakeHandler("/api/cp/uploads", {"Content-Length": "6", "X-Filename": "a.pdf"}, b"123456")
    assert mount.route(h, "POST", "tok") is True
    assert h.sent[0] == 413 and h.rfile.tell() == 0 and h.close_connection is True
