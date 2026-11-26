import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_file,
                   send_from_directory, url_for)
from PIL import Image, UnidentifiedImageError

import colorsys
import io

from . import artwork, storage
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


# The share layout (which folders are versions, which are groups) is cached
# briefly: walking nested folders on every page load would keep waking
# spun-down Unraid disks. Pages that act on the share pass fresh=True, and
# every write the app makes to the share or the versions table invalidates it.
_layout = {"key": None, "at": 0.0, "value": None}
_layout_lock = threading.Lock()


def invalidate_layout():
    with _layout_lock:
        _layout["at"] = 0.0


def folder_rules():
    return {r["path"]: r["kind"] for r in get_db().execute("SELECT path, kind FROM folder_rules")}


def share_layout(fresh=False):
    """(untracked version folders, group folders), both share-relative."""
    key = (games_dir(), cfg("DB_PATH"), cfg("SCAN_DEPTH"))
    ttl = cfg("LAYOUT_CACHE_SECONDS")
    with _layout_lock:
        if not fresh and _layout["key"] == key and time.monotonic() - _layout["at"] < ttl:
            return _layout["value"]
    tracked = tracked_folders()
    games, groups = [], []
    for kind, p in storage.walk_share(games_dir(), folder_rules(), cfg("SCAN_DEPTH"), tracked):
        (games if kind == "game" else groups).append(p)
    value = ([p for p in games if p not in tracked], groups)
    with _layout_lock:
        _layout.update(key=key, at=time.monotonic(), value=value)
    return value


def untracked_folders(fresh=True):
    return share_layout(fresh)[0]


def group_folders(fresh=True):
    return share_layout(fresh)[1]


# ------------------------------------------------------------ categories

def ensure_category_path(path):
    """Create the folder-derived category chain for 'PC/Strategy' and return
    the id of the last one. Level 1 is a group."""
    db, parent, cur, cid = get_db(), None, "", None
    for i, name in enumerate(path.split("/")):
        cur = f"{cur}/{name}" if cur else name
        row = db.execute("SELECT id FROM categories WHERE path=?", (cur,)).fetchone()
        if row:
            cid = row["id"]
        else:
            cid = db.execute("INSERT INTO categories(parent_id,name,is_group,path) VALUES (?,?,?,?)",
                             (parent, name, 1 if i == 0 else 0, cur)).lastrowid
        parent = cid
    return cid


def prune_categories():
    """Drop folder-derived categories with no games and no children, and
    manual categories no game uses any more."""
    db = get_db()
    while True:
        n = db.execute(
            "DELETE FROM categories WHERE id NOT IN (SELECT category_id FROM game_categories) "
            "AND id NOT IN (SELECT parent_id FROM categories WHERE parent_id IS NOT NULL)").rowcount
        if not n:
            break


def sync_folder_categories(gid):
    """Group/category of a game = location of its latest version on the share."""
    db = get_db()
    db.execute("DELETE FROM game_categories WHERE game_id=? AND source='folder'", (gid,))
    latest = db.execute("SELECT folder FROM versions WHERE game_id=? ORDER BY added_at DESC, id DESC LIMIT 1",
                        (gid,)).fetchone()
    if latest:
        group, category = storage.location(latest["folder"])
        for path in filter(None, (group, category)):
            db.execute("INSERT OR REPLACE INTO game_categories(game_id,category_id,source) VALUES (?,?,'folder')",
                       (gid, ensure_category_path(path)))
    prune_categories()


def parse_categories(text):
    return parse_tags(text)  # same rules: comma separated, trimmed, de-duplicated


def manual_categories(gid):
    return get_db().execute(
        "SELECT c.id, c.name FROM categories c JOIN game_categories gc ON gc.category_id=c.id "
        "WHERE gc.game_id=? AND c.path IS NULL ORDER BY c.name COLLATE NOCASE", (gid,)).fetchall()


