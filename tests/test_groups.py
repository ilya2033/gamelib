"""1.2.0: subfolders as groups/categories, folder rules, relink, destinations."""
import os
import re
import sqlite3

import pytest

from app import create_app, db as dbmod, storage
from conftest import game_id_from, make_zip


def q(client, sql, *args):
    con = sqlite3.connect(client.app.config["DB_PATH"])
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def mk(games, rel, files=("game.exe",)):
    d = games / rel
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x")
    return d


def scan_import(client, rows):
    """rows: [(folder, title, label)] -> import them all."""
    form = {"count": str(len(rows))}
    for i, (folder, title, label) in enumerate(rows):
        form.update({f"include-{i}": "1", f"folder-{i}": folder, f"title-{i}": title,
                     f"label-{i}": label, f"platform-{i}": "windows"})
    r = client.post("/scan", form)
    assert r.status_code == 302, r.get_data(as_text=True)[:500]


def location_of(client, title):
    rows = q(client, "SELECT c.path, c.is_group, gc.source FROM games g JOIN game_categories gc ON gc.game_id=g.id "
                     "JOIN categories c ON c.id=gc.category_id WHERE g.title=? ORDER BY c.is_group DESC, c.path", title)
    return [(r["path"], r["is_group"], r["source"]) for r in rows]


@pytest.fixture
def share(dirs):
    _, games = dirs
    mk(games, "PC/Strategy/Starfall Tactics v1.2")
    mk(games, "PC/Strategy/Starfall Tactics v1.3.1")
    mk(games, "PC/Cave Explorer v2.0", ("cave.x86_64",))
    mk(games, "Browser/RPG Maker/Moonlit Text RPG", ("index.html",))
    mk(games, "Pixel Racer v0.9", ("index.html",))
    (games / "PC" / "folder.jpg").write_bytes(b"\xff\xd8\xff")      # ignored file: PC stays a group
    mk(games, "PC/@eaDir/junk")                                      # NAS metadata, skipped
    mk(games, "#recycle/Old v1")
    return games


# ------------------------------------------------------------ the walker

def test_walk_classifies_groups_and_games(share):
    out = dict((p, k) for k, p in storage.walk_share(str(share), depth=3))
    assert out["PC"] == "group" and out["PC/Strategy"] == "group" and out["Browser/RPG Maker"] == "group"
    for p in ("PC/Strategy/Starfall Tactics v1.2", "PC/Cave Explorer v2.0",
              "Browser/RPG Maker/Moonlit Text RPG", "Pixel Racer v0.9"):
        assert out[p] == "game", p
    assert not any("@eaDir" in p or "#recycle" in p for p in out)


def test_walk_rules_and_depth(share):
    mk(share, "Retro v2/Pac Clone v1")                               # version-like name but really a group
    out = dict((p, k) for k, p in storage.walk_share(str(share), depth=3))
    assert out["Retro v2"] == "game" and "Retro v2/Pac Clone v1" not in out
    out = dict((p, k) for k, p in storage.walk_share(str(share), {"Retro v2": "group"}, depth=3))
    assert out["Retro v2"] == "group" and out["Retro v2/Pac Clone v1"] == "game"
    mk(share, "A/B/C/Deep v1")
    out = dict((p, k) for k, p in storage.walk_share(str(share), depth=3))
    assert out["A/B/C"] == "game" and "A/B/C/Deep v1" not in out     # level 3 is always a game


def test_walk_ignores_symlinked_folders(share, tmp_path):
    outside = tmp_path / "outside"
    mk(outside, "Secret v1")
    os.symlink(outside, share / "Linked")
    assert not any(p.startswith("Linked") for _, p in storage.walk_share(str(share)))


def test_location():
    assert storage.location("PC/Strategy/Tactics/Game v1") == ("PC", "PC/Strategy/Tactics")
    assert storage.location("PC/Game v1") == ("PC", None)
    assert storage.location("Game v1") == (None, None)


# ------------------------------------------------------- scan + library

def test_scan_lists_nested_folders_with_location(client, share):
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="PC/Strategy/Starfall Tactics v1.2"' in html
    assert "PC › Strategy" in html and "top level (ungrouped)" in html
    assert "Group folders" in html and "@eaDir" not in html


