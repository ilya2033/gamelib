import os
import secrets
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

from . import auth, db, jobs, play, storage, tasks, tools, views  # noqa: F401  (tasks/tools register handlers and routes)
from .version import __version__


def _load_secret(data_dir):
    path = os.path.join(data_dir, "secret_key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def create_app(config=None):
    app = Flask(__name__)
    env = os.environ
    app.config.update(
        DATA_DIR=env.get("DATA_DIR", "/data"),
        GAMES_DIR=env.get("GAMES_DIR", "/games"),
        USERNAME=env.get("GAMELIB_USER", "admin"),
        PASSWORD=env.get("GAMELIB_PASSWORD", ""),
        MAX_UNCOMPRESSED_BYTES=int(float(env.get("MAX_UNCOMPRESSED_GB", "500")) * 1024**3),
        TRUSTED_ORIGINS=[o.strip() for o in env.get("TRUSTED_ORIGINS", "").split(",") if o.strip()],
        SESSION_COOKIE_SECURE=env.get("COOKIE_SECURE", "0") == "1",
        SCAN_DEPTH=min(max(int(env.get("SCAN_DEPTH", "3") or 3), 1), 6),
        LAYOUT_CACHE_SECONDS=60,
        SCAN_RESULT_MAX_AGE=60,          # opening Scan reuses a discovery younger than this
        JOBS_SYNC=False,                 # tests: run jobs inline
        JOB_WORKERS_AUTOSTART=True,
        JOB_LIGHT_WORKERS=int(env.get("JOB_LIGHT_WORKERS", "2") or 2),
        RESCAN_AT=env.get("RESCAN_AT", "04:00"),
        RESCAN_INTERVAL_HOURS=float(env.get("RESCAN_INTERVAL_HOURS", "24") or 0),
    )
    if config:
        app.config.update(config)
    if app.config.get("TESTING") and "JOBS_SYNC" not in (config or {}):
        app.config["JOBS_SYNC"] = True   # tests run jobs inline unless they ask for real threads
    if not app.config["PASSWORD"]:
        raise RuntimeError("GAMELIB_PASSWORD is not set. Set it in the container settings.")

    data = app.config["DATA_DIR"]
    for sub in ("covers", "tmp", "backups"):
        os.makedirs(os.path.join(data, sub), exist_ok=True)
    app.config.update(
        DB_PATH=os.path.join(data, "gamelib.db"),
        COVERS_DIR=os.path.join(data, "covers"),
        TMP_DIR=os.path.join(data, "tmp"),
        BACKUP_DIR=os.path.join(data, "backups"),
        MAX_CONTENT_LENGTH=None,  # uploads are streamed; forms are capped in auth.guard
        SESSION_COOKIE_NAME="gamelib_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.secret_key = _load_secret(data)

    db.init_app(app)
    auth.init_app(app)
    app.register_blueprint(views.bp)
    app.register_blueprint(play.bp)
    views.register_filters(app)
    app.jinja_env.globals["APP_VERSION"] = __version__
    app.config["APP_VERSION"] = __version__
    storage.cleanup_stale(app.config["TMP_DIR"], app.config["GAMES_DIR"])
    jobs.start_workers(app)
    jobs.start_scheduler(app)

    def handle_http_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description, status=e.code), e.code
        return render_template("error.html", error=e), e.code

    for code in (400, 401, 403, 404, 405, 411, 413, 429):
        app.register_error_handler(code, handle_http_error)
    return app