def set_manual_categories(gid, names, source="user"):
    db = get_db()
    db.execute("DELETE FROM game_categories WHERE game_id=? AND category_id IN "
               "(SELECT id FROM categories WHERE path IS NULL)", (gid,))
    for name in names:
        row = db.execute("SELECT id FROM categories WHERE path IS NULL AND name=? COLLATE NOCASE",
                         (name,)).fetchone()
        cid = row["id"] if row else db.execute(
            "INSERT INTO categories(parent_id,name,is_group,path) VALUES (NULL,?,0,NULL)", (name,)).lastrowid
        db.execute("INSERT OR IGNORE INTO game_categories(game_id,category_id,source) VALUES (?,?,?)",
                   (gid, cid, source))
    prune_categories()


def game_location(gid):
    """(group row, deepest folder category row) for the detail page."""
    rows = get_db().execute(
        "SELECT c.* FROM categories c JOIN game_categories gc ON gc.category_id=c.id "
        "WHERE gc.game_id=? AND gc.source='folder' ORDER BY length(c.path)", (gid,)).fetchall()
    group = next((r for r in rows if r["is_group"]), None)
    cat = next((r for r in rows if not r["is_group"]), None)
    return group, cat


def category_chain(cat):
    """Ancestors of a category, root first (for breadcrumbs)."""
    chain, db = [], get_db()
    while cat is not None:
        chain.append(cat)
        cat = db.execute("SELECT * FROM categories WHERE id=?", (cat["parent_id"],)).fetchone() \
            if cat["parent_id"] else None
    return list(reversed(chain))


def library_tree(selected_group=None):
    """Sidebar data: groups with game counts, and a flat, indented list of
    categories (descendant games counted once) under the selected group, or
    under every group, plus manual categories."""
    db = get_db()
    cats = {r["id"]: dict(r) for r in db.execute("SELECT * FROM categories")}
    members = defaultdict(set)
    for r in db.execute("SELECT game_id, category_id FROM game_categories"):
        members[r["category_id"]].add(r["game_id"])
    children = defaultdict(list)
    for c in cats.values():
        children[c["parent_id"]].append(c)
    for lst in children.values():
        lst.sort(key=lambda c: storage.natural_key(c["name"]))

    def games_under(cid):
        out = set(members[cid])
        for ch in children[cid]:
            out |= games_under(ch["id"])
        return out

    groups = [dict(c, n=len(games_under(c["id"])), has_icon=bool(group_icon_path(c["path"])))
              for c in children[None] if c["is_group"]]
    flat = []

    def walk(cid, depth):
        for ch in children[cid]:
            flat.append(dict(ch, depth=depth, n=len(games_under(ch["id"]))))
            walk(ch["id"], depth + 1)

    if selected_group:
        walk(selected_group, 0)
    else:
        for g in groups:
            if children[g["id"]]:
                flat.append(dict(g, depth=0, n=g["n"], heading=True))
                walk(g["id"], 1)
    manual = [dict(c, n=len(members[c["id"]])) for c in children[None] if c["path"] is None]
    return groups, flat, manual


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
        db.execute("INSERT OR IGNORE INTO game_tags(game_id, tag_id) VALUES (?,?)", (gid, tid))
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


def title_hue(text):
    return sum(map(ord, text or "")) * 37 % 360


