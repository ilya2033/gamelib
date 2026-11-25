"""SQLite access and schema migrations.

The schema version is stored in PRAGMA user_version. Each entry in
MIGRATIONS moves the schema forward by one version. Before an existing
database is migrated, a copy is written to /data/backups so a container
update can be rolled back by restoring that file and the older image."""
import logging
import os
import sqlite3
import time

from flask import current_app, g

log = logging.getLogger("gamelib")

MIGRATIONS = [
    # v1: initial schema
    """
    CREATE TABLE games (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        platform    TEXT NOT NULL DEFAULT 'other',
        description TEXT NOT NULL DEFAULT '',
        cover       TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE versions (
        id         INTEGER PRIMARY KEY,
        game_id    INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        label      TEXT NOT NULL,
        folder     TEXT NOT NULL UNIQUE,
        notes      TEXT NOT NULL DEFAULT '',
        size_bytes INTEGER NOT NULL DEFAULT 0,
        added_at   TEXT NOT NULL
    );
    CREATE INDEX versions_game ON versions(game_id, added_at);
    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    );
    CREATE TABLE game_tags (
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (game_id, tag_id)
    );
    """,
    # v2 (1.1.0): game statuses + status history
    """
    CREATE TABLE statuses (
        id      INTEGER PRIMARY KEY,
        key     TEXT NOT NULL UNIQUE,
        label   TEXT NOT NULL,
        color   TEXT NOT NULL,
        sort    INTEGER NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO statuses(key, label, color, sort, builtin) VALUES
        ('pending',   'Pending',   '#8a93a3', 10, 1),
        ('on_hold',   'On hold',   '#e7b84a', 20, 1),
        ('completed', 'Completed', '#5cc98a', 30, 1),
        ('abandoned', 'Abandoned', '#ef6461', 40, 1);
    ALTER TABLE games ADD COLUMN status_id INTEGER REFERENCES statuses(id);
    ALTER TABLE games ADD COLUMN status_changed_at TEXT;
    UPDATE games SET status_id = (SELECT id FROM statuses WHERE key = 'pending'),
                     status_changed_at = created_at;
    CREATE INDEX games_status ON games(status_id);
    CREATE TABLE status_history (
        id      INTEGER PRIMARY KEY,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        from_id INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
        to_id   INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
        from_label TEXT,
        to_label   TEXT NOT NULL,
        source  TEXT NOT NULL,
        at      TEXT NOT NULL
    );
    CREATE INDEX status_history_game ON status_history(game_id, at);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)
KEEP_MANUAL = 10   # manual backups kept
KEEP_PRE = 5       # pre-migration backups kept (never removed by manual backups)


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def backup(con, backup_dir, reason):
    os.makedirs(backup_dir, exist_ok=True)
    now = time.time()
    stem = f"gamelib-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}{int(now * 1000) % 1000:03d}"
    name, n = f"{stem}-{reason}.db", 1
    while os.path.exists(os.path.join(backup_dir, name)):
        n += 1
        name = f"{stem}.{n}-{reason}.db"
    dest = sqlite3.connect(os.path.join(backup_dir, name))
    with dest:
        con.backup(dest)
    dest.close()
    # Rotate each kind on its own, so routine manual backups can never
    # push out the pre-migration copy you would need for a rollback.
    kind_pre = reason.startswith("pre-v")
    keep = KEEP_PRE if kind_pre else KEEP_MANUAL
    same_kind = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("gamelib-") and f.endswith(".db") and ("-pre-v" in f) == kind_pre)
    for f in same_kind[:-keep]:
        os.unlink(os.path.join(backup_dir, f))
    return name


def migrate(path, backup_dir):
    con = connect(path)
    try:
        current = con.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than this app supports (v{SCHEMA_VERSION}). "
                "You are running an older image than the one that last used this data. "
                "Use the newer image, or restore a backup from /data/backups.")
        if current < SCHEMA_VERSION:
            if current > 0:
                name = backup(con, backup_dir, f"pre-v{SCHEMA_VERSION}")
                log.warning("Database backup before migration: %s", os.path.join(backup_dir, name))
            for v in range(current, SCHEMA_VERSION):
                con.executescript("BEGIN;" + MIGRATIONS[v] + f"; PRAGMA user_version = {v + 1}; COMMIT;")
                log.warning("Database migrated to schema v%d", v + 1)
        return current
    finally:
        con.close()


def init_app(app):
    migrate(app.config["DB_PATH"], app.config["BACKUP_DIR"])
    app.teardown_appcontext(close_db)
