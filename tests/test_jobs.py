"""1.3.0: background workers and jobs."""
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import pytest

from app import create_app, jobs, storage
from conftest import Client, make_zip


def cfg(dirs, **extra):
    data, games = dirs
    c = {"TESTING": True, "DATA_DIR": str(data), "GAMES_DIR": str(games), "PASSWORD": "secret",
         "SCAN_RESULT_MAX_AGE": 0}
    c.update(extra)
    return c


def mk(games, rel, files=("game.exe",)):
    d = games / rel
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x" * 100)
    return d


def q(app, sql, *args):
    con = sqlite3.connect(app.config["DB_PATH"])
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def wait_job(c, job_id, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json
        if not j["active"]:
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} still running")


def import_form(rows):
    form = {"count": str(len(rows))}
    for i, (folder, title, label) in enumerate(rows):
        form.update({f"include-{i}": "1", f"folder-{i}": folder, f"title-{i}": title,
                     f"label-{i}": label, f"platform-{i}": "windows"})
    return form


# ------------------------------------------------------------ real threads

def test_scan_and_import_run_in_background_threads(dirs):
    _, games = dirs
    for i in range(5):
        mk(games, f"PC/Game {i} v1.0")
    app = create_app(cfg(dirs, JOBS_SYNC=False))
    c = Client(app).login()
    html = c.get("/scan").get_data(as_text=True)          # returns at once, discovery runs in the background
    if "data-job-poll" in html:
        job_id = html.split('data-job-poll="')[1].split('"')[0]
        assert wait_job(c, job_id)["state"] == "done"
        html = c.get("/scan").get_data(as_text=True)
    assert 'value="PC/Game 3 v1.0"' in html
    r = c.c.post("/scan", data={**import_form([(f"PC/Game {i} v1.0", f"Game {i}", "1.0") for i in range(5)]),
                                "csrf": c.token()}, headers={"Origin": "http://localhost"})
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    job_id = r.headers["Location"].rsplit("/", 1)[1]
    assert wait_job(c, job_id)["state"] == "done"
    assert q(app, "SELECT COUNT(*) FROM versions")[0][0] == 5
    assert all(r["size_bytes"] == 100 for r in q(app, "SELECT size_bytes FROM versions"))
    # finished job page shows the message once and goes to the library
    r = c.get(f"/jobs/{job_id}")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert "Imported 5 version(s), 5 new game(s)." in c.get("/").get_data(as_text=True)


def test_requests_never_walk_the_share(dirs, monkeypatch):
    _, games = dirs
    mk(games, "Solo v1")
    app = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    c = Client(app).login()
    request_thread = threading.current_thread()
    walks = []
    real = storage.walk_share

    def spy(*a, **kw):
        walks.append(threading.current_thread())
        return real(*a, **kw)
    monkeypatch.setattr(storage, "walk_share", spy)
    for url in ("/", "/games/new", "/scan", "/jobs", "/settings/statuses"):
        assert c.get(url).status_code == 200, url
    gid = int(c.c.post("/games/new", data={"title": "T", "platform": "other", "source": "none",
                                           "csrf": c.token()}, headers={"Origin": "http://localhost"})
              .headers["Location"].rsplit("/", 1)[1])
    c.get(f"/games/{gid}/versions/new")
    r = c.c.post(f"/games/{gid}/versions/new", data={"version": "1", "source": "folder", "folder": "Solo v1",
                                                     "csrf": c.token()}, headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    time.sleep(0.3)                                       # let the background layout refresh finish
    assert request_thread not in walks


# -------------------------------------------------------------- cancelling

def test_cancel_queued_job(dirs):
    app = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    with app.app_context():
        job_id = jobs.enqueue("discover", {}, "Scan")
        jobs.cancel(job_id)
        assert jobs.get(job_id)["state"] == "cancelled"
        assert jobs.run_next(app, "share") is False


def test_cancel_mid_run(dirs):
    app = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    steps = []

    @jobs.handler("test_loop")
    def loop(ctx):
        for i in range(100):
            steps.append(i)
            if i == 3:
                con = sqlite3.connect(app.config["DB_PATH"])
                con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (ctx.id,))
                con.commit()
                con.close()
                time.sleep(0.6)
            ctx.check_cancel()
        return {"message": "should not get here"}
    with app.app_context():
        job_id = jobs.enqueue("test_loop", {}, "Loop")
        jobs.run_next(app, "share")
        assert jobs.get(job_id)["state"] == "cancelled"
    assert len(steps) == 4
    jobs.HANDLERS.pop("test_loop")