def save_icon_cover(src, title):
    """Render a small square icon centred on a 600x800 card coloured from the
    title, instead of stretching it into a blurry crop."""
    try:
        with Image.open(src) as img:
            img.load()
            icon = img.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return None
    h = title_hue(title) / 360
    top = tuple(int(c * 255) for c in colorsys.hls_to_rgb(h, 0.30, 0.45))
    bottom = tuple(int(c * 255) for c in colorsys.hls_to_rgb((h + 40 / 360) % 1, 0.13, 0.50))
    W, H = 600, 800
    card = Image.new("RGBA", (W, H))
    for y in range(H):
        t = y / (H - 1)
        card.paste(tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,), (0, y, W, y + 1))
    side = 360
    scale = side / max(icon.size)
    size = (max(1, round(icon.width * scale)), max(1, round(icon.height * scale)))
    resample = Image.NEAREST if max(icon.size) <= 64 else Image.LANCZOS   # keep pixel art crisp
    icon = icon.resize(size, resample)
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 90), ((W - size[0]) // 2 + 6, 220 + 10, (W + size[0]) // 2 + 6, 220 + size[1] + 10),
                 icon.getchannel("A"))
    card = Image.alpha_composite(card, shadow)
    card.alpha_composite(icon, ((W - size[0]) // 2, 220))
    name = uuid.uuid4().hex + ".webp"
    card.convert("RGB").save(os.path.join(cfg("COVERS_DIR"), name), "WEBP", quality=88)
    return name


def auto_cover(folder, title=""):
    """Cover from the game folder: a cover image, an engine/HTML icon, or the
    icon inside the game's .exe. None if nothing usable is found."""
    try:
        found = artwork.find_artwork(storage.resolve_in(games_dir(), folder), title)
    except BadPath:
        return None
    if not found:
        return None
    kind, src = found
    try:
        if kind == "cover":
            return save_cover(src)
        return save_icon_cover(io.BytesIO(src) if kind == "exe" else src, title)
    except UserError:
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
        storage.resolve_no_links(games_dir(), folder)
        return folder
    parent = destination_from_form(form)
    upload_id = form.get("upload_id", "")
    if not UPLOAD_ID.match(upload_id):
        raise UserError("Upload a file first and wait for it to finish.")
    data_path, meta_path = upload_paths(upload_id)
    if not os.path.exists(data_path):
        raise UserError("That upload has expired. Upload the file again.")
    with open(meta_path) as f:
        meta = json.load(f)
    try:
        created = storage.make_dirs_in(games_dir(), parent)
        for p in created:  # folders made for the destination are groups, whatever their name
            get_db().execute("INSERT OR REPLACE INTO folder_rules(path, kind) VALUES (?, 'group')", (p,))
        folder = storage.install_upload(data_path, meta["filename"], games_dir(),
                                        f"{title} v{label}", cfg("MAX_UNCOMPRESSED_BYTES"), parent)
        invalidate_layout()
        return folder
    except UnsafeZip as e:
        raise UserError(f"Rejected zip: {e}") from None
    finally:
        discard_upload(upload_id)


def destination_from_form(form):
    """Share subfolder an upload goes into: an existing group folder (or the
    top level), optionally plus a new subfolder typed by the user."""
    dest = form.get("dest", "")
    if dest and dest not in group_folders():
        raise UserError("Pick a destination folder from the list.")
    extra = storage.clean_subpath(form.get("dest_new", ""))
    parent = "/".join(p for p in (dest, extra) if p)
    if parent.count("/") + 1 >= cfg("SCAN_DEPTH"):
        raise UserError(f"That destination is too deep: scans look {cfg('SCAN_DEPTH')} folder levels deep, "
                        "so the game folder would not be found. Pick a shallower destination.")
    return parent


def default_destination(gid):
    """New versions go next to the game's latest version by default."""
    row = get_db().execute("SELECT folder FROM versions WHERE game_id=? ORDER BY added_at DESC, id DESC LIMIT 1",
                           (gid,)).fetchone()
    parent = row["folder"].rpartition("/")[0] if row else ""
    return parent if parent in group_folders() else ""


def insert_version(gid, label, folder, notes="", added_at=None):
    path = storage.resolve_in(games_dir(), folder)
    added_at = added_at or now()
    get_db().execute(
        "INSERT INTO versions(game_id,label,folder,notes,size_bytes,added_at) VALUES (?,?,?,?,?,?)",
        (gid, label, folder, notes, storage.folder_size(path), added_at))
    get_db().execute("UPDATE games SET updated_at=max(updated_at, ?) WHERE id=?", (added_at, gid))
    sync_folder_categories(gid)
    invalidate_layout()


def delete_folder(folder):
    """Delete a version folder from the share. Refuses anything reached
    through a symlink, so a swapped-in link can't redirect the delete."""
    try:
        path = storage.resolve_no_links(games_dir(), folder)
    except BadPath:
        return False
    if os.path.isdir(path):
        shutil.rmtree(path)
        invalidate_layout()
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
    group = request.args.get("group", "")
    category = request.args.get("category", "")
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
    if group == "none":
        where.append("NOT EXISTS (SELECT 1 FROM game_categories gc JOIN categories c ON c.id=gc.category_id "
                     "WHERE gc.game_id=g.id AND c.is_group=1)")
    elif group.isdigit():
        where.append("EXISTS (SELECT 1 FROM game_categories gc WHERE gc.game_id=g.id AND gc.category_id=?)")
        args.append(int(group))
    if category.isdigit():
        where.append("EXISTS (SELECT 1 FROM game_categories gc WHERE gc.game_id=g.id AND gc.category_id IN ("
                     "WITH RECURSIVE sub(id) AS (SELECT ? UNION SELECT c.id FROM categories c "
                     "JOIN sub ON c.parent_id=sub.id) SELECT id FROM sub))")
        args.append(int(category))
    sql = (
        "SELECT g.*, s.key AS status_key, s.label AS status_label, s.color AS status_color, "
        "(SELECT label FROM versions v WHERE v.game_id=g.id ORDER BY v.added_at DESC, v.id DESC LIMIT 1) AS latest, "
        "(SELECT COUNT(*) FROM versions v WHERE v.game_id=g.id) AS nversions, "
        "(SELECT group_concat(t.name, '\x1f') FROM game_tags gt JOIN tags t ON t.id=gt.tag_id WHERE gt.game_id=g.id) AS tagstr, "
        "(SELECT c.name FROM game_categories gc JOIN categories c ON c.id=gc.category_id "
        " WHERE gc.game_id=g.id AND c.is_group=1 LIMIT 1) AS group_name "
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
    group_id = int(group) if group.isdigit() else None
    groups, cat_tree, manual_cats = library_tree(group_id)
    ungrouped = db.execute(
        "SELECT COUNT(*) FROM games g WHERE NOT EXISTS (SELECT 1 FROM game_categories gc "
        "JOIN categories c ON c.id=gc.category_id WHERE gc.game_id=g.id AND c.is_group=1)").fetchone()[0]
    category_row = db.execute("SELECT * FROM categories WHERE id=?", (int(category),)).fetchone() \
        if category.isdigit() else None
    active_group = group
    if not group and category_row is not None and category_row["path"]:
        root = db.execute("SELECT id FROM categories WHERE path=?", (category_row["path"].split("/")[0],)).fetchone()
        active_group = str(root["id"]) if root else ""
    status_counts = db.execute(
        "SELECT s.key, s.label, s.color, COUNT(g.id) n FROM statuses s "
        "LEFT JOIN games g ON g.status_id=s.id GROUP BY s.id ORDER BY s.sort, s.id").fetchall()
    return render_template(
        "index.html", games=games, q=q, platform=platform, tag=tag, status=status, sort=sort,
        direction=direction, platform_counts=platform_counts, tag_counts=tag_counts,
        status_counts=status_counts, group=group, category=category, groups=groups,
        cat_tree=cat_tree, manual_cats=manual_cats, ungrouped=ungrouped, category_row=category_row,
        active_group=active_group,
        total=total)


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
    group, cat = game_location(gid)
    return render_template("game.html", game=game, versions=versions, tags=game_tags(gid),
                           status=status, statuses=all_statuses(), history=history,
                           group=group, cat_chain=category_chain(cat)[1:] if cat else [],
                           manual_cats=manual_categories(gid))


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
                cover = auto_cover(folder, title)
            ts = now()
            db = get_db()
            gid = db.execute(
                "INSERT INTO games(title,platform,description,cover,created_at,updated_at,status_id,status_changed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (title, platform, form.get("description", "").strip(), cover, ts, ts,
                 status_from_form(form.get("status")), ts)).lastrowid
            set_tags(gid, parse_tags(form.get("tags")))
            set_manual_categories(gid, parse_categories(form.get("categories")))
            if folder:
                insert_version(gid, label, folder, form.get("notes", "").strip(), ts)
            db.commit()
        except (UserError, BadPath) as e:
            remove_cover(cover)
            flash(str(e), "error")
            return render_template("game_new.html", form=form, folders=untracked_folders(),
                                   statuses=all_statuses(), destinations=group_folders()), 400
        flash(f"Added {title}.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("game_new.html", form={}, folders=untracked_folders(), statuses=all_statuses(),
                           destinations=group_folders())


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
            return render_template("game_edit.html", game=game, tags=parse_tags(f.get("tags")),
                                   categories=parse_categories(f.get("categories"))), 400
        cover = game["cover"]
        if new_cover or f.get("remove_cover"):
            remove_cover(cover)
            cover = new_cover
        db = get_db()
        db.execute("UPDATE games SET title=?, platform=?, description=?, cover=?, updated_at=? WHERE id=?",
                   (title, platform, f.get("description", "").strip(), cover, now(), gid))
        set_tags(gid, parse_tags(f.get("tags")))
        set_manual_categories(gid, parse_categories(f.get("categories")))
        db.commit()
        flash("Saved.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("game_edit.html", game=game, tags=game_tags(gid),
                           categories=[c["name"] for c in manual_categories(gid)])


@bp.post("/games/<int:gid>/artwork")
def game_artwork(gid):
    """Look for artwork in the game's version folders (newest first) and use
    the first one found as the cover, replacing the current one."""
    game = get_game(gid)
    db = get_db()
    for v in db.execute("SELECT folder FROM versions WHERE game_id=? ORDER BY added_at DESC, id DESC", (gid,)):
        cover = auto_cover(v["folder"], game["title"])
        if cover:
            remove_cover(game["cover"])
            db.execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
            db.commit()
            flash("Found artwork in the game folder.", "ok")
            return redirect(url_for("main.game_detail", gid=gid))
    flash("No cover, icon or .exe icon found in this game's folders.", "error")
    return redirect(url_for("main.game_edit", gid=gid))


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
    prune_categories()
    invalidate_layout()
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
                cover = auto_cover(folder, game["title"])
                if cover:
                    get_db().execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
            get_db().commit()
        except (UserError, BadPath) as e:
            flash(str(e), "error")
            return render_template("version_new.html", game=game, form=f, folders=untracked_folders(),
                                   destinations=group_folders()), 400
        flash(f"Added version {label}.", "ok")
        return redirect(url_for("main.game_detail", gid=gid))
    return render_template("version_new.html", game=game, form={"dest": default_destination(gid)},
                           folders=untracked_folders(), destinations=group_folders())


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
        sync_folder_categories(game["id"])
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
    sync_folder_categories(v["game_id"])
    invalidate_layout()
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
        untracked = untracked_folders()
        moved = {r["new"] for r in relink_suggestions(untracked)}
        available = set(untracked) - moved   # moved folders are relinked, never imported as new
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
                    cover = auto_cover(folder, _t)
                    if cover:
                        db.execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
                        break
        db.commit()
        flash(f"Imported {added_versions} version(s), {added_games} new game(s).", "ok")
        return redirect(url_for("main.index"))

    untracked, groups = share_layout(fresh=True)
    rules = folder_rules()
    relinks = relink_suggestions(untracked)
    moved = {r["new"] for r in relinks}
    rows = []
    for folder in untracked:
        if folder in moved:
            continue  # offered under "Moved folders" instead
        path = os.path.join(games_dir(), folder)
        title, label = storage.parse_folder_name(os.path.basename(folder))
        match = existing.get(title.lower())
        if not label:
            label = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc).strftime("%Y-%m-%d")
        group, category = storage.location(folder)
        rows.append({
            "folder": folder, "title": match["title"] if match else title, "label": label,
            "platform": match["platform"] if match else storage.detect_platform(path),
            "existing": bool(match), "group": group,
            "category": category.split("/", 1)[1] if category else None,
            "rule": rules.get(folder),
        })
    rows.sort(key=lambda r: (r["title"].lower(), storage.natural_key(r["label"])))
    return render_template("scan.html", rows=rows, relinks=relinks,
                           group_rows=[{"path": p, "rule": rules.get(p)} for p in groups])


def relink_suggestions(untracked):
    """Tracked versions whose folder is gone, paired with an untracked folder
    of the same name elsewhere on the share (it was probably moved)."""
    by_name = defaultdict(list)
    for p in untracked:
        by_name[os.path.basename(p).lower()].append(p)
    out = []
    for v in get_db().execute("SELECT v.*, g.title FROM versions v JOIN games g ON g.id=v.game_id"):
        try:
            gone = not os.path.isdir(storage.resolve_in(games_dir(), v["folder"]))
        except BadPath:
            gone = True
        if gone:
            for new in by_name.get(os.path.basename(v["folder"]).lower(), []):
                out.append({"vid": v["id"], "title": v["title"], "label": v["label"], "old": v["folder"], "new": new})
    return out


@bp.post("/scan/relink")
def scan_relink():
    v = get_version(int(request.form.get("vid", "0") or 0))
    new = request.form.get("new", "")
    if new not in untracked_folders():
        abort(400, "That folder is not an untracked folder on the share.")
    path = storage.resolve_no_links(games_dir(), new)
    db = get_db()
    db.execute("UPDATE versions SET folder=?, size_bytes=? WHERE id=?", (new, storage.folder_size(path), v["id"]))
    sync_folder_categories(v["game_id"])
    invalidate_layout()
    db.commit()
    flash(f"Relinked version {v['label']}: {v['folder']} → {new}.", "ok")
    return redirect(url_for("main.scan"))


@bp.post("/scan/rule")
def scan_rule():
    """Override automatic detection: 'game:<path>', 'group:<path>' or 'auto:<path>'."""
    kind, _, path = request.form.get("rule", "").partition(":")
    untracked, groups = share_layout(fresh=True)
    if kind not in ("game", "group", "auto") or path not in set(untracked) | set(groups):
        abort(400, "Unknown folder or rule.")
    storage.resolve_no_links(games_dir(), path)
    db = get_db()
    if kind == "auto":
        db.execute("DELETE FROM folder_rules WHERE path=?", (path,))
    else:
        db.execute("INSERT OR REPLACE INTO folder_rules(path, kind) VALUES (?, ?)", (path, kind))
    db.commit()
    invalidate_layout()
    flash({"game": f"{path} is now treated as a game folder.",
           "group": f"{path} is now treated as a group folder; its subfolders are scanned.",
           "auto": f"{path} uses automatic detection again."}[kind], "ok")
    return redirect(url_for("main.scan"))


@bp.get("/groups/<int:cid>/icon")
def group_icon(cid):
    """folder.jpg / folder.png inside a group or category folder."""
    row = get_db().execute("SELECT path FROM categories WHERE id=? AND path IS NOT NULL", (cid,)).fetchone()
    p = group_icon_path(row["path"]) if row else None
    if not p:
        abort(404)
    return send_file(p, max_age=3600)


def group_icon_path(rel):
    try:
        base = storage.resolve_no_links(games_dir(), rel)
    except BadPath:
        return None
    for name in ("folder.jpg", "folder.jpeg", "folder.png", "folder.webp"):
        p = os.path.join(base, name)
        if os.path.isfile(p) and not os.path.islink(p):
            return p
    return None


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
        if session.get("user") and request.endpoint not in ("static", "main.cover", "main.group_icon"):
            return {"untracked": len(untracked_folders(fresh=False))}
        return {}

    @app.template_global()
    def upload_exists(upload_id):
        return bool(upload_id and UPLOAD_ID.match(upload_id)
                    and os.path.exists(upload_paths(upload_id)[0]))

    app.jinja_env.globals["hue"] = title_hue

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
