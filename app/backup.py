"""Make a consistent copy of the database while the app is running.

    docker exec -u 99:100 gamelib python -m app.backup

Writes /data/backups/gamelib-<timestamp>-manual.db (the 10 newest are kept)."""
import os
import sys

from . import db


def main():
    data = os.environ.get("DATA_DIR", "/data")
    path = os.path.join(data, "gamelib.db")
    if not os.path.exists(path):
        sys.exit(f"No database at {path}")
    con = db.connect(path)
    try:
        name = db.backup(con, os.path.join(data, "backups"), "manual")
    finally:
        con.close()
    print(os.path.join(data, "backups", name))


if __name__ == "__main__":
    main()
