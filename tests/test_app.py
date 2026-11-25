"""Integration tests: auth, CSRF/origin, upload, versioning, scan, download, migrations."""
import io
import os
import sqlite3
import zipfile

import pytest
from PIL import Image

from app import create_app, db as dbmod
from conftest import Client, game_id_from, make_zip


def png_bytes(color=(200, 50, 50)):
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), color).save(buf, "PNG")
    return buf.getvalue()


def add_game_with_upload(client, title="Test Quest", version="1.0", filename="game.zip", body=None, **extra):
    body = body if body is not None else make_zip({"TestQuest/index.html": b"<h1>hi</h1>", "TestQuest/js/a.js": b"1"})
    r = client.upload(filename, body)
    assert r.status_code == 200, r.json
    form = {"title": title, "platform": "html", "version": version, "source": "upload",
            "upload_id": r.json["id"], "tags": "rpg, Text, rpg", "description": "A test."}
    form.update(extra)
    return client.post("/games/new", form, content_type="multipart/form-data")


# ------------------------------------------------------------ auth & csrf

def test_requires_login(app):
    c = Client(app)
    assert c.get("/").status_code == 302
    assert c.get("/healthz").status_code == 200
    assert c.post("/api/uploads", {"body": b"x"}, raw=True).status_code in (401, 403)


def test_login_wrong_password_and_throttle(app):
    c = Client(app)
    c.get("/login")
    for _ in range(5):
        assert c.post("/login", {"username": "admin", "password": "nope"}).status_code == 401
    assert c.post("/login", {"username": "admin", "password": "secret"}).status_code == 429


def test_cross_origin_writes_rejected(client):
    # Another origin, including another port on the same host
    for origin in ("http://evil.example", "http://localhost:8081"):
        r = client.post("/games/new", {"title": "X", "source": "none"}, origin=origin)
        assert r.status_code == 403
    # no Origin / Referer at all
    assert client.post("/games/new", {"title": "X", "source": "none"}, origin=None).status_code == 403
    # same origin but no CSRF token
    assert client.post("/games/new", {"title": "X", "source": "none"}, csrf=False).status_code == 403
    # upload endpoint too
    r = client.post("/api/uploads", {"body": b"x"}, raw=True, origin="http://evil.example",
                    headers={"X-Filename": "a.zip"})
    assert r.status_code == 403


def test_trusted_origin_allows_reverse_proxy(dirs):
    data, games = dirs
    app = create_app({"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games),
                      "PASSWORD": "secret", "TRUSTED_ORIGINS": ["games.example.com"]})
    c = Client(app).login()
    r = c.post("/games/new", {"title": "Proxy", "source": "none", "platform": "other"},
               origin="https://games.example.com")
    assert r.status_code == 302


# ------------------------------------------------------------- upload

def test_upload_zip_creates_folder_and_game(client, dirs):
    _, games = dirs
    r = add_game_with_upload(client, cover=(io.BytesIO(png_bytes()), "logo.png"))
    assert r.status_code == 302
    gid = game_id_from(r)
    folder = games / "Test Quest v1.0"
    assert (folder / "index.html").read_bytes() == b"<h1>hi</h1>"  # top-level dir stripped
    assert (folder / "js" / "a.js").exists()
    page = client.get(f"/games/{gid}").get_data(as_text=True)
    assert "Test Quest" in page and "v1.0" in page and "#rpg" in page and "#Text" in page
    assert page.count("#rpg") == 1  # tags de-duplicated
    # cover stored as webp
    with sqlite3.connect(client.app.config["DB_PATH"]) as con:
        cover = con.execute("SELECT cover FROM games").fetchone()[0]
    assert cover.endswith(".webp") and client.get(f"/covers/{cover}").status_code == 200
    # temp files were cleaned up
    assert os.listdir(client.app.config["TMP_DIR"]) == []
    assert not any(n.startswith(".gamelib-staging") for n in os.listdir(games))


def test_upload_single_html(client, dirs):
    _, games = dirs
    r = add_game_with_upload(client, title="Tiny RPG", filename="story.html", body=b"<html>story</html>")
    assert r.status_code == 302
    assert (games / "Tiny RPG v1.0" / "story.html").read_bytes() == b"<html>story</html>"
    # Downloading a single-file version returns the file itself
    vid = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT id FROM versions").fetchone()[0]
    d = client.get(f"/versions/{vid}/download")
    assert d.status_code == 200 and d.data == b"<html>story</html>"


def test_upload_rejects_wrong_extension(client):
    r = client.upload("game.exe", b"MZ")
    assert r.status_code == 400 and "zip" in r.json["error"]


def test_upload_large_streamed_in_chunks(client):
    body = os.urandom(3 * 1024 * 1024 + 17)
    r = client.upload("big.html", body)
    assert r.status_code == 200 and r.json["size"] == len(body)
    data_path = os.path.join(client.app.config["TMP_DIR"], r.json["id"] + ".upload")
    with open(data_path, "rb") as f:
        assert f.read() == body


