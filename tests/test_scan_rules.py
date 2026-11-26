"""1.3.0: in-place Scan rule changes (JSON API, reclassify job, fragments, undo)."""
import sqlite3

from app import create_app, storage
from conftest import ORIGIN, Client


def mk(games, rel, files=("game.exe",)):
    d = games / rel
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x")
    return d


def api(c, url, payload=None, csrf=True):
    headers = {"Origin": ORIGIN}
    if csrf:
        headers["X-CSRF-Token"] = c.token()
    return c.c.post(url, json=payload or {}, headers=headers)


def rules(c, kind, paths):
    r = api(c, "/api/scan/rules", {"rules": [{"path": p, "kind": kind} for p in paths]})
    assert r.status_code == 200, r.get_data(as_text=True)
    frag = c.get(f"/scan/fragment/{r.json['job']}")
    assert frag.status_code == 200
    return frag.json


def q(app, sql, *args):
    con = sqlite3.connect(app.config["DB_PATH"])
    return con.execute(sql, args).fetchall()


def test_toggle_returns_only_the_changed_subtree(client, dirs, monkeypatch):
    _, games = dirs
    mk(games, "Retro v2/Pac Clone v1")
    mk(games, "Retro v2/Snake v3")
    for i in range(20):
        mk(games, f"Other/Game {i} v1")
    client.get("/scan")                                    # discovery
    walks = []
    real = storage.walk_share
    monkeypatch.setattr(storage, "walk_share", lambda *a, **k: walks.append(k.get("start")) or real(*a, **k))
    frag = rules(client, "group", ["Retro v2"])
    assert frag["roots"] == ["Retro v2"] and frag["removed"] == ["Retro v2"]
    assert sorted(r["folder"] for r in frag["rows"]) == ["Retro v2/Pac Clone v1", "Retro v2/Snake v3"]
    assert all(r["section"] == "Retro v2" and 'name="include" value="Retro v2/' in r["html"] for r in frag["rows"])
    assert [g["path"] for g in frag["group_rows"]] == ["Retro v2"]
    assert walks == ["Retro v2"]                           # only that subtree was walked
    assert frag["can_undo"] is True


def test_patched_result_is_what_a_reload_shows(dirs):
    _, games = dirs
    mk(games, "Retro v2/Pac Clone v1")
    app = create_app({"TESTING": True, "DATA_DIR": str(dirs[0]), "GAMES_DIR": str(games), "PASSWORD": "secret",
                      "SCAN_RESULT_MAX_AGE": 3600})        # reloads reuse the saved discovery
    c = Client(app).login()
    assert 'value="Retro v2"' in c.get("/scan").get_data(as_text=True)
    rules(c, "group", ["Retro v2"])
    html = c.get("/scan").get_data(as_text=True)
    assert 'value="Retro v2/Pac Clone v1"' in html and 'name="include" value="Retro v2"' not in html
    assert q(app, "SELECT COUNT(*) FROM jobs WHERE kind='discover'")[0][0] == 1   # no second full scan


def test_bulk_rules_are_one_request_and_one_undo_batch(client, dirs):
    _, games = dirs
    for i in range(200):
        mk(games, f"Packs/Pack {i:03d}/Inner v1")          # 200 group folders
    client.get("/scan")
    paths = [f"Packs/Pack {i:03d}" for i in range(200)]
    frag = rules(client, "game", paths)
    assert len(frag["rows"]) == 200 and all("Inner" not in r["folder"] for r in frag["rows"])
    batches = q(client.app, "SELECT COUNT(DISTINCT batch), COUNT(*) FROM folder_rule_history")[0]
    assert batches == (1, 200)
    r = api(client, "/api/scan/rules/undo")
    assert r.status_code == 200 and r.json["undone"] == 200
    frag = client.get(f"/scan/fragment/{r.json['job']}").json
    assert len(frag["rows"]) == 200 and all(r["folder"].endswith("/Inner v1") for r in frag["rows"])
    assert q(client.app, "SELECT COUNT(*) FROM folder_rules")[0][0] == 0
    assert api(client, "/api/scan/rules/undo").status_code == 400          # nothing left to undo


