"""Hand-picked version order; the top version is the game's default."""
import io
import json
import re

from test_tools import add, html, q


def game_with_versions(client, games, labels=("1", "2", "3"), title="Orbit"):
    gid = None
    for lab in labels:
        d = games / f"{title} v{lab}"
        d.mkdir()
        (d / "index.html").write_text(f"<p>{lab}</p>")
        if gid is None:
            gid = add(client, games, f"{title} v{lab}", title, label=lab, files=("index.html",))
        else:
            client.post(f"/games/{gid}/versions/new", {"source": "folder", "folder": f"{title} v{lab}", "version": lab})
    vids = {r["label"]: r["id"] for r in q(client, "SELECT id, label FROM versions WHERE game_id=?", gid)}
    return gid, vids


def shown_order(client, gid):
    return re.findall(r'data-vid="(\d+)"', html(client.get(f"/games/{gid}")))


def save(client, gid, ids, json_=True):
    return client.post(f"/games/{gid}/versions/order", {"order": ",".join(map(str, ids))},
                       headers={"Accept": "application/json"} if json_ else None)


def test_default_is_newest_until_reordered(client, dirs):
    _, games = dirs
    gid, v = game_with_versions(client, games)
    assert shown_order(client, gid) == [str(v["3"]), str(v["2"]), str(v["1"])]
    page = html(client.get(f"/games/{gid}"))
    assert 'class="badge ok">latest<' in page and "Drag ⠿ to reorder" in page and "Newest first" not in page
    assert f'href="/play/{v["3"]}"' in page.split('class="detail-body"')[0]


def test_reorder_changes_the_default_everywhere(client, dirs):
    _, games = dirs
    gid, v = game_with_versions(client, games)
    r = save(client, gid, [v["1"], v["3"], v["2"]])
    assert r.status_code == 200 and r.json == {"ok": True}
    assert shown_order(client, gid) == [str(v["1"]), str(v["3"]), str(v["2"])]
    page = html(client.get(f"/games/{gid}"))
    head = page.split('class="detail-body"')[0]
    assert 'class="badge ok">default<' in page and "Default <b>v1</b>" in head
    assert f'href="/play/{v["1"]}"' in head and f'/versions/{v["1"]}/download' in head and "Download v1" in head
    assert "Newest first" in page
    lib = html(client.get("/"))
    assert re.search(r"Orbit.*?v1", lib, re.S)
    assert client.get("/api/suggest?q=orbit").json["games"][0]["latest"] == "v1"
    old = html(client.get("/tools/versions"))
    assert "Kept (default): v1" in old
    # removing old versions never offers the default one
    client.post("/tools/versions/delete", {"confirm": "yes", "vid": [str(x) for x in v.values()]})
    assert [r["label"] for r in q(client, "SELECT label FROM versions WHERE game_id=?", gid)] == ["1"]


def test_new_version_goes_on_top_and_reset(client, dirs):
    _, games = dirs
    gid, v = game_with_versions(client, games)
    save(client, gid, [v["1"], v["3"], v["2"]])
    (games / "Orbit v4").mkdir()
    (games / "Orbit v4" / "index.html").write_text("4")
    client.post(f"/games/{gid}/versions/new", {"source": "folder", "folder": "Orbit v4", "version": "4"})
    v4 = q(client, "SELECT id FROM versions WHERE label='4'")[0]["id"]
    assert shown_order(client, gid) == [str(v4), str(v["1"]), str(v["3"]), str(v["2"])]
    r = client.post(f"/games/{gid}/versions/order", {"reset": "1"})
    assert r.status_code == 302
    assert shown_order(client, gid) == [str(v4), str(v["3"]), str(v["2"]), str(v["1"])]
    assert "Newest first" not in html(client.get(f"/games/{gid}"))


def test_bad_orders_rejected(client, dirs):
    _, games = dirs
    gid, v = game_with_versions(client, games)
    other, ov = game_with_versions(client, games, ("1",), title="Other")
    for ids in ([v["1"], v["2"]], [v["1"], v["2"], v["3"], v["3"]], [v["1"], v["2"], ov["1"]], []):
        r = save(client, gid, ids)
        assert r.status_code == 409 and "Reload" in r.json["error"]
    assert q(client, "SELECT COUNT(*) AS n FROM versions WHERE position IS NOT NULL")[0]["n"] == 0
    assert client.post(f"/games/{gid}/versions/order", {"order": "1"}, csrf=False).status_code == 403
    assert save(client, 999, [1]).status_code == 404


def test_single_version_has_no_handle(client, dirs):
    _, games = dirs
    gid, _ = game_with_versions(client, games, ("1",))
    page = html(client.get(f"/games/{gid}"))
    assert "data-sortable" not in page and 'class="drag"' not in page


def test_export_import_keeps_order(client, dirs):
    _, games = dirs
    gid, v = game_with_versions(client, games)
    save(client, gid, [v["2"], v["1"], v["3"]])
    data = json.loads(client.get("/tools/export").data)
    client.post(f"/games/{gid}/versions/order", {"reset": "1"})
    client.post("/tools/import", {"file": (io.BytesIO(json.dumps(data).encode()), "e.json")},
                content_type="multipart/form-data")
    assert shown_order(client, gid) == [str(v["2"]), str(v["1"]), str(v["3"])]
