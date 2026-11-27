"""Start files per version, the browser player and server-side saves."""
import io
import json
import re
import time

from app import artwork, play
from conftest import Client
from test_tools import add, gid_of, html, q


def html_game(client, games, folder="Web Game v1", files=None):
    d = games / folder
    d.mkdir(parents=True, exist_ok=True)
    for rel, body in (files or {"index.html": "<!doctype html><html><head><meta charset='utf-8'><title>t</title>"
                                             "</head><body>hi</body></html>", "img/a.png": "png"}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    r = client.post("/scan", {"include": [folder], f"title:{folder}": "Web Game", f"label:{folder}": "1",
                              f"platform:{folder}": "browser"})
    assert r.status_code in (200, 302)
    gid = gid_of(client, "Web Game")
    vid = q(client, "SELECT id FROM versions WHERE game_id=?", gid)[0]["id"]
    return gid, vid


def play_src(client, vid):
    r = client.get(f"/play/{vid}")
    assert r.status_code == 200
    return re.search(r'<iframe id="game" class="player-frame" src="([^"]+)"', html(r)).group(1)


def anon(app):
    return app.test_client()     # no session cookie, like the sandboxed frame


# ------------------------------------------------------------ start files

def test_auto_entry(tmp_path):
    d = tmp_path / "g"
    (d / "www").mkdir(parents=True)
    (d / "www" / "index.html").write_text("x")
    (d / "Game.exe").write_text("x")
    assert play.auto_entry(str(d)) == "www/index.html"          # RPG Maker layout beats the exe
    (d / "index.htm").write_text("x")
    assert play.auto_entry(str(d)) == "index.htm"
    e = tmp_path / "e"
    e.mkdir()
    (e / "story.html").write_text("x")
    assert play.auto_entry(str(e)) == "story.html"              # a lone page
    (e / "other.html").write_text("x")
    assert play.auto_entry(str(e)) is None                      # two pages, no index: ask the user
    x = tmp_path / "x"
    x.mkdir()
    (x / "unins000.exe").write_text("x")
    (x / "Hero.exe").write_text("x")
    assert play.auto_entry(str(x), "Hero") == "Hero.exe"


def test_candidates_order_and_limits(tmp_path):
    d = tmp_path / "g"
    (d / "a" / "b").mkdir(parents=True)
    (d / "node_modules").mkdir()
    for rel in ("Game.exe", "unins000.exe", "a/b/index.html", "readme.txt", "node_modules/x.html", "run.sh"):
        (d / rel).write_text("x")
    assert play.candidates(str(d)) == ["a/b/index.html", "Game.exe", "run.sh", "unins000.exe"]


def test_clean_entry():
    assert play.clean_entry(" a\\b/./c.html ") == "a/b/c.html"
    for bad in ("", "../x.html", "/etc/passwd", "a/../../x", "a\x00b"):
        try:
            play.clean_entry(bad)
        except play.EntryError:
            continue
        raise AssertionError(bad)


def test_game_page_play_button_and_entry(client, dirs):
    _, games = dirs
    gid, vid = html_game(client, games)
    page = html(client.get(f"/games/{gid}"))
    assert f'href="/play/{vid}"' in page and "▶ Play" in page
    assert "index.html" in page and "auto" in page
    assert 'id="saves"' in page and "Nothing saved yet" in page
    exe_gid = add(client, games, "Desk v1", "Desk", files=("Desk.exe",))
    page = html(client.get(f"/games/{exe_gid}"))
    assert "▶ Play" not in page and "Desk.exe" in page and 'id="saves"' not in page


def test_choose_start_file(client, dirs):
    _, games = dirs
    (games / "Web Game v1" / "bin").mkdir(parents=True)
    (games / "Web Game v1" / "bin" / "Web.exe").write_text("x")
    gid, vid = html_game(client, games)
    form = html(client.get(f"/versions/{vid}/edit"))
    assert '<option value="index.html"' in form and '<option value="bin/Web.exe"' in form
    assert "Automatic (index.html)" in form

    def save(entry):
        return client.post(f"/versions/{vid}/edit", {"version": "1", "added": "", "notes": "", "entry": entry})

    assert save("bin/Web.exe").status_code == 302
    assert q(client, "SELECT entry FROM versions WHERE id=?", vid)[0]["entry"] == "bin/Web.exe"
    page = html(client.get(f"/games/{gid}"))
    assert "▶ Play" not in page and "bin/Web.exe" in page        # the user's choice wins over the page
    assert client.get(f"/play/{vid}").status_code == 404
    for bad in ("../Desk/x.exe", "nope.html", "/etc/passwd"):
        assert save(bad).status_code == 400
    assert q(client, "SELECT entry FROM versions WHERE id=?", vid)[0]["entry"] == "bin/Web.exe"
    assert save("").status_code == 302                          # back to automatic
    assert q(client, "SELECT entry FROM versions WHERE id=?", vid)[0]["entry"] is None


def test_missing_chosen_file_falls_back(client, dirs):
    _, games = dirs
    gid, vid = html_game(client, games, files={"index.html": "<p>x</p>", "alt.html": "<p>y</p>"})
    client.post(f"/versions/{vid}/edit", {"version": "1", "entry": "alt.html"})
    assert "alt.html" in play_src(client, vid)
    (games / "Web Game v1" / "alt.html").unlink()
    assert play_src(client, vid).endswith("/index.html")
    assert "no longer in the folder" in html(client.get(f"/versions/{vid}/edit"))


def test_artwork_uses_chosen_page_and_exe(tmp_path):
    d = tmp_path / "g"
    (d / "www" / "icons").mkdir(parents=True)
    (d / "www" / "index.html").write_text('<link rel="icon" href="icons/i.png">')
    (d / "www" / "icons" / "i.png").write_text("x")
    assert artwork.html_favicon(str(d), "www/index.html").endswith("www/icons/i.png")
    assert artwork.html_favicon(str(d)) is None
    assert artwork.html_favicon(str(d), "../x.html") is None


# ------------------------------------------------------------ serving files

def test_player_and_files(client, app, dirs):
    _, games = dirs
    gid, vid = html_game(client, games)
    src = play_src(client, vid)
    assert 'sandbox="allow-scripts' in html(client.get(f"/play/{vid}"))
    c = anon(app)
    r = c.get(src)
    assert r.status_code == 200
    body = html(r)
    assert re.search(r"<meta charset='utf-8'><script src=\"/play/t/[^\"]+/__gamelib__/boot.js\"></script><title>", body)
    assert r.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in r.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in r.headers
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    base = src.rsplit("/", 1)[0]
    r = c.get(base + "/img/a.png")
    assert r.status_code == 200 and r.data == b"png" and r.headers["Content-Type"] == "image/png"
    assert c.get(base + "/").status_code == 200                 # folder → its index.html
    assert c.get(base + "/../Desk/x").status_code == 404
    assert c.get(base + "/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert c.get(base + "/nope.js").status_code == 404
    # a normal page still can't be framed
    assert client.get(f"/games/{gid}").headers["X-Frame-Options"] == "DENY"


def test_bad_and_expired_links(client, app, dirs, monkeypatch):
    _, games = dirs
    _, vid = html_game(client, games)
    src = play_src(client, vid)
    c = anon(app)
    tok = src.split("/")[3]
    forged = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    assert c.get(src.replace(tok, forged)).status_code == 404
    other = re.sub(r"^\d+", str(vid + 1), tok)
    assert c.get(src.replace(tok, other)).status_code == 404     # signature covers the version id
    monkeypatch.setattr(time, "time", lambda: 10 ** 11)
    assert c.get(src).status_code == 404
    assert c.get(f"/play/{vid}").status_code == 302              # the player page itself needs a login


def test_precompressed_files(client, app, dirs):
    _, games = dirs
    _, vid = html_game(client, games, files={"index.html": "<p>x</p>", "Build/game.wasm.br": "BR"})
    base = play_src(client, vid).rsplit("/", 1)[0]
    r = anon(app).get(base + "/Build/game.wasm.br")
    assert r.headers["Content-Encoding"] == "br" and r.headers["Content-Type"] == "application/wasm"


def test_inject_without_head():
    assert play.inject(b"<!DOCTYPE html><p>x", "/b.js") == b'<!DOCTYPE html><script src="/b.js"></script><p>x'
    assert play.inject(b"<p>x", "/b.js") == b'<script src="/b.js"></script><p>x'
    assert play.inject(b"<html><HEAD lang=x><p>", "/b.js") == b'<html><HEAD lang=x><script src="/b.js"></script><p>'


# ------------------------------------------------------------ saves

def boot_saves(c, src):
    js = html(c.get(src.rsplit("/", 1)[0] + "/__gamelib__/boot.js"))
    return json.loads(re.search(r"var data = (.*);\n", js).group(1))


def post_save(c, src, changes, **headers):
    tok = src.split("/")[3]
    return c.post(f"/play/t/{tok}/__gamelib__/save", data=json.dumps(changes),
                  headers={"Content-Type": "text/plain", "Origin": "null", **headers})


def test_saves_round_trip(client, app, dirs):
    _, games = dirs
    gid, vid = html_game(client, games)
    src = play_src(client, vid)
    c = anon(app)
    assert boot_saves(c, src) == {}
    r = post_save(c, src, {"set": {"RPG File1": "abc", "config": "{}"}})
    assert r.status_code == 200 and r.json["ok"] and r.headers["Access-Control-Allow-Origin"] == "*"
    assert boot_saves(c, src) == {"RPG File1": "abc", "config": "{}"}
    post_save(c, src, {"del": ["config"], "set": {"RPG File2": "</script><b>"}})
    js = html(c.get(src.rsplit("/", 1)[0] + "/__gamelib__/boot.js"))
    assert "</script><b>" not in js                             # can't break out of the script
    assert boot_saves(c, src) == {"RPG File1": "abc", "RPG File2": "</script><b>"}
    post_save(c, src, {"clear": True, "set": {"only": "1"}})
    assert boot_saves(c, src) == {"only": "1"}
    page = html(client.get(f"/games/{gid}"))
    assert "1 entry" in page and "Download saves" in page
    # saves belong to the game, so a new version sees them
    (games / "Web Game v2").mkdir()
    (games / "Web Game v2" / "index.html").write_text("<p>2</p>")
    client.post(f"/games/{gid}/versions/new", {"source": "folder", "folder": "Web Game v2", "version": "2"})
    vids = [r["id"] for r in q(client, "SELECT id FROM versions WHERE game_id=? ORDER BY id", gid)]
    assert len(vids) == 2 and boot_saves(c, play_src(client, vids[1])) == {"only": "1"}


def test_bad_saves_rejected(client, app, dirs, monkeypatch):
    _, games = dirs
    gid, vid = html_game(client, games)
    src = play_src(client, vid)
    c = anon(app)
    tok = src.split("/")[3]
    assert c.post(f"/play/t/{tok}/__gamelib__/save", data="not json",
                  headers={"Content-Type": "text/plain"}).status_code == 400
    assert post_save(c, src, {"set": {"a": 1}}).status_code == 400            # values are strings
    assert post_save(c, src, {"set": ["a"]}).status_code == 400
    assert post_save(c, src, {"set": {"k" * 2000: "v"}}).status_code == 400
    assert post_save(c, src.replace(tok, tok[:-1] + "x"), {"set": {"a": "b"}}).status_code == 404
    monkeypatch.setattr(play, "MAX_GAME_SAVES", 50)
    post_save(c, src, {"set": {"a": "1"}})
    r = post_save(c, src, {"set": {"big": "x" * 100}})
    assert r.status_code == 413
    assert boot_saves(c, src) == {"a": "1"}                    # nothing of the rejected request was kept
    assert c.open(f"/play/t/{tok}/__gamelib__/save", method="OPTIONS").status_code == 204


def test_saves_download_restore_delete(client, app, dirs):
    _, games = dirs
    gid, vid = html_game(client, games)
    src = play_src(client, vid)
    post_save(anon(app), src, {"set": {"slot1": "hello"}})
    r = client.get(f"/games/{gid}/saves.json")
    data = json.loads(r.data)
    assert data["format"] == "gamelib-saves" and data["saves"] == {"slot1": "hello"}
    assert "attachment" in r.headers["Content-Disposition"]
    assert client.post(f"/games/{gid}/saves/delete").status_code == 400      # needs confirm
    assert client.post(f"/games/{gid}/saves/delete", {"confirm": "yes"}).status_code == 302
    assert q(client, "SELECT COUNT(*) AS n FROM saves")[0]["n"] == 0
    data["saves"]["slot2"] = "x"
    r = client.post(f"/games/{gid}/saves/import",
                    {"file": (io.BytesIO(json.dumps(data).encode()), "s.json")}, content_type="multipart/form-data")
    assert r.status_code == 302
    assert boot_saves(anon(app), src) == {"slot1": "hello", "slot2": "x"}
    client.post(f"/games/{gid}/saves/import", {"file": (io.BytesIO(b'{"format": "x"}'), "s.json")},
                content_type="multipart/form-data")
    assert boot_saves(anon(app), src) == {"slot1": "hello", "slot2": "x"}    # wrong file: unchanged
    # anonymous requests to the save management pages are refused
    assert anon(app).get(f"/games/{gid}/saves.json").status_code == 302


def test_saves_follow_merge_and_go_with_the_game(client, app, dirs):
    _, games = dirs
    gid, vid = html_game(client, games)
    other = add(client, games, "Web Game Copy", "Web Game Copy", files=("index.html",))
    ovid = q(client, "SELECT id FROM versions WHERE game_id=?", other)[0]["id"]
    post_save(anon(app), play_src(client, ovid), {"set": {"slot": "from copy", "k": "copy"}})
    post_save(anon(app), play_src(client, vid), {"set": {"k": "mine"}})
    client.post(f"/games/{gid}/merge", {"other": other})
    assert boot_saves(anon(app), play_src(client, vid)) == {"k": "mine", "slot": "from copy"}
    client.post(f"/games/{gid}/delete", {"confirm": "yes"})
    assert q(client, "SELECT COUNT(*) AS n FROM saves")[0]["n"] == 0


def test_export_import_keeps_start_file(client, app, dirs, tmp_path):
    _, games = dirs
    gid, vid = html_game(client, games, files={"index.html": "x", "alt.html": "y"})
    client.post(f"/versions/{vid}/edit", {"version": "1", "entry": "alt.html"})
    data = json.loads(client.get("/tools/export").data)
    assert data["games"][0]["versions"][0]["entry"] == "alt.html"
    client.post(f"/versions/{vid}/edit", {"version": "1", "entry": ""})
    client.post("/tools/import", {"file": (io.BytesIO(json.dumps(data).encode()), "e.json")},
                content_type="multipart/form-data")
    assert q(client, "SELECT entry FROM versions WHERE id=?", vid)[0]["entry"] == "alt.html"


def test_cached_files_keep_cors_and_boot_patches_images(client, app, dirs):
    _, games = dirs
    _, vid = html_game(client, games)
    src = play_src(client, vid)
    c = anon(app)
    first = c.get(src.rsplit("/", 1)[0] + "/img/a.png")
    again = c.get(src.rsplit("/", 1)[0] + "/img/a.png", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304 and again.headers["Access-Control-Allow-Origin"] == "*"
    js = html(c.get(src.rsplit("/", 1)[0] + "/__gamelib__/boot.js"))
    assert 'el.crossOrigin = "anonymous"' in js           # canvas/WebGL games aren't tainted