def test_games_inside_covers_folders_added_later(client, dirs):
    _, games = dirs
    mk(games, "HTML/Collection/Inner v1", ("index.html",))
    client.get("/scan")
    frag = rules(client, "games-inside", ["HTML"])
    assert [r["folder"] for r in frag["rows"]] == ["HTML/Collection"]
    (games / "HTML" / "Brand New").mkdir()                 # no files, no version: would be a group
    assert 'name="include" value="HTML/Brand New"' in client.get("/scan").get_data(as_text=True)


def test_suspects_are_flagged(client, dirs):
    _, games = dirs
    coll = mk(games, "Collection", ("readme.txt",))        # has a file -> detected as a game
    mk(coll, "Alpha v1")
    mk(coll, "Beta v2")
    mk(games, "Normal v1")
    html = client.get("/scan").get_data(as_text=True)
    assert html.count(">probably a group</span>") == 1 and "Fix all suspects (1)" in html
    frag = rules(client, "group", ["Collection"])
    assert sorted(r["folder"] for r in frag["rows"]) == ["Collection/Alpha v1", "Collection/Beta v2"]


def test_keyed_import_form(client, dirs):
    _, games = dirs
    mk(games, "PC/Keyed v1")
    r = client.post("/scan", {"include": ["PC/Keyed v1"], "title:PC/Keyed v1": "Keyed", "label:PC/Keyed v1": "1",
                              "platform:PC/Keyed v1": "windows"})
    assert r.status_code == 302
    assert q(client.app, "SELECT g.title, v.folder FROM games g JOIN versions v ON v.game_id=g.id") == \
        [("Keyed", "PC/Keyed v1")]


def test_rules_api_validation(client, dirs):
    _, games = dirs
    mk(games, "Tracked v1")
    client.post("/scan", {"include": ["Tracked v1"], "title:Tracked v1": "T", "label:Tracked v1": "1",
                          "platform:Tracked v1": "windows"})
    for payload in ({"rules": [{"path": "Tracked v1", "kind": "group"}]},     # tracked version
                    {"rules": [{"path": "../etc", "kind": "group"}]},
                    {"rules": [{"path": "Nope", "kind": "group"}]},
                    {"rules": [{"path": "Tracked v1", "kind": "explode"}]},
                    {"rules": []}, {}):
        r = api(client, "/api/scan/rules", payload)
        assert r.status_code == 400 and r.is_json, payload
    assert api(client, "/api/scan/rules", {"rules": [{"path": "x", "kind": "group"}]}, csrf=False).status_code == 403
    assert client.get("/scan/fragment/nope").status_code == 404


def test_undo_label_and_new_version_preselect(client, dirs):
    _, games = dirs
    mk(games, "Packs/Pack 1/Inner v1")
    mk(games, "Packs/Pack 2/Inner v1")
    client.get("/scan")
    rules(client, "game", ["Packs/Pack 1", "Packs/Pack 2"])
    assert "Last detection change: 2 folders → game" in client.get("/scan").get_data(as_text=True)
    mk(games, "Hero v1.0")
    mk(games, "Hero v1.2")
    mk(games, "Hero v1.10")
    mk(games, "Zed v9")
    client.post("/scan", {"include": ["Hero v1.0"], "title:Hero v1.0": "Hero", "label:Hero v1.0": "1.0",
                          "platform:Hero v1.0": "windows"})
    gid = q(client.app, "SELECT id FROM games WHERE title='Hero'")[0][0]
    html = client.get(f"/games/{gid}/versions/new").get_data(as_text=True)
    assert '<option value="Hero v1.10" selected>' in html             # newest matching version folder