def test_import_assigns_group_and_category(client, share):
    scan_import(client, [("PC/Strategy/Starfall Tactics v1.2", "Starfall Tactics", "1.2"),
                         ("PC/Strategy/Starfall Tactics v1.3.1", "Starfall Tactics", "1.3.1"),
                         ("PC/Cave Explorer v2.0", "Cave Explorer", "2.0"),
                         ("Browser/RPG Maker/Moonlit Text RPG", "Moonlit Text RPG", "1"),
                         ("Pixel Racer v0.9", "Pixel Racer", "0.9")])
    assert location_of(client, "Starfall Tactics") == [("PC", 1, "folder"), ("PC/Strategy", 0, "folder")]
    assert location_of(client, "Cave Explorer") == [("PC", 1, "folder")]
    assert location_of(client, "Moonlit Text RPG") == [("Browser", 1, "folder"), ("Browser/RPG Maker", 0, "folder")]
    assert location_of(client, "Pixel Racer") == []

    pc = q(client, "SELECT id FROM categories WHERE path='PC'")[0]["id"]
    strat = q(client, "SELECT id FROM categories WHERE path='PC/Strategy'")[0]["id"]
    titles = lambda html: sorted(t for t in ("Starfall Tactics", "Cave Explorer", "Moonlit Text RPG", "Pixel Racer")
                                 if f'title="{t}"' in html)
    assert titles(client.get(f"/?group={pc}").get_data(as_text=True)) == ["Cave Explorer", "Starfall Tactics"]
    assert titles(client.get(f"/?category={strat}").get_data(as_text=True)) == ["Starfall Tactics"]
    assert titles(client.get(f"/?category={pc}").get_data(as_text=True)) == ["Cave Explorer", "Starfall Tactics"]
    assert titles(client.get("/?group=none").get_data(as_text=True)) == ["Pixel Racer"]

    html = client.get("/").get_data(as_text=True)
    assert 'class="group-tabs"' in html and "Ungrouped" in html and ">PC <" in html.replace("\n", "")
    page = client.get(f"/games/{q(client, 'SELECT id FROM games WHERE title=?', 'Starfall Tactics')[0]['id']}")
    assert "📁 PC" in page.get_data(as_text=True) and "Strategy" in page.get_data(as_text=True)
    # group icon from folder.jpg
    assert client.get(f"/groups/{pc}/icon").status_code == 200
    assert client.get(f"/groups/{strat}/icon").status_code == 404


def test_group_follows_latest_version_and_prunes(client, share):
    mk(share, "Browser/Starfall Tactics v2.0")
    scan_import(client, [("PC/Strategy/Starfall Tactics v1.2", "Starfall Tactics", "1.2")])
    gid = q(client, "SELECT id FROM games")[0]["id"]
    client.post(f"/games/{gid}/versions/new", {"version": "2.0", "source": "folder",
                                               "folder": "Browser/Starfall Tactics v2.0"})
    assert location_of(client, "Starfall Tactics") == [("Browser", 1, "folder")]
    # PC and PC/Strategy no longer have games -> pruned
    assert q(client, "SELECT COUNT(*) FROM categories WHERE path LIKE 'PC%'")[0][0] == 0
    vid = q(client, "SELECT id FROM versions WHERE label='2.0'")[0]["id"]
    client.post(f"/versions/{vid}/delete", {"confirm": "yes"})
    assert location_of(client, "Starfall Tactics") == [("PC", 1, "folder"), ("PC/Strategy", 0, "folder")]


def test_folder_rules_change_detection(client, share):
    mk(share, "Retro v2/Pac Clone v1")
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="Retro v2"' in html and "Retro v2/Pac Clone v1" not in html
    assert client.post("/scan/rule", {"rule": "group:Retro v2"}).status_code == 302
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="Retro v2/Pac Clone v1"' in html
    assert client.post("/scan/rule", {"rule": "game:PC/Strategy"}).status_code == 302
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="PC/Strategy"' in html and "PC/Strategy/Starfall" not in html
    client.post("/scan/rule", {"rule": "auto:PC/Strategy"})
    assert "PC/Strategy/Starfall" in client.get("/scan").get_data(as_text=True)
    for bad in ("group:../etc", "game:/etc", "nope:PC", "group:Does Not Exist"):
        assert client.post("/scan/rule", {"rule": bad}).status_code == 400


