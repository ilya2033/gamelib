"""Background workers with a SQLite-backed job queue.

Slow share work (walking folders, detecting platforms, measuring sizes,
unpacking uploads, deleting folders) runs here so web requests return
immediately.

Lanes:
  share  1 thread  - anything that reads/writes a lot of share data, one at a
                     time so an Unraid array isn't thrashed across disks
  light  N threads - quick jobs

Jobs survive restarts: on start, jobs left 'running' are marked
'interrupted' and the idempotent ones are queued again. Everything runs in
the single gunicorn worker process (see gunicorn.conf.py). With JOBS_SYNC
(tests) a job runs inline before enqueue() returns."""
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app

from . import db as dbmod

log = logging.getLogger("gamelib")

HANDLERS = {}            # kind -> (lane, fn, retry_on_restart)
VERSIONS = {}            # kind -> current handler version
MIGRATIONS = {}          # (kind, from_version) -> fn(params) returning params for from_version + 1
# Live progress and cancel requests live in memory (single process), so they
# never wait on a database write lock held by a running job.
_live = {}               # job id -> {"done", "total", "message"}
_cancels = set()         # job ids asked to stop
_mem = threading.Lock()
ACTIVE = ("queued", "running")
KEEP_JOBS = 200
KEEP_DAYS = 30


class Cancelled(Exception):
    pass


def handler(kind, lane="share", retry_on_restart=True, version=1):
    """Register fn(ctx) as the runner for a job kind.

    Job versioning: bump `version` whenever a handler's params or behaviour
    change incompatibly, and register a params upgrade for the old version
    with @migrates(kind, old_version). Jobs queued by an older GameLib (still
    queued, or re-queued after a restart) are then upgraded step by step;
    without an upgrade path they fail with a clear message instead of running
    with params they don't understand. Jobs from a newer GameLib (after a
    rollback) are refused."""
    def deco(fn):
        HANDLERS[kind] = (lane, fn, retry_on_restart)
        VERSIONS[kind] = version
        return fn
    return deco


def migrates(kind, from_version):
    """Register fn(params) -> params upgrading a job from from_version to from_version + 1."""
    def deco(fn):
        MIGRATIONS[(kind, from_version)] = fn
        return fn
    return deco


class Incompatible(Exception):
    pass


def upgrade_params(kind, version, params):
    """Return (params, version) brought up to the current handler version."""
    if kind not in HANDLERS:
        raise Incompatible(f"This kind of job ({kind}) is no longer supported by this GameLib version.")
    current = VERSIONS[kind]
    if version > current:
        raise Incompatible(f"This job was created by a newer GameLib (job v{version}, this app knows v{current}). "
                           "Start it again from this version.")
    while version < current:
        step = MIGRATIONS.get((kind, version))
        if step is None:
            raise Incompatible(f"This job was created by an older GameLib (job v{version}, now v{current}) "
                               "and can't be upgraded. Start it again.")
        params = step(dict(params))
        version += 1
    return params, version


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _con(app):
    return dbmod.connect(app.config["DB_PATH"])


class Ctx:
    """What a job handler gets: its params, progress reporting and cancel checks."""

    def __init__(self, app, row):
        self.app = app
        self.id = row["id"]
        self.kind = row["kind"]
        self.params = json.loads(row["params"])
        self._last_write = 0.0
        self._last_cancel_check = 0.0
        self._cancelled = False

    def progress(self, done=None, total=None, message=None, force=False):
        with _mem:
            live = _live.setdefault(self.id, {"done": 0, "total": 0, "message": ""})
            if done is not None:
                live["done"] = done
            if total is not None:
                live["total"] = total
            if message is not None:
                live["message"] = str(message)[:300]

    def check_cancel(self):
        now = time.monotonic()
        if not self._cancelled and now - self._last_cancel_check > 0.5:
            self._last_cancel_check = now
            with _mem:
                self._cancelled = self.id in _cancels
            if not self._cancelled:   # also honour a flag set directly in the DB (other tools, tests)
                con = _con(self.app)
                row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,)).fetchone()
                con.close()
                self._cancelled = bool(row and row["cancel_requested"])
        if self._cancelled:
            raise Cancelled()


# ------------------------------------------------------------- queue API