def test_cancelled_upload_leaves_nothing(dirs):
    _, games = dirs
    app = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    c = Client(app).login()
    uid = c.upload("g.zip", make_zip({f"f{i}.bin": b"0" * 1000 for i in range(5)})).json["id"]
    r = c.c.post("/games/new", data={"title": "Gone", "platform": "html", "version": "1", "source": "upload",
                                     "upload_id": uid, "csrf": c.token()}, headers={"Origin": "http://localhost"})
    job_id = r.headers["Location"].rsplit("/", 1)[1]
    assert c.c.post(f"/jobs/{job_id}/cancel", data={"csrf": c.token()},
                    headers={"Origin": "http://localhost"}).status_code == 302
    with app.app_context():
        assert jobs.get(job_id)["state"] == "cancelled"
        # a queued upload that was cancelled never ran; cleanup happens via the CLEANUP hook on recovery,
        # and stale temp files are swept after 24 h. A running one cleans up itself:
        job2 = jobs.enqueue("install", {"mode": "new_game", "upload_id": uid, "parent": "", "title": "Gone",
                                        "platform": "html", "label": "1"}, "Unpack")
        con = sqlite3.connect(app.config["DB_PATH"])
        con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job2,))
        con.commit()
        con.close()
        jobs.run_next(app, "share")
        assert jobs.get(job2)["state"] == "cancelled"
    assert q(app, "SELECT COUNT(*) FROM games")[0][0] == 0
    assert [p for p in os.listdir(games) if not p.startswith(".")] == []
    assert os.listdir(app.config["TMP_DIR"]) == []


# ---------------------------------------------------------------- recovery

def test_restart_recovery(dirs):
    data, games = dirs
    app = create_app(cfg(dirs))
    tmp = app.config["TMP_DIR"]
    uid = uuid.uuid4().hex
    open(os.path.join(tmp, uid + ".upload"), "wb").write(b"zip")
    open(os.path.join(tmp, uid + ".json"), "w").write(json.dumps({"filename": "a.zip", "size": 3}))
    con = sqlite3.connect(app.config["DB_PATH"])
    for jid, kind, params in (("a1", "discover", {}), ("b2", "install", {"mode": "new_game", "upload_id": uid})):
        con.execute("INSERT INTO jobs(id,kind,lane,label,state,params,created_at) VALUES (?,?,?,?,?,?,?)",
                    (jid, kind, "share", kind, "running", json.dumps(params), "2026-01-01T00:00:00+00:00"))
    con.commit()
    con.close()
    app2 = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    rows = {r["id"]: r for r in q(app2, "SELECT id, state, error FROM jobs")}
    assert rows["a1"]["state"] == "queued"                    # safe to repeat: queued again
    assert rows["b2"]["state"] == "interrupted" and "stopped" in rows["b2"]["error"]
    assert not os.path.exists(os.path.join(tmp, uid + ".upload"))   # cleanup hook removed the upload


# --------------------------------------------------------------- scheduler

@pytest.mark.parametrize("now, at, hours, expected", [
    ("2026-11-26 01:00", "04:00", 24, "2026-11-26 04:00"),
    ("2026-11-26 04:00", "04:00", 24, "2026-11-27 04:00"),
    ("2026-11-26 13:30", "04:00", 6, "2026-11-26 16:00"),
    ("2026-11-26 23:59", "00:30", 12, "2026-11-27 00:30"),
])
def test_next_rescan(now, at, hours, expected):
    fmt = "%Y-%m-%d %H:%M"
    assert jobs.next_rescan(datetime.strptime(now, fmt), at, hours) == datetime.strptime(expected, fmt)


def test_rescan_disabled():
    assert jobs.next_rescan(datetime(2026, 1, 1), "04:00", 0) is None


# ------------------------------------------------- incremental rescans

def test_incremental_rescan_skips_unchanged(dirs, monkeypatch):
    _, games = dirs
    for i in range(4):
        mk(games, f"G{i} v1")
    app = create_app(cfg(dirs))
    c = Client(app).login()
    c.post("/scan", import_form([("G0 v1", "G0", "1"), ("G1 v1", "G1", "1")]))
    calls = []
    real_detect, real_size = storage.detect_platform, storage.folder_size
    monkeypatch.setattr(storage, "detect_platform", lambda p, *a, **k: calls.append(("detect", p)) or real_detect(p))
    monkeypatch.setattr(storage, "folder_size", lambda p, *a, **k: calls.append(("size", p)) or real_size(p, *a, **k))
    with app.app_context():
        jobs.enqueue("rescan", {}, "Rescan")
    first = len(calls)
    assert first >= 2                                       # untracked folders checked, sizes recorded
    calls.clear()
    with app.app_context():
        jobs.enqueue("rescan", {}, "Rescan")
    assert calls == []                                      # nothing changed: nothing re-read
    (games / "G1 v1" / "new.dat").write_text("more data")   # changes G1's mtime
    with app.app_context():
        jobs.enqueue("rescan", {}, "Rescan")
    assert [k for k, p in calls] == ["size"] and calls[0][1].endswith("G1 v1")
    size = q(app, "SELECT size_bytes FROM versions WHERE folder='G1 v1'")[0][0]
    assert size == 100 + len("more data")