def test_relink_moved_folder(client, share):
    scan_import(client, [("PC/Cave Explorer v2.0", "Cave Explorer", "2.0")])
    gid = q(client, "SELECT id FROM games")[0]["id"]
    client.post(f"/games/{gid}/status", {"status": "completed"})
    (share / "Linux").mkdir()
    os.rename(share / "PC/Cave Explorer v2.0", share / "Linux/Cave Explorer v2.0")
    html = client.get("/scan").get_data(as_text=True)
    assert "Moved folders" in html and "Linux/Cave Explorer v2.0" in html
    vid = q(client, "SELECT id FROM versions")[0]["id"]
    assert client.post("/scan/relink", {"vid": vid, "new": "../outside"}).status_code == 400
    assert client.post("/scan/relink", {"vid": vid, "new": "Linux/Cave Explorer v2.0"}).status_code == 302
    assert q(client, "SELECT folder FROM versions")[0]["folder"] == "Linux/Cave Explorer v2.0"
    assert location_of(client, "Cave Explorer") == [("Linux", 1, "folder")]
    assert q(client, "SELECT s.key FROM games g JOIN statuses s ON s.id=g.status_id")[0]["key"] == "completed"
    assert "Moved folders" not in client.get("/scan").get_data(as_text=True)


# ----------------------------------------------------- upload destination

def upload(client, body=None, name="g.zip"):
    r = client.upload(name, body or make_zip({"Top/index.html": b"<h1>x</h1>"}))
    return r.json["id"]


def test_upload_into_existing_group_and_new_subfolder(client, share):
    r = client.post("/games/new", {"title": "Nebula", "platform": "html", "version": "0.3", "source": "upload",
                                   "upload_id": upload(client), "dest": "Browser", "dest_new": "Shooters"})
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    assert (share / "Browser/Shooters/Nebula v0.3/index.html").exists()
    assert location_of(client, "Nebula") == [("Browser", 1, "folder"), ("Browser/Shooters", 0, "folder")]
    # the created folder is remembered as a group, even with a version-like name
    assert q(client, "SELECT kind FROM folder_rules WHERE path='Browser/Shooters'")[0]["kind"] == "group"
    gid = game_id_from(r)
    # next version defaults to the same place
    assert 'value="Browser/Shooters" selected' in client.get(f"/games/{gid}/versions/new").get_data(as_text=True)


def test_upload_new_top_level_group(client, share):
    r = client.post("/games/new", {"title": "Proto", "platform": "html", "version": "1", "source": "upload",
                                   "upload_id": upload(client), "dest": "", "dest_new": "Prototypes v2"})
    assert r.status_code == 302
    assert (share / "Prototypes v2/Proto v1").is_dir()
    assert location_of(client, "Proto") == [("Prototypes v2", 1, "folder")]
    assert "Prototypes v2/Proto v1" not in client.get("/scan").get_data(as_text=True)  # tracked, not offered


@pytest.mark.parametrize("dest, dest_new", [
    ("../", ""), ("/etc", ""), ("Nope", ""),                 # not a known group folder
    ("PC", "a/b/c"),                                           # deeper than SCAN_DEPTH would find
])
def test_upload_destination_rejected(client, share, dest, dest_new):
    r = client.post("/games/new", {"title": "Bad", "platform": "html", "version": "1", "source": "upload",
                                   "upload_id": upload(client), "dest": dest, "dest_new": dest_new})
    assert r.status_code == 400
    assert not list(share.glob("**/Bad v1"))


def test_upload_dest_new_is_sanitised(client, share):
    r = client.post("/games/new", {"title": "Safe", "platform": "html", "version": "1", "source": "upload",
                                   "upload_id": upload(client), "dest": "", "dest_new": "../../evil"})
    assert r.status_code == 302
    assert (share / "evil/Safe v1").is_dir() and not (share.parent / "evil").exists()


def test_upload_refuses_symlinked_destination(client, share, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, share / "PC" / "Link")
    r = client.post("/games/new", {"title": "Esc", "platform": "html", "version": "1", "source": "upload",
                                   "upload_id": upload(client), "dest": "PC", "dest_new": "Link"})
    assert r.status_code == 400
    assert os.listdir(outside) == []