def enqueue(kind, params=None, label=None, exclusive=False):
    """Queue a job and return its id. With exclusive, a queued or running
    job of the same kind is returned instead of adding another."""
    app = current_app._get_current_object()
    lane = HANDLERS[kind][0]
    con = _con(app)
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            if exclusive:
                row = con.execute("SELECT id FROM jobs WHERE kind=? AND state IN ('queued','running') "
                                  "ORDER BY created_at LIMIT 1", (kind,)).fetchone()
                if row:
                    return row["id"]
            job_id = uuid.uuid4().hex[:16]
            con.execute("INSERT INTO jobs(id,kind,lane,label,state,params,version,app_version,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (job_id, kind, lane, label or kind, "queued", json.dumps(params or {}),
                         VERSIONS[kind], app.config.get("APP_VERSION"), _now()))
    finally:
        con.close()
    if app.config.get("JOBS_SYNC"):
        run_next(app, lane)
    else:
        _wake(app, lane)
    return job_id


def get(job_id):
    con = _con(current_app)
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    con.close()
    return _as_dict(row) if row else None


def recent(limit=50):
    con = _con(current_app)
    rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [_as_dict(r) for r in rows]


def latest(kinds, state="done"):
    con = _con(current_app)
    q = ",".join("?" * len(kinds))
    row = con.execute(f"SELECT * FROM jobs WHERE kind IN ({q}) AND state=? ORDER BY finished_at DESC LIMIT 1",
                      (*kinds, state)).fetchone()
    con.close()
    return _as_dict(row) if row else None


def active(kinds=None):
    con = _con(current_app)
    if kinds:
        q = ",".join("?" * len(kinds))
        rows = con.execute(f"SELECT * FROM jobs WHERE state IN ('queued','running') AND kind IN ({q}) "
                           "ORDER BY created_at", tuple(kinds)).fetchall()
    else:
        rows = con.execute("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY created_at").fetchall()
    con.close()
    return [_as_dict(r) for r in rows]


def cancel(job_id):
    app = current_app._get_current_object()
    con = _con(app)
    with con:
        row = con.execute("SELECT kind, params FROM jobs WHERE id=? AND state='queued'", (job_id,)).fetchone()
        n = con.execute("UPDATE jobs SET state='cancelled', finished_at=? WHERE id=? AND state='queued'",
                        (_now(), job_id)).rowcount
    con.close()
    with _mem:
        _cancels.add(job_id)
    if n and row:                      # never started: undo what the request prepared (e.g. the upload)
        _cleanup(app, row["kind"], row["params"])


def _cleanup(app, kind, params_json):
    fn = CLEANUP.get(kind)
    if fn:
        try:
            with app.app_context():
                fn(json.loads(params_json))
        except Exception:
            log.exception("Cleanup for %s job failed", kind)


def mark_seen(job_id):
    con = _con(current_app)
    with con:
        n = con.execute("UPDATE jobs SET seen=1 WHERE id=? AND seen=0", (job_id,)).rowcount
    con.close()
    return bool(n)


def _as_dict(row):
    d = dict(row)
    with _mem:
        live = _live.get(d["id"])
        if live and d["state"] == "running":
            d.update(live)
    d["params"] = json.loads(d["params"] or "{}")
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["active"] = d["state"] in ACTIVE
    d["pct"] = round(100 * d["done"] / d["total"], 1) if d["total"] else None
    return d


# --------------------------------------------------------------- running

def run_next(app, lane):
    """Claim and run the oldest queued job of a lane. Returns False if none."""
    con = _con(app)
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE lane=? AND state='queued' ORDER BY created_at, rowid LIMIT 1",
                              (lane,)).fetchone()
            if row is None:
                return False
            con.execute("UPDATE jobs SET state='running', started_at=? WHERE id=?", (_now(), row["id"]))
    finally:
        con.close()
    _execute(app, row)
    return True


def _execute(app, row):
    state, result, error = "done", None, None
    try:
        params, version = upgrade_params(row["kind"], row["version"], json.loads(row["params"]))
    except Incompatible as e:
        _finish(app, row["id"], "failed", None, str(e))
        _cleanup(app, row["kind"], row["params"])
        return
    if version != row["version"]:
        con = _con(app)
        with con:
            con.execute("UPDATE jobs SET params=?, version=? WHERE id=?", (json.dumps(params), version, row["id"]))
        con.close()
        row = dict(row, params=json.dumps(params), version=version)
    _lane, fn, _retry = HANDLERS[row["kind"]]
    with app.app_context():
        ctx = Ctx(app, row)
        try:
            if row["cancel_requested"]:
                _cleanup(app, row["kind"], row["params"])
                raise Cancelled()
            result = fn(ctx)
        except Cancelled:
            state = "cancelled"
        except Exception as e:      # report it on the job, keep the worker alive
            log.exception("Job %s (%s) failed", row["kind"], row["id"])
            state, error = "failed", (str(e) or e.__class__.__name__)[:1000]
    _finish(app, row["id"], state, result, error)


