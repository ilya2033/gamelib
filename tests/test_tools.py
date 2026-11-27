"""1.4.0: personal fields, missing folders, export/import, saved views, bulk
edit, SteamGridDB, duplicates, old versions and storage."""
import io
import json
import shutil
import sqlite3

import pytest
from PIL import Image

from app import create_app, steamgriddb
from conftest import Client


def add(client, games, folder, title=None, label="1", files=("game.exe",)):
    d = games / folder
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x")
    r = client.post("/scan", {"include": [folder], f"title:{folder}": title or folder.split(" v")[0],
                              f"label:{folder}": label, f"platform:{folder}": "windows"})
    assert r.status_code in (200, 302)
    return gid_of(client, title or folder.split(" v")[0])


def q(client, sql, *args):
    con = sqlite3.connect(client.app.config["DB_PATH"])
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def gid_of(client, title):
    return q(client, "SELECT id FROM games WHERE title=? ORDER BY id DESC", title)[0]["id"]


def html(r):
    return r.get_data(as_text=True)


# ---------------------------------------------------------- personal fields

def test_personal_fields_edit_and_quick_actions(client, dirs):
    gid = add(client, dirs[1], "Alpha v1")
    r = client.post(f"/games/{gid}/edit", {"title": "Alpha", "platform": "windows", "personal": "1",
                                           "rating": "4", "hours": "12,5", "favorite": "1", "notes": "Chapter 3"})
    assert r.status_code == 302
    g = q(client, "SELECT * FROM games WHERE id=?", gid)[0]
    assert (g["rating"], g["hours"], g["favorite"], g["notes"]) == (4, 12.5, 1, "Chapter 3")
    client.post(f"/games/{gid}/personal", {"rating": "2"})             # a star click keeps the favourite
    g = q(client, "SELECT * FROM games WHERE id=?", gid)[0]
    assert (g["rating"], g["favorite"]) == (2, 1)
    client.post(f"/games/{gid}/personal", {"favorite": ""})            # un-favourite
    client.post(f"/games/{gid}/personal", {"rating": ""})              # clear rating
    g = q(client, "SELECT * FROM games WHERE id=?", gid)[0]
    assert (g["rating"], g["favorite"], g["hours"]) == (None, 0, 12.5)
    page = html(client.get(f"/games/{gid}"))
    assert "12.5 h played" in page and "Chapter 3" in page
    for bad in ({"rating": "6"}, {"hours": "lots"}, {"hours": "-1"}):
        client.post(f"/games/{gid}/edit", {"title": "Alpha", "platform": "windows", **bad})
    assert q(client, "SELECT hours FROM games WHERE id=?", gid)[0]["hours"] == 12.5


def test_library_filters(client, dirs):
    a = add(client, dirs[1], "Fav v1")
    add(client, dirs[1], "Plain v1")
    client.post(f"/games/{a}/personal", {"favorite": "1", "rating": "5"})
    page = html(client.get("/?fav=1"))
    assert ">Fav<" in page and ">Plain<" not in page
    assert ">Plain<" not in html(client.get("/?rating=4"))
    assert ">Plain<" in html(client.get("/?nocover=1"))
    assert ">Fav<" in html(client.get("/?sort=rating"))


# --------------------------------------------------------- missing folders