# ---------------------------------------------------------------- rules

def test_games_inside_rule(client, dirs):
    _, games = dirs
    for name in ("Retro v2", "Snake v1", "Pong v3"):
        mk(games, f"HTML/{name}", ("index.html",))
    (games / "HTML" / "Folder Of Games").mkdir()
    mk(games, "HTML/Folder Of Games/Inner v1")
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="HTML/Folder Of Games/Inner v1"' in html
    client.post("/scan/rule", {"rule": "games-inside:HTML"})
    html = client.get("/scan").get_data(as_text=True)
    assert 'value="HTML/Folder Of Games"' in html and "Folder Of Games/Inner" not in html


# ---------------------------------------------------------------- scale

def test_hundreds_of_small_games(dirs):
    _, games = dirs
    n = 300
    for i in range(n):
        mk(games, f"{['PC', 'Browser', 'Linux'][i % 3]}/Cat {i % 7}/Game {i:03d} v1.{i % 4}",
           ("index.html",) if i % 3 == 1 else ("game.exe",))
    app = create_app(cfg(dirs))
    c = Client(app).login()
    t0 = time.time()
    html = c.get("/scan").get_data(as_text=True)
    assert html.count('name="folder-') == n
    rows = [(f"{['PC', 'Browser', 'Linux'][i % 3]}/Cat {i % 7}/Game {i:03d} v1.{i % 4}", f"Game {i:03d}", f"1.{i % 4}")
            for i in range(n)]
    r = c.post("/scan", import_form(rows))
    assert r.status_code == 302
    assert q(app, "SELECT COUNT(*) FROM games")[0][0] == n
    assert c.get("/").status_code == 200 and c.get("/?group=1").status_code == 200
    assert time.time() - t0 < 60


def test_measuring_shown_until_measured(client, dirs):
    from app.views import human_size
    assert human_size(-1) == "measuring…"
    _, games = dirs
    mk(games, "Sized v1")
    r = client.post("/games/new", {"title": "Sized", "platform": "other", "version": "1", "source": "folder",
                                   "folder": "Sized v1"})
    assert r.status_code == 302
    gid = r.headers["Location"].rsplit("/", 1)[1]
    assert "100 B" in client.get(f"/games/{gid}").get_data(as_text=True)   # measured by the (inline) job


# ---------------------------------------------------------- job versioning

def test_job_versioning(dirs):
    app = create_app(cfg(dirs, JOBS_SYNC=False, JOB_WORKERS_AUTOSTART=False))
    seen = []

    @jobs.handler("test_v", version=3)
    def run(ctx):
        seen.append(ctx.params)
        return {}

    @jobs.migrates("test_v", 1)
    def v1_to_v2(p):
        p["folders"] = [p.pop("folder")]
        return p

    @jobs.migrates("test_v", 2)
    def v2_to_v3(p):
        p["mode"] = p.get("mode", "default")
        return p

    def insert(jid, version, params, kind="test_v"):
        con = sqlite3.connect(app.config["DB_PATH"])
        con.execute("INSERT INTO jobs(id,kind,lane,label,state,params,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (jid, kind, "share", "t", "queued", json.dumps(params), version, f"2026-01-01T00:00:0{len(jid)}"))
        con.commit()
        con.close()
    try:
        with app.app_context():
            insert("old", 1, {"folder": "A"})               # queued by an older GameLib
            jobs.run_next(app, "share")
            assert seen == [{"folders": ["A"], "mode": "default"}]
            assert jobs.get("old")["state"] == "done" and jobs.get("old")["version"] == 3
            insert("newer", 9, {})                            # queued by a newer GameLib, then rolled back
            jobs.run_next(app, "share")
            j = jobs.get("newer")
            assert j["state"] == "failed" and "newer GameLib" in j["error"]
            jobs.MIGRATIONS.pop(("test_v", 1))
            insert("nopath", 1, {"folder": "B"})              # no upgrade path any more
            jobs.run_next(app, "share")
            assert jobs.get("nopath")["state"] == "failed" and "can't be upgraded" in jobs.get("nopath")["error"]
            insert("gone", 1, {}, kind="removed_kind")       # kind removed from the app
            jobs.run_next(app, "share")
            assert "no longer supported" in jobs.get("gone")["error"]
            jid = jobs.enqueue("test_v", {"folders": []}, "x")
            row = q(app, "SELECT version, app_version FROM jobs WHERE id=?", jid)[0]
            assert row["version"] == 3 and row["app_version"] == app.config["APP_VERSION"]
    finally:
        jobs.HANDLERS.pop("test_v", None)
        jobs.VERSIONS.pop("test_v", None)
        jobs.MIGRATIONS.pop(("test_v", 2), None)
