import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_file,
                   send_from_directory, url_for)
from PIL import Image, UnidentifiedImageError

from . import storage
from .db import get_db
from .storage import PLATFORMS, BadPath, UnsafeZip
from .version import __version__

bp = Blueprint("main", __name__)

UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_EXTS = (".zip", ".html", ".htm")
SORTS = {"name": ("g.title COLLATE NOCASE", "asc"),
         "added": ("g.created_at", "desc"),
         "updated": ("g.updated_at", "desc"),
         "status": ("s.sort", "asc"),
         "status_changed": ("g.status_changed_at", "desc")}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class UserError(Exception):
    pass


# ------------------------------------------------------------- helpers

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_precise():
    """Microsecond timestamps for status changes, so "Status changed" sorts exactly."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def iso_from_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def cfg(key):
    return current_app.config[key]


def games_dir():
    return cfg("GAMES_DIR")


def tracked_folders():
    return {r["folder"] for r in get_db().execute("SELECT folder FROM versions")}


def untracked_folders():
    tracked = tracked_folders()
    return [f for f in storage.list_folders(games_dir()) if f not in tracked]


def get_game(gid):
    row = get_db().execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if row is None:
        abort(404)
    return row


def get_version(vid):
    row = get_db().execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
    if row is None:
        abort(404)
    return row


def game_tags(gid):
    rows = get_db().execute(
        "SELECT t.name FROM tags t JOIN game_tags gt ON gt.tag_id=t.id "
        "WHERE gt.game_id=? ORDER BY t.name COLLATE NOCASE", (gid,))
    return [r["name"] for r in rows]


def parse_tags(text):
    seen, out = set(), []
    for t in (text or "").split(","):
        t = " ".join(t.split())[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:30]


def set_tags(gid, tags):
    db = get_db()
    db.execute("DELETE FROM game_tags WHERE game_id=?", (gid,))
    for t in tags:
        db.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (t,))
        tid = db.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO game_tags VALUES (?,?)", (gid, tid))
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM game_tags)")


def save_cover(src):
    """Validate an image (file object or path), shrink it and store as WebP."""
    try:
        with Image.open(src) as img:
            img.load()
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img.thumbnail((600, 900))
            name = uuid.uuid4().hex + ".webp"
            img.save(os.path.join(cfg("COVERS_DIR"), name), "WEBP", quality=85)
            return name
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise UserError("The cover must be a PNG, JPEG, WebP or GIF image.") from None


def remove_cover(name):
    if name:
        try:
            os.unlink(os.path.join(cfg("COVERS_DIR"), os.path.basename(name)))
        except FileNotFoundError:
            pass


def cover_from_form():
    f = request.files.get("cover")
    if f and f.filename:
        return save_cover(f.stream)
    return None


def auto_cover(folder):
    try:
        path = storage.find_cover_file(storage.resolve_in(games_dir(), folder))
        return save_cover(path) if path else None
    except (UserError, BadPath):
        return None


def upload_paths(upload_id):
    tmp = cfg("TMP_DIR")
    return os.path.join(tmp, upload_id + ".upload"), os.path.join(tmp, upload_id + ".json")


def discard_upload(upload_id):
    for p in upload_paths(upload_id):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def materialize_source(form, title, label):
    """Returns the share-relative folder for a new version, or None."""
    source = form.get("source", "upload")
    if source == "none":
        return None
    if source == "folder":
        folder = form.get("folder", "")
        if folder not in untracked_folders():
            raise UserError("Pick an untracked folder from the games share.")
        storage.resolve_in(games_dir(), folder)
        return folder
    upload_id = form.get("upload_id", "")
    if not UPLOAD_ID.match(upload_id):
        raise UserError("Upload a file first and wait for it to finish.")
    data_path, meta_path = upload_paths(upload_id)
    if not os.path.exists(data_path):
        raise UserError("That upload has expired. Upload the file again.")
    with open(meta_path) as f:
        meta = json.load(f)
    try:
        return storage.install_upload(data_path, meta["filename"], games_dir(),
                                      f"{title} v{label}", cfg("MAX_UNCOMPRESSED_BYTES"))
    except UnsafeZip as e:
        raise UserError(f"Rejected zip: {e}") from None
    finally:
        discard_upload(upload_id)


def insert_version(gid, label, folder, notes="", added_at=None):
    path = storage.resolve_in(games_dir(), folder)
    added_at = added_at or now()
    get_db().execute(
        "INSERT INTO versions(game_id,label,folder,notes,size_bytes,added_at) VALUES (?,?,?,?,?,?)",
        (gid, label, folder, notes, storage.folder_size(path), added_at))
    get_db().execute("UPDATE games SET updated_at=max(updated_at, ?) WHERE id=?", (added_at, gid))


def delete_folder(folder):
    """Delete a version folder from the share. Refuses anything reached
    through a symlink, so a swapped-in link can't redirect the delete."""
    try:
        path = storage.resolve_no_links(games_dir(), folder)
    except BadPath:
        return False
    if os.path.isdir(path):
        shutil.rmtree(path)
        return True
    return False