def test_missing_folders_marked_relinked_and_forgotten(client, dirs):
    games = dirs[1]
    gid = add(client, games, "Moved v1")
    add(client, games, "Gone v1")
    add(client, games, "Stays v1")
    (games / "Archive").mkdir()
    shutil.move(str(games / "Moved v1"), str(games / "Archive" / "Moved v1"))
    shutil.rmtree(games / "Gone v1")
    client.post("/scan/rescan")                                          # background rescan marks them
    rows = {r["folder"]: r["missing_since"] for r in q(client, "SELECT folder, missing_since FROM versions")}
    assert rows["Moved v1"] and rows["Gone v1"] and rows["Stays v1"] is None
    assert ">Moved<" in html(client.get("/?missing=1")) and ">Stays<" not in html(client.get("/?missing=1"))
    page = html(client.get("/tools/missing"))
    assert 'value="Archive/Moved v1">Archive/Moved v1 (same name)' in page
    vid = q(client, "SELECT id FROM versions WHERE folder='Moved v1'")[0]["id"]
    client.post("/tools/missing/relink", {"vid": vid, "new": "Archive/Moved v1"})
    assert q(client, "SELECT folder, missing_since FROM versions WHERE id=?", vid)[0][:] == ("Archive/Moved v1", None)
    gone = q(client, "SELECT id, game_id FROM versions WHERE folder='Gone v1'")[0]
    assert client.post("/tools/missing/forget", {"vid": gone["id"]}).status_code == 400   # needs confirm
    client.post("/tools/missing/forget", {"vid": gone["id"], "confirm": "yes"})
    assert not q(client, "SELECT 1 FROM versions WHERE id=?", gone["id"])
    assert q(client, "SELECT 1 FROM games WHERE id=?", gone["game_id"])  # the game itself stays
    assert gid


def test_unmounted_share_marks_nothing(client, dirs):
    games = dirs[1]
    add(client, games, "Solo v1")
    shutil.rmtree(games / "Solo v1")                                     # share now empty, like a lost mount
    client.post("/tools/missing/check")
    assert q(client, "SELECT missing_since FROM versions")[0][0] is None


# ---------------------------------------------------------- export/import

def test_export_import_round_trip(client, dirs, tmp_path):
    games = dirs[1]
    gid = add(client, games, "Saga v1")
    client.post(f"/games/{gid}/edit", {"title": "Saga", "platform": "windows", "personal": "1", "rating": "5",
                                       "hours": "40", "favorite": "1", "notes": "Boss", "tags": "rpg, long",
                                       "categories": "Best"})
    client.post(f"/games/{gid}/status", {"status": "completed"})
    data = json.loads(client.get("/tools/export").get_data())
    assert data["format"] == "gamelib-export" and data["games"][0]["rating"] == 5
    assert data["games"][0]["versions"][0]["folder"] == "Saga v1"

    data2 = tmp_path / "data2"                                           # a fresh install on the same share
    data2.mkdir()
    app2 = create_app({"TESTING": True, "DATA_DIR": str(data2), "GAMES_DIR": str(games), "PASSWORD": "secret"})
    c2 = Client(app2).login()
    data["games"].append({"title": "Elsewhere", "versions": [{"folder": "Not Here v1", "label": "1"}]})
    r = c2.post("/tools/import", {"file": (io.BytesIO(json.dumps(data).encode()), "x.json")},
                content_type="multipart/form-data")
    assert r.status_code == 302
    g = q(c2, "SELECT g.*, s.key FROM games g JOIN statuses s ON s.id=g.status_id")
    assert len(g) == 1
    g = g[0]
    assert (g["title"], g["rating"], g["hours"], g["favorite"], g["notes"], g["key"]) == \
        ("Saga", 5, 40, 1, "Boss", "completed")
    assert sorted(r["name"] for r in q(c2, "SELECT name FROM tags")) == ["long", "rpg"]
    assert q(c2, "SELECT folder FROM versions")[0][0] == "Saga v1"
    # importing again updates instead of duplicating
    c2.post("/tools/import", {"file": (io.BytesIO(json.dumps(data).encode()), "x.json")},
            content_type="multipart/form-data")
    assert q(c2, "SELECT COUNT(*) FROM games")[0][0] == 1


def test_import_rejects_other_files(client):
    for body in (b"not json", b'{"format": "something"}', b'{"format": "gamelib-export", "version": 99}'):
        client.post("/tools/import", {"file": (io.BytesIO(body), "x.json")}, content_type="multipart/form-data")
    assert q(client, "SELECT COUNT(*) FROM games")[0][0] == 0


# ------------------------------------------------------------ saved views