def test_unsafe_zip_upload_rejected(client, dirs):
    _, games = dirs
    body = make_zip({"ok.html": b"1", "../../escape.txt": b"pwned"})
    r = add_game_with_upload(client, title="Evil", body=body)
    assert r.status_code == 400
    assert "Rejected zip" in r.get_data(as_text=True)
    assert os.listdir(games) == []  # staging cleaned, nothing installed
    assert not (games.parent / "escape.txt").exists()
    assert sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0


def test_folder_name_collision_gets_suffix(client, dirs):
    _, games = dirs
    (games / "Test Quest v1.0").mkdir()
    (games / "Test Quest v1.0" / "keep.txt").write_text("mine")
    assert add_game_with_upload(client).status_code == 302
    assert (games / "Test Quest v1.0" / "keep.txt").read_text() == "mine"
    assert (games / "Test Quest v1.0 (2)" / "index.html").exists()


def test_validation_error_keeps_upload(client):
    r = client.upload("game.zip", make_zip({"index.html": b"x"}))
    uid = r.json["id"]
    r = client.post("/games/new", {"title": "", "version": "1", "source": "upload", "upload_id": uid})
    assert r.status_code == 400
    assert uid in r.get_data(as_text=True)  # still offered, no re-upload needed


# ---------------------------------------------------------- versioning

def test_versions_history_and_latest(client, dirs):
    _, games = dirs
    gid = game_id_from(add_game_with_upload(client, version="1.0"))
    r = client.upload("v2.zip", make_zip({"index.html": b"v2"}))
    r = client.post(f"/games/{gid}/versions/new",
                    {"version": "2.0", "source": "upload", "upload_id": r.json["id"], "notes": "Big update"})
    assert r.status_code == 302
    assert (games / "Test Quest v1.0" / "index.html").exists()  # old version kept
    assert (games / "Test Quest v2.0" / "index.html").read_bytes() == b"v2"
    page = client.get(f"/games/{gid}").get_data(as_text=True)
    assert "Big update" in page
    assert page.index("v2.0") < page.index("v1.0")
    assert "Latest <b>v2.0</b>" in page
    assert "v2.0" in client.get("/").get_data(as_text=True)


def test_version_from_existing_folder_and_delete(client, dirs):
    _, games = dirs
    gid = game_id_from(add_game_with_upload(client))
    (games / "Test Quest v1.1").mkdir()
    (games / "Test Quest v1.1" / "game.exe").write_bytes(b"MZ")
    r = client.post(f"/games/{gid}/versions/new", {"version": "1.1", "source": "folder", "folder": "Test Quest v1.1"})
    assert r.status_code == 302
    con = sqlite3.connect(client.app.config["DB_PATH"])
    vid = con.execute("SELECT id FROM versions WHERE label='1.1'").fetchone()[0]
    # deleting needs confirmation
    assert client.post(f"/versions/{vid}/delete", {}).status_code == 400
    # library-only delete keeps the folder
    assert client.post(f"/versions/{vid}/delete", {"confirm": "yes"}).status_code == 302
    assert (games / "Test Quest v1.1").exists()
    # re-add, then delete with files
    client.post(f"/games/{gid}/versions/new", {"version": "1.1", "source": "folder", "folder": "Test Quest v1.1"})
    vid = con.execute("SELECT id FROM versions WHERE label='1.1'").fetchone()[0]
    client.post(f"/versions/{vid}/delete", {"confirm": "yes", "delete_files": "1"})
    assert not (games / "Test Quest v1.1").exists()


def test_folder_source_rejects_escape(client, dirs):
    gid = game_id_from(add_game_with_upload(client))
    for bad in ("../", "../data", "/etc", "Test Quest v1.0"):  # last: already tracked
        r = client.post(f"/games/{gid}/versions/new", {"version": "9", "source": "folder", "folder": bad})
        assert r.status_code == 400


