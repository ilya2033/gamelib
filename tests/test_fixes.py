"""Regression tests for the 1.0.1 validation findings."""
import io
import os
import sqlite3
import stat
import threading
import zipfile

import pytest

from app import create_app, db as dbmod, storage
from app.views import version_label
from conftest import Client, game_id_from, make_zip
from test_app import add_game_with_upload


class ExplodingStream(io.BytesIO):
    """Fails the test if anything tries to read the request body."""

    def read(self, *a):
        raise AssertionError("request body was read before the request was rejected")

    readline = readinto = read1 = read


def vid_of(client, label=None, folder=None):
    con = sqlite3.connect(client.app.config["DB_PATH"])
    if folder:
        return con.execute("SELECT id FROM versions WHERE folder=?", (folder,)).fetchone()[0]
    if label:
        return con.execute("SELECT id FROM versions WHERE label=?", (label,)).fetchone()[0]
    return con.execute("SELECT id FROM versions ORDER BY id").fetchone()[0]


# ------------------------------------------------------------ security

def test_delete_files_refuses_swapped_in_symlink(client, dirs):
    _, games = dirs
    add_game_with_upload(client, title="Victim")
    add_game_with_upload(client, title="Target")
    victim = games / "Victim v1.0"
    import shutil
    shutil.rmtree(victim)
    os.symlink(games / "Target v1.0", victim)       # swap folder for a link
    vid = vid_of(client, folder="Victim v1.0")
    r = client.post(f"/versions/{vid}/delete", {"confirm": "yes", "delete_files": "1"})
    assert r.status_code == 302
    assert (games / "Target v1.0" / "index.html").exists()  # other game untouched


def test_unauthenticated_write_rejected_before_body(app):
    c = Client(app)
    c.get("/login")
    # ExplodingStream fails the test if the body is read at all.
    for url in ("/games/new", "/scan", "/api/uploads"):
        for length, expected in (("1000", (401,)), ("400000000", (401, 413))):
            r = c.c.post(url, headers={"Origin": "http://localhost", "Content-Type": "multipart/form-data; boundary=x"},
                         input_stream=ExplodingStream(), environ_overrides={"CONTENT_LENGTH": length})
            assert r.status_code in expected, (url, length, r.status_code)
    assert os.listdir(app.config["TMP_DIR"]) == []


def test_chunked_body_rejected(client):
    r = client.c.post("/games/new", headers={"Origin": "http://localhost", "Transfer-Encoding": "chunked",
                                             "Content-Type": "application/x-www-form-urlencoded"},
                      input_stream=ExplodingStream())
    assert r.status_code == 411


def test_login_body_size_capped(app):
    c = Client(app)
    c.get("/login")
    r = c.c.post("/login", headers={"Origin": "http://localhost", "Content-Type": "application/x-www-form-urlencoded"},
                 input_stream=ExplodingStream(), environ_overrides={"CONTENT_LENGTH": str(10 * 1024 * 1024)})
    assert r.status_code == 413


@pytest.mark.parametrize("nxt", ["/\t/evil.example", "//evil.example", "/\\evil.example",
                                 "https://evil.example", "/\t\t//evil.example", " /x", "/ok\n/x", "/\x00/evil"])
def test_login_next_cannot_leave_site(app, nxt):
    c = Client(app)
    c.get("/login")
    r = c.c.post("/login", query_string={"next": nxt}, headers={"Origin": "http://localhost"},
                 data={"username": "admin", "password": "secret", "csrf": c.token()})
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/"), r.headers["Location"]


def test_login_next_keeps_local_path(app):
    c = Client(app)
    c.get("/login")
    r = c.c.post("/login", query_string={"next": "/games/new?x=1"}, headers={"Origin": "http://localhost"},
                 data={"username": "admin", "password": "secret", "csrf": c.token()})
    assert r.headers["Location"].endswith("/games/new?x=1")