def test_saved_views(client, dirs):
    add(client, dirs[1], "Viewed v1")
    r = client.post("/views", {"name": "My RPGs", "query": "status=pending&fav=1&evil=1&sort=rating"})
    assert r.status_code == 302
    assert q(client, "SELECT query FROM saved_views")[0][0] == "status=pending&fav=1&sort=rating"
    page = html(client.get("/?status=pending&fav=1&sort=rating"))
    assert "My RPGs" in page and "side-link active" in page
    vid = q(client, "SELECT id FROM saved_views")[0][0]
    client.post(f"/views/{vid}/delete", {"back": "//evil.example"})
    assert not q(client, "SELECT 1 FROM saved_views")


# --------------------------------------------------------------- bulk edit

def test_bulk_edit(client, dirs):
    ids = [add(client, dirs[1], f"Bulk {i} v1") for i in range(3)]
    form = lambda **kw: {"ids": [str(i) for i in ids[:2]], "back": "/", **kw}  # noqa: E731
    client.post("/games/bulk", form(action="status:completed"))
    assert [r[0] for r in q(client, "SELECT s.key FROM games g JOIN statuses s ON s.id=g.status_id ORDER BY g.id")] \
        == ["completed", "completed", "pending"]
    client.post("/games/bulk", form(action="fav"))
    client.post("/games/bulk", form(action="rating", value="3"))
    client.post("/games/bulk", form(action="tag-add", value="coop, short"))
    client.post("/games/bulk", form(action="tag-remove", value="short"))
    client.post("/games/bulk", form(action="cat-add", value="Weekend"))
    rows = q(client, "SELECT favorite, rating FROM games ORDER BY id")
    assert [tuple(r) for r in rows] == [(1, 3), (1, 3), (0, None)]
    assert [r[0] for r in q(client, "SELECT name FROM tags")] == ["coop"]
    assert q(client, "SELECT COUNT(*) FROM game_categories gc JOIN categories c ON c.id=gc.category_id "
                     "WHERE c.name='Weekend'")[0][0] == 2
    assert client.post("/games/bulk", form(action="explode")).status_code == 400
    client.post("/games/bulk", form(action="rating", value="9"))
    assert q(client, "SELECT rating FROM games WHERE id=?", ids[0])[0][0] == 3