# --------------------------------------------------------- manual categories

def test_manual_categories(client, share):
    r = client.post("/games/new", {"title": "Party", "platform": "other", "source": "none",
                                   "categories": "Couch co-op, Favourites, couch CO-OP"})
    gid = game_id_from(r)
    rows = location_of(client, "Party")
    assert [p for p, *_ in rows] == [None, None] and all(s == "user" for *_, s in rows)
    fav = q(client, "SELECT id FROM categories WHERE name='Favourites'")[0]["id"]
    assert 'title="Party"' in client.get(f"/?category={fav}").get_data(as_text=True)
    assert "★ Favourites" in client.get(f"/games/{gid}").get_data(as_text=True)
    client.post(f"/games/{gid}/edit", {"title": "Party", "platform": "other", "categories": "Favourites"})
    assert q(client, "SELECT COUNT(*) FROM categories WHERE path IS NULL")[0][0] == 1   # unused one pruned


# ----------------------------------------------------------- migration

def test_v2_database_migrates_to_v3(dirs):
    data, games = dirs
    path = data / "gamelib.db"
    con = sqlite3.connect(path)
    for m in dbmod.MIGRATIONS[:2]:
        con.executescript(m)
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    create_app({"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": "x"})
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    assert con.execute("SELECT name FROM sqlite_master WHERE name='folder_rules'").fetchone()
    assert any("-pre-v3" in f for f in os.listdir(data / "backups"))


def test_layout_cache_is_invalidated_by_writes(client, share):
    client.get("/")                                   # warm the cache
    mk(share, "New One v1")
    # scan always walks fresh
    assert 'value="New One v1"' in client.get("/scan").get_data(as_text=True)
    scan_import(client, [("New One v1", "New One", "1")])
    # badge count comes from the (invalidated) cache: the imported folder is gone from it
    html = client.get("/").get_data(as_text=True)
    assert "New One v1" not in client.get("/scan").get_data(as_text=True)
    assert 'title="New One"' in html


def test_moved_folder_is_not_offered_or_imported_as_new(client, share):
    scan_import(client, [("Pixel Racer v0.9", "Pixel Racer", "0.9")])
    (share / "Browser").mkdir(exist_ok=True)
    os.rename(share / "Pixel Racer v0.9", share / "Browser/Pixel Racer v0.9")
    html = client.get("/scan").get_data(as_text=True)
    assert "Moved folders" in html
    assert 'name="new" value="Browser/Pixel Racer v0.9"' in html          # offered as a relink
    assert not re.search(r'name="folder-\d+" value="Browser/Pixel Racer v0\.9"', html)  # not as an import row
    # a forged import of the moved folder is ignored
    scan_import(client, [("Browser/Pixel Racer v0.9", "Pixel Racer", "0.9")])
    assert q(client, "SELECT COUNT(*) FROM versions")[0][0] == 1
    assert "Moved folders" in client.get("/scan").get_data(as_text=True)


def test_group_icon_only_requested_when_present(client, share):
    scan_import(client, [("PC/Cave Explorer v2.0", "Cave Explorer", "2.0"),
                         ("Browser/RPG Maker/Moonlit Text RPG", "Moonlit Text RPG", "1")])
    html = client.get("/").get_data(as_text=True)
    pc = q(client, "SELECT id FROM categories WHERE path='PC'")[0]["id"]
    br = q(client, "SELECT id FROM categories WHERE path='Browser'")[0]["id"]
    assert f"/groups/{pc}/icon" in html and f"/groups/{br}/icon" not in html


def test_category_selection_highlights_its_group_tab(client, share):
    scan_import(client, [("PC/Strategy/Starfall Tactics v1.2", "Starfall Tactics", "1.2")])
    strat = q(client, "SELECT id FROM categories WHERE path='PC/Strategy'")[0]["id"]
    pc = q(client, "SELECT id FROM categories WHERE path='PC'")[0]["id"]
    html = client.get(f"/?category={strat}").get_data(as_text=True)
    tabs = html.split('class="group-tabs"')[1].split("</nav>")[0]
    assert f'class="gtab active" href="/?group={pc}"' in tabs
    assert 'class="gtab " href="/"' in tabs or 'class="gtab" href="/"' in tabs