def test_edit_game_and_version(client):
    gid = game_id_from(add_game_with_upload(client))
    r = client.post(f"/games/{gid}/edit", {"title": "Renamed", "platform": "windows", "tags": "a, b",
                                           "description": "New", "cover": (io.BytesIO(png_bytes()), "c.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    page = client.get(f"/games/{gid}").get_data(as_text=True)
    assert "Renamed" in page and "Windows" in page and "#a" in page and "#rpg" not in page
    vid = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT id FROM versions").fetchone()[0]
    client.post(f"/versions/{vid}/edit", {"version": "1.0-final", "notes": "gold", "added": "2024-01-02"})
    page = client.get(f"/games/{gid}").get_data(as_text=True)
    assert "v1.0-final" in page and "2024-01-02" in page
    # bad image rejected
    r = client.post(f"/games/{gid}/edit", {"title": "R", "platform": "html",
                                           "cover": (io.BytesIO(b"not an image"), "x.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_delete_game(client, dirs):
    _, games = dirs
    gid = game_id_from(add_game_with_upload(client))
    assert client.post(f"/games/{gid}/delete", {}).status_code == 400
    assert client.post(f"/games/{gid}/delete", {"confirm": "yes"}).status_code == 302
    assert client.get(f"/games/{gid}").status_code == 404
    assert (games / "Test Quest v1.0").exists()  # files kept by default
    gid = game_id_from(add_game_with_upload(client, title="Second"))
    client.post(f"/games/{gid}/delete", {"confirm": "yes", "delete_files": "1"})
    assert not (games / "Second v1.0").exists()


# ------------------------------------------------------------------ scan

def test_scan_suggests_and_imports(client, dirs):
    _, games = dirs
    for name, f in (("Hollow Knight v1.4", "hollow_knight.exe"), ("Hollow Knight v1.5.78", "hollow_knight.exe"),
                    ("Text Adventure", "index.html"), (".hidden", "x")):
        (games / name).mkdir()
        (games / name / f).write_text("x")
    Image.new("RGB", (10, 10)).save(games / "Hollow Knight v1.5.78" / "cover.png")
    page = client.get("/scan").get_data(as_text=True)
    assert "Hollow Knight v1.4" in page and "Text Adventure" in page and ".hidden" not in page
    assert 'value="1.5.78"' in page and 'value="windows" selected' in page

    form = {"count": "3",
            "include-0": "1", "folder-0": "Hollow Knight v1.4", "title-0": "Hollow Knight", "label-0": "1.4", "platform-0": "windows",
            "include-1": "1", "folder-1": "Hollow Knight v1.5.78", "title-1": "Hollow Knight", "label-1": "1.5.78", "platform-1": "windows",
            "folder-2": "Text Adventure", "title-2": "Text Adventure", "label-2": "1", "platform-2": "html"}
    assert client.post("/scan", form).status_code == 302
    con = sqlite3.connect(client.app.config["DB_PATH"])
    assert con.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 2
    assert con.execute("SELECT cover FROM games").fetchone()[0]  # cover.png picked up
    # not-ticked folder still offered, imported ones gone
    page = client.get("/scan").get_data(as_text=True)
    assert "Text Adventure" in page and "Hollow Knight v1.4" not in page
    # a new version folder matches the existing game
    (games / "Hollow Knight v1.6").mkdir()
    assert "existing game" in client.get("/scan").get_data(as_text=True)


# ------------------------------------------------- library list & filters

def test_search_filter_sort(client):
    add_game_with_upload(client, title="Alpha", tags="puzzle")
    add_game_with_upload(client, title="beta", platform="windows", tags="rpg")
    client.post("/games/new", {"title": "Gamma_100%", "platform": "linux", "source": "none"})
    names = lambda html: [n for n in ("Alpha", "beta", "Gamma_100%") if f'title="{n}"' in html]
    assert names(client.get("/?q=alp").get_data(as_text=True)) == ["Alpha"]
    assert names(client.get("/?q=rpg").get_data(as_text=True)) == ["beta"]  # tag search
    assert names(client.get("/?q=100%25").get_data(as_text=True)) == ["Gamma_100%"]  # LIKE escaped
    assert names(client.get("/?q=_").get_data(as_text=True)) == ["Gamma_100%"]
    assert names(client.get("/?platform=windows").get_data(as_text=True)) == ["beta"]
    assert names(client.get("/?tag=PUZZLE").get_data(as_text=True)) == ["Alpha"]
    html = client.get("/?sort=name&dir=desc").get_data(as_text=True)
    assert html.index('title="Gamma_100%"') < html.index('title="beta"') < html.index('title="Alpha"')
    html = client.get("/?sort=added").get_data(as_text=True)
    assert html.index('title="Gamma_100%"') < html.index('title="Alpha"')


# --------------------------------------------------------------- download

def test_download_streams_zip(client):
    gid = game_id_from(add_game_with_upload(client))
    vid = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT id FROM versions").fetchone()[0]
    r = client.get(f"/versions/{vid}/download")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    assert "Test%20Quest%20v1.0.zip" in r.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        assert zf.read("Test Quest v1.0/index.html") == b"<h1>hi</h1>"
    assert gid


def test_missing_folder_marked(client, dirs):
    import shutil
    _, games = dirs
    gid = game_id_from(add_game_with_upload(client))
    shutil.rmtree(games / "Test Quest v1.0")
    assert "missing" in client.get(f"/games/{gid}").get_data(as_text=True)
    vid = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT id FROM versions").fetchone()[0]
    assert client.get(f"/versions/{vid}/download").status_code == 404


# -------------------------------------------------------- migrations

def test_migration_backup_and_downgrade_guard(dirs, monkeypatch):
    data, games = dirs
    cfg = {"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": "secret"}
    create_app(cfg)
    dbp = data / "gamelib.db"
    assert sqlite3.connect(dbp).execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    # Simulate an app update that adds a migration: a backup is taken first.
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS + ["ALTER TABLE games ADD COLUMN extra TEXT"])
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", len(dbmod.MIGRATIONS))
    create_app(cfg)
    assert any(f.endswith(".db") for f in os.listdir(data / "backups"))
    # Running an older image against newer data refuses to start.
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", 1)
    with pytest.raises(RuntimeError, match="newer"):
        create_app(cfg)


def test_password_required(dirs):
    data, games = dirs
    with pytest.raises(RuntimeError, match="GAMELIB_PASSWORD"):
        create_app({"DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": ""})