def _finish(app, job_id, state, result, error):
    with _mem:
        live = _live.pop(job_id, {})
        _cancels.discard(job_id)
    con = _con(app)
    with con:
        con.execute("UPDATE jobs SET state=?, result=?, error=?, finished_at=?, done=?, total=?, message=? "
                    "WHERE id=?", (state, json.dumps(result) if result is not None else None, error, _now(),
                                   live.get("done", 0), live.get("total", 0), live.get("message", ""), job_id))
    con.close()


_events = {}
_lock = threading.Lock()


def _wake(app, lane):
    ev = _events.get((id(app), lane))
    if ev:
        ev.set()


def _worker(app, lane, ev):
    while True:
        try:
            while run_next(app, lane):
                pass
        except Exception:
            log.exception("Worker %s crashed while claiming a job", lane)
        ev.wait(5)
        ev.clear()


def start_workers(app):
    """Recover interrupted jobs, prune history, start the worker threads."""
    recover(app)
    if app.config.get("JOBS_SYNC") or not app.config.get("JOB_WORKERS_AUTOSTART", True):
        return
    with _lock:
        if app.extensions.get("gamelib_workers"):
            return
        app.extensions["gamelib_workers"] = True
    lanes = [("share", 1), ("light", max(1, int(app.config.get("JOB_LIGHT_WORKERS", 2))))]
    for lane, n in lanes:
        ev = threading.Event()
        _events[(id(app), lane)] = ev
        for i in range(n):
            threading.Thread(target=_worker, args=(app, lane, ev), daemon=True, name=f"gamelib-{lane}-{i}").start()
        ev.set()


def recover(app):
    """Jobs that were running when the container stopped: re-queue the ones
    that are safe to repeat, fail the rest (their cleanup hook runs)."""
    con = _con(app)
    rows = con.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
    with con:
        for r in rows:
            kind_info = HANDLERS.get(r["kind"])
            if kind_info is None:
                con.execute("UPDATE jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                            (_now(), f"This kind of job ({r['kind']}) is no longer supported.", r["id"]))
            elif kind_info[2]:
                con.execute("UPDATE jobs SET state='queued', started_at=NULL, message='Restarted after interruption' "
                            "WHERE id=?", (r["id"],))
            else:
                con.execute("UPDATE jobs SET state='interrupted', finished_at=?, "
                            "error='The container stopped while this job was running.' WHERE id=?", (_now(), r["id"]))
                _cleanup(app, r["kind"], r["params"])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        con.execute("DELETE FROM jobs WHERE state NOT IN ('queued','running') AND (finished_at < ? OR id NOT IN "
                    "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?))", (cutoff, KEEP_JOBS))
    con.close()


CLEANUP = {}             # kind -> fn(params) run when a non-retryable job was interrupted


# ------------------------------------------------------------- scheduler

def next_rescan(now, at="04:00", hours=24):
    """Next scheduled rescan after `now` (a local, naive datetime)."""
    if hours <= 0:
        return None
    hh, mm = (int(x) for x in at.split(":"))
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    step = timedelta(hours=hours)
    while base > now:
        base -= step
    while base <= now:
        base += step
    return base


def start_scheduler(app, kind="rescan"):
    hours = float(app.config.get("RESCAN_INTERVAL_HOURS", 24))
    if hours <= 0 or app.config.get("JOBS_SYNC") or not app.config.get("JOB_WORKERS_AUTOSTART", True):
        return
    at = app.config.get("RESCAN_AT", "04:00")

    def loop():
        due = next_rescan(datetime.now(), at, hours)
        log.warning("Next scheduled share rescan: %s", due.strftime("%Y-%m-%d %H:%M"))
        while True:
            time.sleep(30)
            if datetime.now() >= due:
                with app.app_context():
                    enqueue(kind, {}, "Scheduled share rescan", exclusive=True)
                due = next_rescan(datetime.now(), at, hours)

    threading.Thread(target=loop, daemon=True, name="gamelib-scheduler").start()