def require_confirm():
    if request.form.get("confirm") != "yes":
        abort(400, "Deletion was not confirmed.")


# -------------------------------------------------------------- statuses

def all_statuses():
    return get_db().execute("SELECT * FROM statuses ORDER BY sort, id").fetchall()


def status_by_key(key):
    return get_db().execute("SELECT * FROM statuses WHERE key=?", (key,)).fetchone()


def default_status_id():
    return status_by_key("pending")["id"]


def status_from_form(value):
    """A status key from a form, or the default (Pending)."""
    row = status_by_key(value) if value else None
    return row["id"] if row else default_status_id()


def set_status(gid, status_id, source="user"):
    """Change a game's status and record it in the history. No-op if unchanged."""
    db = get_db()
    cur = db.execute("SELECT g.status_id, s.label FROM games g LEFT JOIN statuses s ON s.id=g.status_id "
                     "WHERE g.id=?", (gid,)).fetchone()
    new = db.execute("SELECT id, label FROM statuses WHERE id=?", (status_id,)).fetchone()
    if cur is None or new is None or cur["status_id"] == new["id"]:
        return False
    ts = now_precise()
    db.execute("UPDATE games SET status_id=?, status_changed_at=? WHERE id=?", (new["id"], ts, gid))
    db.execute("INSERT INTO status_history(game_id,from_id,to_id,from_label,to_label,source,at) "
               "VALUES (?,?,?,?,?,?,?)", (gid, cur["status_id"], new["id"], cur["label"], new["label"], source, ts))
    return True


def unique_status_key(label):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "status"
    key, n = base, 2
    while status_by_key(key):
        key, n = f"{base}_{n}", n + 1
    return key


# --------------------------------------------------------------- library

