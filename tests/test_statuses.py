"""1.1.0: game statuses, status history, settings, schema v2 migration."""
import os
import sqlite3

from app import create_app, db as dbmod
from conftest import game_id_from
from test_app import add_game_with_upload


def q(client, sql, *args):
    con = sqlite3.connect(client.app.config["DB_PATH"])
    con.row_factory = sqlite3.Row
    return con.execute(sql, args).fetchall()


def status_of(client, gid):
    return q(client, "SELECT s.key FROM games g JOIN statuses s ON s.id=g.status_id WHERE g.id=?", gid)[0]["key"]


def new_game(client, title, status=None, **kw):
    form = {"title": title, "platform": kw.get("platform", "other"), "source": "none"}
    if status:
        form["status"] = status
    return game_id_from(client.post("/games/new", form))


# ------------------------------------------------------------ migration

def test_v1_database_migrates_to_statuses(dirs):
    data, games = dirs
    path = data / "gamelib.db"
    con = sqlite3.connect(path)
    con.executescript(dbmod.MIGRATIONS[0])
    con.execute("PRAGMA user_version = 1")
    con.execute("INSERT INTO games(title,platform,created_at,updated_at) VALUES ('Old One','windows','2024-01-01T00:00:00+00:00','2024-01-01T00:00:00+00:00')")
    con.commit()
    con.close()

    create_app({"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": "x"})

    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION >= 2
    keys = [r[0] for r in con.execute("SELECT key FROM statuses WHERE builtin=1 ORDER BY sort")]
    assert keys == ["pending", "on_hold", "completed", "abandoned"]
    row = con.execute("SELECT s.key, g.status_changed_at FROM games g JOIN statuses s ON s.id=g.status_id").fetchone()
    assert row == ("pending", "2024-01-01T00:00:00+00:00")
    backups = os.listdir(data / "backups")
    tag = f"-pre-v{dbmod.SCHEMA_VERSION}"
    assert any(tag in b for b in backups)
    # the backup is the untouched v1 database
    b = sqlite3.connect(data / "backups" / [b for b in backups if tag in b][0])
    assert b.execute("PRAGMA user_version").fetchone()[0] == 1


# --------------------------------------------------------------- defaults

def test_new_and_scanned_games_start_pending(client, dirs):
    _, games = dirs
    assert status_of(client, new_game(client, "Manual")) == "pending"
    assert status_of(client, game_id_from(add_game_with_upload(client, title="Uploaded"))) == "pending"
    (games / "Scanned v1").mkdir()
    client.post("/scan", {"count": "1", "include-0": "1", "folder-0": "Scanned v1",
                          "title-0": "Scanned", "label-0": "1", "platform-0": "other"})
    gid = q(client, "SELECT id FROM games WHERE title='Scanned'")[0]["id"]
    assert status_of(client, gid) == "pending"


def test_status_can_be_chosen_when_adding(client):
    assert status_of(client, new_game(client, "Done already", status="completed")) == "completed"
    assert status_of(client, new_game(client, "Bogus", status="nope")) == "pending"


# ---------------------------------------------------- changing + history

def test_change_status_records_history(client):
    gid = new_game(client, "Saga")
    assert client.post(f"/games/{gid}/status", {"status": "on_hold"}).status_code == 302
    client.post(f"/games/{gid}/status", {"status": "on_hold"})        # no-op, no extra history row
    client.post(f"/games/{gid}/status", {"status": "completed"})
    assert status_of(client, gid) == "completed"
    hist = q(client, "SELECT from_label, to_label, source FROM status_history WHERE game_id=? ORDER BY id", gid)
    assert [tuple(h) for h in hist] == [("Pending", "On hold", "user"), ("On hold", "Completed", "user")]
    page = client.get(f"/games/{gid}").get_data(as_text=True)
    assert "Status history" in page and "Completed" in page
    assert client.post(f"/games/{gid}/status", {"status": "nope"}).status_code == 400
    assert client.post("/games/999/status", {"status": "completed"}).status_code == 404


def test_status_change_does_not_touch_updated_sort(client):
    gid = new_game(client, "Stable")
    before = q(client, "SELECT updated_at FROM games WHERE id=?", gid)[0][0]
    client.post(f"/games/{gid}/status", {"status": "abandoned"})
    assert q(client, "SELECT updated_at FROM games WHERE id=?", gid)[0][0] == before


# ----------------------------------------------------- filter / sort / UI