# ------------------------------------------------------------- SteamGridDB

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (60, 90), (200, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def fake_sgdb(monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append((url, req.get_header("Authorization")))
        if "/search/autocomplete/" in url:
            return FakeResp(json.dumps({"success": True, "data": [{"id": 77, "name": "Hollow Thing",
                                                                   "release_date": 1488326400}]}).encode())
        if "/grids/game/77" in url:
            return FakeResp(json.dumps({"success": True, "data": [
                {"url": "https://cdn2.steamgriddb.com/grid/a.png", "thumb": "https://cdn2.steamgriddb.com/thumb/a.png",
                 "width": 600, "height": 900},
                {"url": "https://evil.example/b.png", "thumb": "https://evil.example/b.png"}]}).encode())
        if url.startswith("https://cdn2.steamgriddb.com/"):
            return FakeResp(png_bytes())
        raise AssertionError(url)
    monkeypatch.setattr(steamgriddb.urllib.request, "urlopen", urlopen)
    return calls


def test_steamgriddb_pick_and_job(client, dirs, fake_sgdb):
    gid = add(client, dirs[1], "Hollow v1", files=("readme.txt",))
    assert client.get(f"/games/{gid}/steamgriddb").status_code == 302      # no key yet
    client.post("/settings/integrations", {"steamgriddb_key": "abcdef0123456789abcdef"})
    page = html(client.get(f"/games/{gid}/steamgriddb"))
    assert "Hollow Thing (2017)" in page
    page = html(client.get(f"/games/{gid}/steamgriddb?sid=77"))
    assert "cdn2.steamgriddb.com/thumb/a.png" in page and "evil.example" not in page
    client.post(f"/games/{gid}/steamgriddb", {"url": "https://evil.example/b.png"})
    assert q(client, "SELECT cover FROM games")[0][0] is None              # other hosts refused
    client.post(f"/games/{gid}/steamgriddb", {"url": "https://cdn2.steamgriddb.com/grid/a.png"})
    assert q(client, "SELECT cover FROM games")[0][0].startswith("sgdb-")
    assert all(auth == "Bearer abcdef0123456789abcdef" for url, auth in fake_sgdb if "/api/v2/" in url)
    assert all(auth is None for url, auth in fake_sgdb if "cdn2" in url)  # the key never goes to the CDN

    g2 = add(client, dirs[1], "Other v1", files=("readme.txt",))
    client.post("/tools/steamgriddb", {"scope": "missing"})
    assert q(client, "SELECT cover FROM games WHERE id=?", g2)[0][0].startswith("sgdb-")
    client.post("/artwork/missing", {"scope": "auto"})                    # folder refresh leaves sgdb covers
    assert q(client, "SELECT cover FROM games WHERE id=?", g2)[0][0].startswith("sgdb-")


def test_steamgriddb_bad_key_message(client, dirs, monkeypatch):
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    monkeypatch.setattr(steamgriddb.urllib.request, "urlopen", urlopen)
    gid = add(client, dirs[1], "Keyless v1")
    client.post("/settings/integrations", {"steamgriddb_key": "abcdef0123456789abcdef"})
    assert "refused the API key" in html(client.get(f"/games/{gid}/steamgriddb"))
    client.post("/settings/integrations", {"steamgriddb_key": "short!"})
    assert q(client, "SELECT value FROM settings")[0][0] == "abcdef0123456789abcdef"


# ---------------------------------------------- duplicates and old versions

def test_duplicates_and_merge(client, dirs):
    a = add(client, dirs[1], "The Witcher v1", title="The Witcher")
    b = add(client, dirs[1], "Witcher (GOG) v1", title="Witcher (GOG)")
    client.post(f"/games/{b}/edit", {"title": "Witcher (GOG)", "platform": "windows", "personal": "1",
                                     "hours": "3", "tags": "rpg"})
    assert "Witcher (GOG)" in html(client.get("/tools/duplicates"))
    client.post(f"/games/{a}/merge", {"other": str(b)})
    assert [r[0] for r in q(client, "SELECT id FROM games")] == [a]
    assert sorted(r[0] for r in q(client, "SELECT folder FROM versions WHERE game_id=?", a)) == \
        ["The Witcher v1", "Witcher (GOG) v1"]
    assert q(client, "SELECT hours FROM games")[0][0] == 3
    assert [r[0] for r in q(client, "SELECT name FROM tags")] == ["rpg"]
    assert client.post(f"/games/{a}/merge", {"other": str(a)}).status_code == 400


def test_old_versions_never_delete_newest(client, dirs):
    games = dirs[1]
    gid = add(client, games, "Long v1", title="Long")
    for n in (2, 3):
        (games / f"Long v{n}").mkdir()
        (games / f"Long v{n}" / "game.exe").write_text("x" * 100)
        client.post(f"/games/{gid}/versions/new", {"version": str(n), "source": "folder", "folder": f"Long v{n}"})
    vers = {r["label"]: r["id"] for r in q(client, "SELECT id, label FROM versions")}
    page = html(client.get("/tools/versions"))
    assert f'value="{vers["1"]}"' in page and f'value="{vers["3"]}"' not in page
    client.post("/tools/versions/delete", {"vid": [str(vers["1"]), str(vers["3"])], "confirm": "yes",
                                           "delete_files": "1"})
    assert sorted(r[0] for r in q(client, "SELECT label FROM versions")) == ["2", "3"]
    assert not (games / "Long v1").exists() and (games / "Long v3").exists()


def test_tools_pages_render(client, dirs):
    add(client, dirs[1], "Render v1")
    for url in ("/tools", "/tools/storage", "/tools/duplicates", "/tools/versions", "/tools/missing",
                "/settings/integrations"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Render" in html(client.get("/tools/storage"))