@bp.get("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "")
    tag = request.args.get("tag", "")
    status = request.args.get("status", "")
    sort = request.args.get("sort", "name")
    if sort not in SORTS:
        sort = "name"
    col, default_dir = SORTS[sort]
    direction = request.args.get("dir", default_dir)
    if direction not in ("asc", "desc"):
        direction = default_dir

    where, args = [], []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(g.title LIKE ? ESCAPE '\\' OR g.description LIKE ? ESCAPE '\\' OR EXISTS ("
                     "SELECT 1 FROM game_tags gt JOIN tags t ON t.id=gt.tag_id "
                     "WHERE gt.game_id=g.id AND t.name LIKE ? ESCAPE '\\'))")
        args += [like] * 3
    if platform in PLATFORMS:
        where.append("g.platform=?")
        args.append(platform)
    if tag:
        where.append("EXISTS (SELECT 1 FROM game_tags gt JOIN tags t ON t.id=gt.tag_id "
                     "WHERE gt.game_id=g.id AND t.name=? COLLATE NOCASE)")
        args.append(tag)
    if status:
        where.append("s.key=?")
        args.append(status)
    sql = (
        "SELECT g.*, s.key AS status_key, s.label AS status_label, s.color AS status_color, "
        "(SELECT label FROM versions v WHERE v.game_id=g.id ORDER BY v.added_at DESC, v.id DESC LIMIT 1) AS latest, "
        "(SELECT COUNT(*) FROM versions v WHERE v.game_id=g.id) AS nversions, "
        "(SELECT group_concat(t.name, '\x1f') FROM game_tags gt JOIN tags t ON t.id=gt.tag_id WHERE gt.game_id=g.id) AS tagstr "
        "FROM games g LEFT JOIN statuses s ON s.id=g.status_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY {col} {direction.upper()}, "
        + ("g.id ASC" if sort == "name" else
           "g.title COLLATE NOCASE ASC" if sort == "status" else f"g.id {direction.upper()}")
    )
    games = []
    for r in db.execute(sql, args):
        d = dict(r)
        d["tags"] = sorted((r["tagstr"] or "").split("\x1f"), key=str.lower) if r["tagstr"] else []
        games.append(d)

    platform_counts = {r["platform"]: r["n"] for r in db.execute(
        "SELECT platform, COUNT(*) n FROM games GROUP BY platform")}
    tag_counts = db.execute(
        "SELECT t.name, COUNT(*) n FROM tags t JOIN game_tags gt ON gt.tag_id=t.id "
        "GROUP BY t.id ORDER BY t.name COLLATE NOCASE").fetchall()
    total = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    status_counts = db.execute(
        "SELECT s.key, s.label, s.color, COUNT(g.id) n FROM statuses s "
        "LEFT JOIN games g ON g.status_id=s.id GROUP BY s.id ORDER BY s.sort, s.id").fetchall()
    return render_template(
        "index.html", games=games, q=q, platform=platform, tag=tag, status=status, sort=sort,
        direction=direction, platform_counts=platform_counts, tag_counts=tag_counts,
        status_counts=status_counts,
        total=total, untracked=len(untracked_folders()))


@bp.get("/games/<int:gid>")
def game_detail(gid):
    game = get_game(gid)
    status = get_db().execute("SELECT * FROM statuses WHERE id=?", (game["status_id"],)).fetchone()
    history = get_db().execute(
        "SELECT * FROM status_history WHERE game_id=? ORDER BY at DESC, id DESC LIMIT 20", (gid,)).fetchall()
    versions = []
    for v in get_db().execute(
            "SELECT * FROM versions WHERE game_id=? ORDER BY added_at DESC, id DESC", (gid,)):
        d = dict(v)
        try:
            d["exists"] = os.path.isdir(storage.resolve_in(games_dir(), v["folder"]))
        except BadPath:
            d["exists"] = False
        versions.append(d)
    return render_template("game.html", game=game, versions=versions, tags=game_tags(gid),
                           status=status, statuses=all_statuses(), history=history)


@bp.post("/games/<int:gid>/status")
def game_status(gid):
    get_game(gid)
    row = status_by_key(request.form.get("status", ""))
    if row is None:
        abort(400, "Unknown status.")
    if set_status(gid, row["id"]):
        get_db().commit()
        flash(f"Status set to {row['label']}.", "ok")
    return redirect(url_for("main.game_detail", gid=gid))


@bp.route("/games/new", methods=["GET", "POST"])
def game_new():
    form = request.form
    if request.method == "POST":
        title = form.get("title", "").strip()
        platform = form.get("platform", "other")
        label = form.get("version", "").strip()
        cover = None
        try:
            if not title:
                raise UserError("Title is required.")
            if platform not in PLATFORMS:
                raise UserError("Unknown platform.")
            if form.get("source") != "none" and not label:
                raise UserError("Version label is required.")
            cover = cover_from_form()
            folder = materialize_source(form, title, label)
            if folder and not cover:
                cover = auto_cover(folder)
            ts = now()
            db = get_db()
            gid = db.execute(
                "INSERT INTO games(title,platform,description,cover,created_at,updated_at,status_id,status_changed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (title, platform, form.get("description", "").strip(), cover, ts, ts,
                 status_from_form(form.get("status")), ts)).lastrowid
            set_tags(gid, parse_tags(form.get("tags")))
            if folder:
                insert_version(gid, label, folder, form.get("notes", "").strip(), ts)
            db.commit()
        except (UserError, BadPath) as e:
            remove_cover(cover)
            flash(str(e), "error")
            return render_template("game_new.html", form=form, folders=untracked_folders(),
                                   statuses=all_statuses()), 400
        flash(f"Added {title}.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("game_new.html", form={}, folders=untracked_folders(), statuses=all_statuses())


@bp.route("/games/<int:gid>/edit", methods=["GET", "POST"])
def game_edit(gid):
    game = get_game(gid)
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()
        platform = f.get("platform", "other")
        try:
            if not title:
                raise UserError("Title is required.")
            if platform not in PLATFORMS:
                raise UserError("Unknown platform.")
            new_cover = cover_from_form()
        except UserError as e:
            flash(str(e), "error")
            return render_template("game_edit.html", game=game, tags=parse_tags(f.get("tags"))), 400
        cover = game["cover"]
        if new_cover or f.get("remove_cover"):
            remove_cover(cover)
            cover = new_cover
        db = get_db()
        db.execute("UPDATE games SET title=?, platform=?, description=?, cover=?, updated_at=? WHERE id=?",
                   (title, platform, f.get("description", "").strip(), cover, now(), gid))
        set_tags(gid, parse_tags(f.get("tags")))
        db.commit()
        flash("Saved.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("game_edit.html", game=game, tags=game_tags(gid))


@bp.post("/games/<int:gid>/delete")
def game_delete(gid):
    game = get_game(gid)
    require_confirm()
    db = get_db()
    removed = 0
    if request.form.get("delete_files"):
        for v in db.execute("SELECT folder FROM versions WHERE game_id=?", (gid,)).fetchall():
            removed += delete_folder(v["folder"])
    db.execute("DELETE FROM games WHERE id=?", (gid,))
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM game_tags)")
    db.commit()
    remove_cover(game["cover"])
    msg = f"Removed {game['title']} from the library"
    flash(msg + (f" and deleted {removed} folder(s) from the share." if removed else "."), "ok")
    return redirect(url_for("main.index"))


# -------------------------------------------------------------- versions

@bp.route("/games/<int:gid>/versions/new", methods=["GET", "POST"])
def version_new(gid):
    game = get_game(gid)
    if request.method == "POST":
        f = request.form
        label = f.get("version", "").strip()
        try:
            if not label:
                raise UserError("Version label is required.")
            if f.get("source") == "none":
                raise UserError("Choose an upload or a folder.")
            folder = materialize_source(f, game["title"], label)
            insert_version(gid, label, folder, f.get("notes", "").strip())
            if not game["cover"]:
                cover = auto_cover(folder)
                if cover:
                    get_db().execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
            get_db().commit()
        except (UserError, BadPath) as e:
            flash(str(e), "error")
            return render_template("version_new.html", game=game, form=f, folders=untracked_folders()), 400
        flash(f"Added version {label}.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("version_new.html", game=game, form={}, folders=untracked_folders())


@bp.route("/versions/<int:vid>/edit", methods=["GET", "POST"])
def version_edit(vid):
    v = get_version(vid)
    game = get_game(v["game_id"])
    if request.method == "POST":
        f = request.form
        label = f.get("version", "").strip()
        added = v["added_at"]
        day = f.get("added", "")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", day) and day != added[:10]:
            added = day + "T12:00:00+00:00"
        if not label:
            flash("Version label is required.", "error")
            return render_template("version_edit.html", v=v, game=game), 400
        db = get_db()
        db.execute("UPDATE versions SET label=?, notes=?, added_at=? WHERE id=?",
                   (label, f.get("notes", "").strip(), added, vid))
        db.execute("UPDATE games SET updated_at=max(updated_at, ?) WHERE id=?", (added, game["id"]))
        db.commit()
        flash("Version saved.", "ok")
        return redirect(url_for("main.game_detail", gid=game["id"]))
    return render_template("version_edit.html", v=v, game=game)


@bp.post("/versions/<int:vid>/delete")
def version_delete(vid):
    v = get_version(vid)
    require_confirm()
    deleted = delete_folder(v["folder"]) if request.form.get("delete_files") else False
    db = get_db()
    db.execute("DELETE FROM versions WHERE id=?", (vid,))
    db.commit()
    flash(f"Removed version {v['label']}" + (" and deleted its folder." if deleted else "."), "ok")
    return redirect(url_for("main.game_detail", gid=v["game_id"]))


@bp.post("/versions/<int:vid>/refresh")
def version_refresh(vid):
    v = get_version(vid)
    try:
        size = storage.folder_size(storage.resolve_in(games_dir(), v["folder"]))
    except BadPath:
        abort(404)
    get_db().execute("UPDATE versions SET size_bytes=? WHERE id=?", (size, vid))
    get_db().commit()
    return redirect(url_for("main.game_detail", gid=v["game_id"]))


@bp.get("/versions/<int:vid>/download")
def version_download(vid):
    v = get_version(vid)
    try:
        path = storage.resolve_in(games_dir(), v["folder"])
    except BadPath:
        abort(404)
    if not os.path.isdir(path):
        abort(404, "The folder is missing from the games share.")
    entries = os.listdir(path)
    if len(entries) == 1 and os.path.isfile(os.path.join(path, entries[0])) \
            and not os.path.islink(os.path.join(path, entries[0])):
        return send_file(os.path.join(path, entries[0]), as_attachment=True,
                         download_name=_clean_name(entries[0]))
    arcroot = _clean_name(os.path.basename(v["folder"]))
    name = arcroot + ".zip"
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return Response(
        storage.stream_folder_zip(path, arcroot),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-store",
        })


def _clean_name(name):
    """Safe for a Content-Disposition header: no control characters."""
    return re.sub(r"[\x00-\x1f\x7f]", "_", name) or "download"


# ----------------------------------------------------------------- upload

_last_cleanup = [0.0]


def _cleanup_tmp_hourly():
    import time
    if time.time() - _last_cleanup[0] > 3600:
        _last_cleanup[0] = time.time()
        storage.cleanup_stale(cfg("TMP_DIR"), games_dir())

@bp.post("/api/uploads")
def upload():
    """Raw request body streamed straight to /data/tmp (no multipart)."""
    raw = unquote(request.headers.get("X-Filename", ""))
    filename = os.path.basename(raw.replace("\\", "/")).strip()
    if not filename.lower().endswith(UPLOAD_EXTS):
        return jsonify(error="Only .zip or .html files can be uploaded."), 400
    _cleanup_tmp_hourly()
    upload_id = uuid.uuid4().hex
    data_path, meta_path = upload_paths(upload_id)
    part = data_path + ".part"
    size = 0
    try:
        with open(part, "wb") as out:
            while True:
                chunk = request.stream.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
        expected = request.content_length
        if expected is not None and size != expected:
            raise OSError("Upload was interrupted.")
        if size == 0:
            raise OSError("The file is empty.")
        os.replace(part, data_path)
        with open(meta_path, "w") as f:
            json.dump({"filename": filename, "size": size}, f)
    except OSError as e:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
        return jsonify(error=str(e)), 400
    return jsonify(id=upload_id, filename=filename, size=size)


@bp.post("/api/uploads/<upload_id>/delete")
def upload_cancel(upload_id):
    if UPLOAD_ID.match(upload_id):
        discard_upload(upload_id)
    return jsonify(ok=True)


# ------------------------------------------------------------------- scan

@bp.route("/scan", methods=["GET", "POST"])
def scan():
    db = get_db()
    existing = {r["title"].lower(): r for r in db.execute("SELECT id, title, platform FROM games")}
    if request.method == "POST":
        f = request.form
        available = set(untracked_folders())
        groups = {}
        try:
            count = min(max(int(f.get("count", "0")), 0), 10000)
        except ValueError:
            abort(400, "Bad scan form.")
        seen = set()
        for i in range(count):
            if not f.get(f"include-{i}"):
                continue
            folder = f.get(f"folder-{i}", "")
            if folder in seen:
                continue
            seen.add(folder)
            title = f.get(f"title-{i}", "").strip()
            label = f.get(f"label-{i}", "").strip()
            platform = f.get(f"platform-{i}", "other")
            if folder not in available or not title or not label:
                continue
            groups.setdefault(title.lower(), []).append(
                (title, label, folder, platform if platform in PLATFORMS else "other"))
        added_games = added_versions = 0
        for key, rows in groups.items():
            rows.sort(key=lambda r: storage.natural_key(r[1]))
            ts = now()
            if key in existing:
                gid = existing[key]["id"]
            else:
                title, _, folder, platform = rows[0]
                gid = db.execute(
                    "INSERT INTO games(title,platform,description,cover,created_at,updated_at,status_id,status_changed_at) "
                    "VALUES (?,?,'',NULL,?,?,?,?)",
                    (title, platform, ts, ts, default_status_id(), ts)).lastrowid
                added_games += 1
            for _title, label, folder, _platform in rows:
                path = storage.resolve_in(games_dir(), folder)
                insert_version(gid, label, folder, "", iso_from_ts(os.stat(path).st_mtime))
                added_versions += 1
            if not db.execute("SELECT cover FROM games WHERE id=?", (gid,)).fetchone()["cover"]:
                for _t, _l, folder, _p in reversed(rows):
                    cover = auto_cover(folder)
                    if cover:
                        db.execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
                        break
        db.commit()
        flash(f"Imported {added_versions} version(s), {added_games} new game(s).", "ok")
        return redirect(url_for("main.index"))

    rows = []
    for folder in untracked_folders():
        path = os.path.join(games_dir(), folder)
        title, label = storage.parse_folder_name(folder)
        match = existing.get(title.lower())
        if not label:
            label = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d")
        rows.append({
            "folder": folder, "title": match["title"] if match else title, "label": label,
            "platform": match["platform"] if match else storage.detect_platform(path),
            "existing": bool(match),
        })
    rows.sort(key=lambda r: (r["title"].lower(), storage.natural_key(r["label"])))
    return render_template("scan.html", rows=rows)


# --------------------------------------------------------------- settings

@bp.route("/settings/statuses", methods=["GET", "POST"])
def settings_statuses():
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        if action == "save":
            for st in all_statuses():
                label = " ".join(f.get(f"label-{st['id']}", st["label"]).split())[:40]
                color = f.get(f"color-{st['id']}", st["color"])
                try:
                    sort = int(f.get(f"sort-{st['id']}", st["sort"]))
                except ValueError:
                    sort = st["sort"]
                if not label or not COLOR_RE.match(color):
                    flash("Each status needs a name and a colour like #a1b2c3.", "error")
                    return render_template("settings_statuses.html", statuses=all_statuses()), 400
                db.execute("UPDATE statuses SET label=?, color=?, sort=? WHERE id=?",
                           (label, color.lower(), max(0, min(sort, 9999)), st["id"]))
            db.commit()
            flash("Statuses saved.", "ok")
        elif action == "add":
            label = " ".join(f.get("label", "").split())[:40]
            color = f.get("color", "#6aa6f8")
            if not label or not COLOR_RE.match(color):
                flash("Give the new status a name and a colour.", "error")
                return render_template("settings_statuses.html", statuses=all_statuses()), 400
            if db.execute("SELECT 1 FROM statuses WHERE label=? COLLATE NOCASE", (label,)).fetchone():
                flash(f"A status called {label} already exists.", "error")
                return render_template("settings_statuses.html", statuses=all_statuses()), 400
            top = db.execute("SELECT COALESCE(MAX(sort), 0) FROM statuses").fetchone()[0]
            db.execute("INSERT INTO statuses(key,label,color,sort,builtin) VALUES (?,?,?,?,0)",
                       (unique_status_key(label), label, color.lower(), top + 10))
            db.commit()
            flash(f"Added status {label}.", "ok")
        else:
            abort(400)
        return redirect(url_for("main.settings_statuses"))
    return render_template("settings_statuses.html", statuses=all_statuses())


@bp.post("/settings/statuses/<int:sid>/delete")
def settings_status_delete(sid):
    db = get_db()
    st = db.execute("SELECT * FROM statuses WHERE id=?", (sid,)).fetchone()
    if st is None:
        abort(404)
    if st["builtin"]:
        abort(400, "Built-in statuses can be renamed but not deleted.")
    require_confirm()
    pending = default_status_id()
    moved = 0
    for g in db.execute("SELECT id FROM games WHERE status_id=?", (sid,)).fetchall():
        set_status(g["id"], pending, source="system")
        moved += 1
    db.execute("DELETE FROM statuses WHERE id=?", (sid,))
    db.commit()
    pending_label = db.execute("SELECT label FROM statuses WHERE id=?", (pending,)).fetchone()["label"]
    flash(f"Deleted status {st['label']}"
          + (f"; {moved} game{'' if moved == 1 else 's'} moved to {pending_label}." if moved else "."), "ok")
    return redirect(url_for("main.settings_statuses"))


# ----------------------------------------------------------------- health

@bp.get("/healthz")
def healthz():
    """Used by the Docker HEALTHCHECK. Public, reveals only status."""
    get_db().execute("SELECT 1").fetchone()
    share_ok = os.path.isdir(games_dir()) and os.access(games_dir(), os.R_OK | os.X_OK)
    return jsonify(status="ok" if share_ok else "degraded", games_share=share_ok), 200


# ----------------------------------------------------------------- covers

@bp.get("/covers/<name>")
def cover(name):
    return send_from_directory(cfg("COVERS_DIR"), name, max_age=30 * 86400)


# ---------------------------------------------------------------- filters

def version_label(label):
    """'1.2' -> 'v1.2'; dates and words ('2026-11-25', 'beta') stay as they are."""
    label = label or ""
    if re.match(r"^\d+(\.\d+)*([a-z]\w*)?([-+][\w.]+)?$", label, re.I) and not re.match(r"^\d{4}-\d{2}-\d{2}$", label):
        return "v" + label
    return label


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def register_filters(app):
    app.jinja_env.filters["day"] = lambda s: (s or "")[:10]
    app.jinja_env.filters["when"] = lambda s: ((s or "")[:16].replace("T", " ") + " UTC") if s else ""
    app.jinja_env.filters["size"] = human_size
    app.jinja_env.filters["platform"] = lambda p: PLATFORMS.get(p, "Other")
    app.jinja_env.filters["vlabel"] = version_label
    app.jinja_env.globals["PLATFORMS"] = PLATFORMS

    @app.context_processor
    def inject_untracked():
        from flask import session
        if session.get("user") and request.endpoint not in ("static", "main.cover"):
            return {"untracked": len(untracked_folders())}
        return {}

    @app.template_global()
    def upload_exists(upload_id):
        return bool(upload_id and UPLOAD_ID.match(upload_id)
                    and os.path.exists(upload_paths(upload_id)[0]))

    @app.template_global()
    def hue(text):
        return sum(map(ord, text or "")) * 37 % 360

    @app.template_global()
    def initials(text):
        words = [w for w in re.split(r"\W+", text or "") if w]
        return "".join(w[0] for w in words[:2]).upper() or "?"

    @app.template_global()
    def url_with(**changes):
        args = request.args.to_dict()
        for k, val in changes.items():
            if val is None or val == "":
                args.pop(k, None)
            else:
                args[k] = val
        return url_for("main.index", **args)
