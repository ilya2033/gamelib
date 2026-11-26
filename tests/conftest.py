import io
import os
import re
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

ORIGIN = "http://localhost"


@pytest.fixture(autouse=True)
def reset_throttle():
    from app import auth
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture
def dirs(tmp_path):
    data, games = tmp_path / "data", tmp_path / "games"
    data.mkdir()
    games.mkdir()
    return data, games


@pytest.fixture
def app(dirs):
    data, games = dirs
    return create_app({
        "TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games),
        "USERNAME": "admin", "PASSWORD": "secret", "JOBS_SYNC": True, "SCAN_RESULT_MAX_AGE": 0,
    })


class Client:
    """Test client that sends a same-origin Origin header and the CSRF token."""

    def __init__(self, app):
        self.c = app.test_client()
        self.app = app

    def token(self):
        with self.c.session_transaction() as s:
            return s.get("csrf", "")

    def get(self, *a, **kw):
        return self.c.get(*a, **kw)

    def post(self, url, data=None, origin=ORIGIN, csrf=True, headers=None, **kw):
        h = {"Origin": origin} if origin else {}
        h.update(headers or {})
        data = dict(data or {})
        if csrf and not kw.get("raw"):
            data.setdefault("csrf", self.token())
        if kw.pop("raw", False):
            h.setdefault("X-CSRF-Token", self.token() if csrf else "")
            return self.c.post(url, data=data.get("body", b""), headers=h, **kw)
        r = self.c.post(url, data=data, headers=h, **kw)
        # Like a browser: follow the redirect to a background job's page. Jobs
        # run inline in tests, so that page redirects to the result (or shows
        # the error, with status 200).
        if r.status_code == 302 and "/jobs/" in r.headers.get("Location", ""):
            r = self.c.get(r.headers["Location"])
        return r

    def login(self):
        self.get("/login")
        r = self.post("/login", {"username": "admin", "password": "secret"})
        assert r.status_code == 302, r.data
        return self

    def upload(self, filename, body):
        r = self.post("/api/uploads", {"body": body}, raw=True,
                      headers={"X-Filename": filename, "Content-Type": "application/octet-stream"})
        return r


@pytest.fixture
def client(app):
    return Client(app).login()


def make_zip(files, symlinks=()):
    """files: {name: bytes}. symlinks: [(name, target)]"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            if name.endswith("/"):
                zf.writestr(zipfile.ZipInfo(name), b"")
            else:
                zf.writestr(name, content)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, target)
    return buf.getvalue()


def game_id_from(resp):
    m = re.search(r"/games/(\d+)", resp.headers["Location"])
    return int(m.group(1))