def test_filter_counts_and_sort_by_status(client):
    a = new_game(client, "Alpha", status="abandoned")
    new_game(client, "Bravo", status="completed")
    new_game(client, "Charlie")
    new_game(client, "Delta", status="on_hold", platform="windows")
    titles = lambda html: [t for t in ("Alpha", "Bravo", "Charlie", "Delta") if f'title="{t}"' in html]

    assert titles(client.get("/?status=completed").get_data(as_text=True)) == ["Bravo"]
    assert titles(client.get("/?status=on_hold&platform=windows").get_data(as_text=True)) == ["Delta"]
    assert titles(client.get("/?status=on_hold&platform=linux").get_data(as_text=True)) == []
    html = client.get("/?sort=status").get_data(as_text=True)
    order = sorted(["Alpha", "Bravo", "Charlie", "Delta"], key=lambda t: html.index(f'title="{t}"'))
    assert order == ["Charlie", "Delta", "Bravo", "Alpha"]          # pending, on hold, completed, abandoned
    client.post(f"/games/{a}/status", {"status": "pending"})
    html = client.get("/?sort=status_changed").get_data(as_text=True)
    assert html.index('title="Alpha"') < html.index('title="Bravo"')
    # sidebar lists every status with its count, and the card shows a status pip
    html = client.get("/").get_data(as_text=True)
    assert 'Status: Pending' in html and 'href="/?status=abandoned"' in html


def test_search_keeps_status_filter(client):
    new_game(client, "Keep")
    html = client.get("/?status=pending").get_data(as_text=True)
    assert '<input type="hidden" name="status" value="pending">' in html


# ------------------------------------------------------------- settings

def test_rename_and_recolor_builtin(client):
    sts = {r["key"]: r for r in q(client, "SELECT * FROM statuses")}
    form = {"action": "save"}
    for s in sts.values():
        form.update({f"label-{s['id']}": s["label"], f"color-{s['id']}": s["color"], f"sort-{s['id']}": s["sort"]})
    pid = sts["pending"]["id"]
    form.update({f"label-{pid}": "Backlog", f"color-{pid}": "#123ABC"})
    assert client.post("/settings/statuses", form).status_code == 302
    row = q(client, "SELECT label, color, key FROM statuses WHERE id=?", pid)[0]
    assert tuple(row) == ("Backlog", "#123abc", "pending")            # key stays stable
    form[f"color-{pid}"] = "red; background:url(x)"
    assert client.post("/settings/statuses", form).status_code == 400
    form[f"color-{pid}"], form[f"label-{pid}"] = "#123abc", "   "
    assert client.post("/settings/statuses", form).status_code == 400


def test_custom_status_add_use_delete(client):
    assert client.post("/settings/statuses", {"action": "add", "label": "Playing", "color": "#6aa6f8"}).status_code == 302
    assert client.post("/settings/statuses", {"action": "add", "label": "playing", "color": "#6aa6f8"}).status_code == 400
    st = q(client, "SELECT * FROM statuses WHERE label='Playing'")[0]
    assert st["key"] == "playing" and st["builtin"] == 0
    gid = new_game(client, "Current")
    client.post(f"/games/{gid}/status", {"status": "playing"})
    assert status_of(client, gid) == "playing"

    assert client.post(f"/settings/statuses/{st['id']}/delete", {}).status_code == 400  # needs confirm
    assert client.post(f"/settings/statuses/{st['id']}/delete", {"confirm": "yes"}).status_code == 302
    assert status_of(client, gid) == "pending"
    last = q(client, "SELECT from_label, to_label, source FROM status_history WHERE game_id=? ORDER BY id DESC", gid)[0]
    assert tuple(last) == ("Playing", "Pending", "system")
    assert "Playing" in client.get(f"/games/{gid}").get_data(as_text=True)  # history still readable


def test_builtin_status_cannot_be_deleted(client):
    pid = q(client, "SELECT id FROM statuses WHERE key='completed'")[0]["id"]
    assert client.post(f"/settings/statuses/{pid}/delete", {"confirm": "yes"}).status_code == 400
    assert q(client, "SELECT COUNT(*) FROM statuses WHERE key='completed'")[0][0] == 1


def test_status_label_is_escaped(client):
    client.post("/settings/statuses", {"action": "add", "label": "<script>x</script>", "color": "#000000"})
    html = client.get("/").get_data(as_text=True) + client.get("/settings/statuses").get_data(as_text=True)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_messages_use_renamed_pending_label(client):
    pid = q(client, "SELECT id FROM statuses WHERE key='pending'")[0]["id"]
    sts = q(client, "SELECT * FROM statuses")
    form = {"action": "save"}
    for s in sts:
        form.update({f"label-{s['id']}": s["label"], f"color-{s['id']}": s["color"], f"sort-{s['id']}": s["sort"]})
    form[f"label-{pid}"] = "Backlog"
    client.post("/settings/statuses", form)
    client.post("/settings/statuses", {"action": "add", "label": "Playing", "color": "#6aa6f8"})
    page = client.get("/settings/statuses").get_data(as_text=True)
    assert "moved back to Backlog" in page and "back to Pending" not in page
    gid = new_game(client, "G")
    client.post(f"/games/{gid}/status", {"status": "playing"})
    sid = q(client, "SELECT id FROM statuses WHERE key='playing'")[0]["id"]
    r = client.post(f"/settings/statuses/{sid}/delete", {"confirm": "yes"}, follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "1 game moved to Backlog" in text and "Pending" not in text.split("Statuses")[0][-200:]