def test_throttle_holds_under_parallel_attempts(app):
    clients = []
    for _ in range(30):
        c = Client(app)
        c.get("/login")
        clients.append(c)
    codes, barrier = [], threading.Barrier(len(clients))

    def attempt(c):
        barrier.wait()
        codes.append(c.post("/login", {"username": "admin", "password": "wrong"}).status_code)

    threads = [threading.Thread(target=attempt, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(401) <= 5 and codes.count(429) >= 25


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_non_ascii_inputs_do_not_crash(app):
    c = Client(app)
    c.get("/login")
    assert c.post("/login", {"username": "адмін", "password": "x"}).status_code == 401
    c = Client(app).login()
    r = c.c.post("/api/uploads", data=b"x", headers={"Origin": "http://localhost", "X-CSRF-Token": "токен",
                                                     "X-Filename": "a.zip"})
    assert r.status_code == 403


def test_scan_form_garbage_is_400_not_500(client, dirs):
    _, games = dirs
    (games / "Dup v1").mkdir()
    assert client.post("/scan", {"count": "abc"}).status_code == 400
    form = {"count": "2", "include-0": "1", "folder-0": "Dup v1", "title-0": "Dup", "label-0": "1", "platform-0": "other",
            "include-1": "1", "folder-1": "Dup v1", "title-1": "Dup", "label-1": "1", "platform-1": "other"}
    assert client.post("/scan", form).status_code == 302
    assert sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1


def _bad_crc_zip():
    data = bytearray(make_zip({"a.txt": b"hello world hello world"}))
    i = data.index(b"hello world")
    data[i] ^= 0xFF
    return bytes(data)


@pytest.mark.parametrize("body", [
    make_zip({"x": b"file", "x/y": b"clash"}),          # file vs directory
    _bad_crc_zip(),                                       # corrupt data
    make_zip({("n" * 300) + ".txt": b"too long"}),       # name too long for the filesystem
])
def test_broken_zips_are_rejected_cleanly(client, dirs, body):
    _, games = dirs
    r = add_game_with_upload(client, title="Broken", body=body)
    assert "Rejected zip" in r.get_data(as_text=True)
    assert os.listdir(games) == []


def test_download_with_control_chars_in_folder_name(client, dirs):
    _, games = dirs
    gid = game_id_from(add_game_with_upload(client, title="Plain"))
    bad = "Evil\r\nSet-Cookie: x=1 v1"
    (games / bad).mkdir()
    (games / bad / "a.txt").write_text("a")
    (games / bad / "b.txt").write_text("b")
    client.post(f"/games/{gid}/versions/new", {"version": "2", "source": "folder", "folder": bad})
    r = client.get(f"/versions/{vid_of(client, '2')}/download")
    assert r.status_code == 200
    assert "\n" not in r.headers["Content-Disposition"] and "\r" not in r.headers["Content-Disposition"]
    assert not any("x=1" in c for c in r.headers.getlist("Set-Cookie"))
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        assert all("\n" not in n and "\r" not in n for n in zf.namelist())


def test_api_errors_are_json(app):
    c = Client(app)
    r = c.c.post("/api/uploads", data=b"x", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.is_json and "Cross-origin" in r.json["error"]


# --------------------------------------------------------- data safety

def test_manual_backups_never_rotate_out_pre_migration(dirs, monkeypatch):
    data, games = dirs
    cfg = {"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": "secret"}
    create_app(cfg)
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS + ["ALTER TABLE games ADD COLUMN extra TEXT"])
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", len(dbmod.MIGRATIONS))
    create_app(cfg)
    con = dbmod.connect(str(data / "gamelib.db"))
    for _ in range(dbmod.KEEP_MANUAL + 5):
        dbmod.backup(con, str(data / "backups"), "manual")
    con.close()
    files = os.listdir(data / "backups")
    assert sum("-pre-v" in f for f in files) == 1
    assert sum(f.endswith("-manual.db") for f in files) == dbmod.KEEP_MANUAL


def test_unzip_keeps_exec_bits(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("Game/run_me")
        info.create_system = 3
        info.external_attr = (0o100755 << 16)
        zf.writestr(info, b"#!/bin/sh\n")
        zf.writestr("Game/game.x86_64", b"ELF")      # no unix mode (e.g. made on Windows)
        zf.writestr("Game/readme.txt", b"hi")
    src = tmp_path / "g.zip"
    src.write_bytes(buf.getvalue())
    dest = tmp_path / "out"
    dest.mkdir()
    storage.safe_extract(str(src), str(dest), 10**6)
    is_exec = lambda p: bool(os.stat(dest / p).st_mode & stat.S_IXUSR)
    assert is_exec("run_me") and is_exec("game.x86_64") and not is_exec("readme.txt")


# ------------------------------------------------------------------- UI

@pytest.mark.parametrize("label, shown", [
    ("1.2", "v1.2"), ("1.0-final", "v1.0-final"), ("0.9b", "v0.9b"), ("2", "v2"),
    ("2026-11-25", "2026-11-25"), ("beta", "beta"), ("Early Access", "Early Access"),
])
def test_version_label(label, shown):
    assert version_label(label) == shown


def test_footer_shows_version(client):
    from app.version import __version__
    assert f"GameLib v{__version__}" in client.get("/").get_data(as_text=True)
