"""1.4.0 library tools: missing folders, export/import, saved views, bulk
edit, SteamGridDB covers, duplicates, old versions and storage stats.

Routes live on the main blueprint (imported by create_app before the
blueprint is registered)."""
import io
import json
import os
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode

from flask import Response, abort, flash, redirect, render_template, request, url_for

from . import jobs, play, steamgriddb as sgdb
from .db import get_db
from .jobs import handler
from .storage import PLATFORMS
from . import views as v
from .views import UserError, bp

EXPORT_FORMAT = "gamelib-export"
EXPORT_VERSION = 1


# ------------------------------------------------------------- settings

def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    if value:
        db.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))
    else:
        db.execute("DELETE FROM settings WHERE key=?", (key,))


def sgdb_key():
    return os.environ.get("STEAMGRIDDB_API_KEY", "").strip() or get_setting("steamgriddb_key")


# ----------------------------------------------------------------- hub

@bp.get("/tools")
def tools_hub():
    db = get_db()
    counts = db.execute(
        "SELECT (SELECT COUNT(*) FROM versions WHERE missing_since IS NOT NULL) missing, "
        "(SELECT COUNT(*) FROM games) games, "
        "(SELECT COUNT(*) FROM versions) versions, "
        "(SELECT COALESCE(SUM(CASE WHEN size_bytes > 0 THEN size_bytes END), 0) FROM versions) bytes").fetchone()
    return render_template("tools.html", counts=counts, dupes=len(duplicate_groups()),
                           old=old_versions_summary(), has_sgdb=bool(sgdb_key()))


# ------------------------------------------------------- missing folders

@bp.get("/tools/missing")
def tools_missing():
    db = get_db()
    rows = db.execute("SELECT v.*, g.title FROM versions v JOIN games g ON g.id=v.game_id "
                      "WHERE v.missing_since IS NOT NULL ORDER BY g.title COLLATE NOCASE, v.added_at").fetchall()
    untracked = v.untracked_folders()
    by_name = defaultdict(list)
    for p in untracked:
        by_name[os.path.basename(p).lower()].append(p)
    items = [dict(r, suggestions=by_name.get(os.path.basename(r["folder"]).lower(), [])) for r in rows]
    return render_template("tools_missing.html", items=items, untracked=untracked)


@bp.post("/tools/missing/check")
def tools_missing_check():
    n = v.mark_missing()
    if n is None:
        flash("The games share looks empty or unreadable, so nothing was marked. Check the /games mapping.", "error")
    else:
        flash(f"{n} version folder(s) are missing." if n else "Every tracked folder is on the share.", "ok")
    return redirect(url_for("main.tools_missing"))


@bp.post("/tools/missing/relink")
def tools_missing_relink():
    ver = v.get_version(int(request.form.get("vid", "0") or 0))
    new = request.form.get("new", "")
    try:
        v.check_untracked_folder(new)
    except UserError:
        flash("Pick a folder from the list of untracked folders on the share.", "error")
        return redirect(url_for("main.tools_missing"))
    db = get_db()
    db.execute("UPDATE versions SET folder=?, size_bytes=-1, missing_since=NULL WHERE id=?", (new, ver["id"]))
    v.sync_folder_categories(ver["game_id"])
    v.invalidate_layout()
    db.commit()
    v.measure_later([ver["id"]])
    flash(f"Relinked {ver['folder']} → {new}.", "ok")
    return redirect(url_for("main.tools_missing"))


@bp.post("/tools/missing/forget")
def tools_missing_forget():
    """Remove missing versions from the library (their folders are already gone).
    Games stay, even with no versions left."""
    v.require_confirm()
    ids = [int(i) for i in request.form.getlist("vid") if i.isdigit()]
    db = get_db()
    rows = db.execute(f"SELECT id, game_id FROM versions WHERE missing_since IS NOT NULL AND id IN "
                      f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []
    for r in rows:
        db.execute("DELETE FROM versions WHERE id=?", (r["id"],))
    for gid in {r["game_id"] for r in rows}:
        v.sync_folder_categories(gid)
    v.invalidate_layout()
    db.commit()
    flash(f"Forgot {len(rows)} missing version(s).", "ok")
    return redirect(url_for("main.tools_missing"))


# -------------------------------------------------------- export / import

def export_data():
    db = get_db()
    statuses = [dict(r) for r in db.execute("SELECT key, label, color, sort, builtin FROM statuses ORDER BY sort, id")]
    games = []
    for g in db.execute("SELECT g.*, s.key AS status_key FROM games g LEFT JOIN statuses s ON s.id=g.status_id "
                        "ORDER BY g.title COLLATE NOCASE, g.id"):
        games.append({
            "title": g["title"], "platform": g["platform"], "description": g["description"],
            "status": g["status_key"], "status_changed_at": g["status_changed_at"],
            "rating": g["rating"], "favorite": bool(g["favorite"]), "hours": g["hours"], "notes": g["notes"],
            "tags": v.game_tags(g["id"]),
            "categories": [c["name"] for c in v.manual_categories(g["id"])],
            "created_at": g["created_at"], "updated_at": g["updated_at"],
            "versions": [{"folder": r["folder"], "label": r["label"], "notes": r["notes"], "added_at": r["added_at"],
                          "entry": r["entry"], "position": r["position"]}
                         for r in db.execute("SELECT * FROM versions WHERE game_id=? ORDER BY added_at, id",
                                             (g["id"],))],
        })
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "app_version": v.__version__,
            "exported_at": v.now(), "statuses": statuses, "games": games}


@bp.get("/tools/export")
def tools_export():
    body = json.dumps(export_data(), ensure_ascii=False, indent=1)
    name = f"gamelib-export-{v.now()[:10]}.json"
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _str(x, limit):
    return str(x or "").strip()[:limit]


def import_data(data):
    """Merge an export into this library (last write wins). A game is matched
    by any of its version folders, then by exact title. Unmatched games are
    added when at least one of their folders is on the share and untracked.
    Returns counts."""
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        raise UserError("This isn't a GameLib export file.")
    if int(data.get("version", 0) or 0) > EXPORT_VERSION:
        raise UserError("This export was made by a newer GameLib. Update this one first.")
    db = get_db()
    stats = {"updated": 0, "added": 0, "skipped": 0, "statuses": 0}
    new_vids = []
    for s in data.get("statuses") or []:
        key, label = _str(s.get("key"), 40), _str(s.get("label"), 40)
        if not key or not label or v.status_by_key(key):
            continue
        color = s.get("color") if v.COLOR_RE.match(str(s.get("color", ""))) else "#8a93a3"
        db.execute("INSERT INTO statuses(key,label,color,sort,builtin) VALUES (?,?,?,?,0)",
                   (key, label, color, int(s.get("sort") or 100)))
        stats["statuses"] += 1
    by_folder = {r["folder"]: r["game_id"] for r in db.execute("SELECT folder, game_id FROM versions")}
    by_title = defaultdict(list)
    for r in db.execute("SELECT id, title FROM games"):
        by_title[r["title"]].append(r["id"])
    for g in data.get("games") or []:
        if not isinstance(g, dict) or not _str(g.get("title"), 200):
            stats["skipped"] += 1
            continue
        title = _str(g["title"], 200)
        folders = [_str(x.get("folder"), 1000) for x in g.get("versions") or [] if isinstance(x, dict)]
        gid = next((by_folder[f] for f in folders if f in by_folder), None)
        if gid is None and len(by_title.get(title, [])) == 1:
            gid = by_title[title][0]
        if gid is None:
            gid = _import_new_game(g, title, new_vids)
            if gid is None:
                stats["skipped"] += 1
                continue
            stats["added"] += 1
            for f in folders:
                by_folder.setdefault(f, gid)
        else:
            stats["updated"] += 1
        _apply_fields(gid, g)
    db.commit()
    v.measure_later(new_vids)          # after the commit, so the job sees the rows
    return stats


def _import_new_game(g, title, new_vids):
    usable = []
    for x in g.get("versions") or []:
        f = _str(x.get("folder"), 1000) if isinstance(x, dict) else ""
        try:
            v.check_untracked_folder(f)
        except (UserError, v.BadPath):
            continue
        usable.append(x)
    if not usable:
        return None
    platform = g.get("platform") if g.get("platform") in PLATFORMS else "other"
    gid = v.create_game(title, platform, _str(g.get("description"), 20000), None, None, "", "")
    for x in usable:
        new_vids.append(v.insert_version(gid, _str(x.get("label"), 80) or "1", _str(x.get("folder"), 1000),
                                         _str(x.get("notes"), 5000), added_at=_str(x.get("added_at"), 40) or None))
    return gid


def _apply_fields(gid, g):
    db = get_db()
    game = v.get_game(gid)
    rating = g.get("rating")
    rating = rating if isinstance(rating, int) and 1 <= rating <= 5 else game["rating"] if rating is None else None
    hours = g.get("hours")
    hours = round(float(hours), 1) if isinstance(hours, (int, float)) and 0 <= hours <= 100000 else game["hours"]
    db.execute("UPDATE games SET rating=?, favorite=?, hours=?, notes=?, description=? WHERE id=?",
               (rating, 1 if g.get("favorite") else 0, hours,
                _str(g.get("notes"), 20000) if "notes" in g else game["notes"],
                _str(g.get("description"), 20000) or game["description"], gid))
    st = v.status_by_key(_str(g.get("status"), 40)) if g.get("status") else None
    if st is not None:
        v.set_status(gid, st["id"], source="import")
    if isinstance(g.get("tags"), list):
        v.set_tags(gid, v.parse_tags(",".join(str(t) for t in g["tags"])))
    if isinstance(g.get("categories"), list):
        v.set_manual_categories(gid, v.parse_categories(",".join(str(c) for c in g["categories"])))
    for x in g.get("versions") or []:           # hand-picked order
        if isinstance(x, dict) and isinstance(x.get("position"), int) and 0 <= x["position"] < 100000:
            db.execute("UPDATE versions SET position=? WHERE game_id=? AND folder=? AND position IS NULL",
                       (x["position"], gid, _str(x.get("folder"), 1000)))
    for x in g.get("versions") or []:           # chosen start files
        if isinstance(x, dict) and isinstance(x.get("entry"), str) and x["entry"]:
            try:
                entry = play.clean_entry(x["entry"])
            except play.EntryError:
                continue
            db.execute("UPDATE versions SET entry=? WHERE game_id=? AND folder=? AND entry IS NULL",
                       (entry, gid, _str(x.get("folder"), 1000)))


@bp.post("/tools/import")
def tools_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose an export file (.json) to import.", "error")
        return redirect(url_for("main.tools_hub"))
    try:
        try:
            data = json.loads(f.stream.read(32 * 1024 * 1024 + 1).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise UserError("The file isn't valid JSON.") from None
        stats = import_data(data)
    except UserError as e:
        get_db().rollback()
        flash(str(e), "error")
        return redirect(url_for("main.tools_hub"))
    msg = f"Import done: {stats['updated']} game(s) updated, {stats['added']} added"
    if stats["skipped"]:
        msg += f", {stats['skipped']} skipped (their folders aren't on this share)"
    if stats["statuses"]:
        msg += f", {stats['statuses']} status(es) added"
    flash(msg + ".", "ok")
    return redirect(url_for("main.tools_hub"))


# ------------------------------------------------------------ saved views

def clean_query(qs):
    pairs = [(k, val) for k, val in parse_qsl(qs or "") if k in v.LIBRARY_KEYS and val]
    return urlencode(pairs)


@bp.post("/views")
def view_save():
    name = " ".join(request.form.get("name", "").split())[:60]
    query = clean_query(request.form.get("query", ""))
    if not name:
        flash("Give the view a name.", "error")
    elif not query:
        flash("Pick some filters first, then save them as a view.", "error")
    else:
        db = get_db()
        db.execute("INSERT INTO saved_views(name, query) VALUES (?,?) "
                   "ON CONFLICT(name) DO UPDATE SET query=excluded.query", (name, query))
        db.commit()
        flash(f"Saved view “{name}”.", "ok")
    return redirect(url_for("main.index") + ("?" + query if query else ""))


@bp.post("/views/<int:view_id>/delete")
def view_delete(view_id):
    db = get_db()
    db.execute("DELETE FROM saved_views WHERE id=?", (view_id,))
    db.commit()
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/")
                    and not request.form.get("back", "").startswith("//") else url_for("main.index"))


# --------------------------------------------------------------- bulk edit

BULK_ACTIONS = ("status", "fav", "unfav", "rating", "tag-add", "tag-remove", "cat-add", "cat-remove")


@bp.post("/games/bulk")
def games_bulk():
    f = request.form
    ids = sorted({int(i) for i in f.getlist("ids") if i.isdigit()})
    action, value = f.get("action", ""), f.get("value", "").strip()
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("main.index")
    db = get_db()
    ids = [r["id"] for r in db.execute(f"SELECT id FROM games WHERE id IN ({','.join('?' * len(ids))})", ids)] \
        if ids else []
    if not ids:
        flash("Select at least one game.", "error")
        return redirect(back)
    if action.startswith("status:"):
        action, value = "status", action.partition(":")[2]
    if action not in BULK_ACTIONS:
        abort(400, "Unknown bulk action.")
    try:
        if action == "status":
            st = v.status_by_key(value)
            if st is None:
                raise UserError("Unknown status.")
            changed = sum(v.set_status(gid, st["id"]) for gid in ids)
            msg = f"Status set to {st['label']} for {changed} game(s)."
        elif action in ("fav", "unfav"):
            db.execute(f"UPDATE games SET favorite=? WHERE id IN ({','.join('?' * len(ids))})",
                       [1 if action == "fav" else 0, *ids])
            msg = f"{'Added' if action == 'fav' else 'Removed'} {len(ids)} game(s) {'to' if action == 'fav' else 'from'} favourites."
        elif action == "rating":
            if value and not (value.isdigit() and 1 <= int(value) <= 5):
                raise UserError("Rating must be 1 to 5, or empty to clear it.")
            db.execute(f"UPDATE games SET rating=? WHERE id IN ({','.join('?' * len(ids))})",
                       [int(value) if value else None, *ids])
            msg = f"Rating {'set to ' + value + '★' if value else 'cleared'} for {len(ids)} game(s)."
        else:
            names = v.parse_tags(value)
            if not names:
                raise UserError("Type a tag or category name.")
            for gid in ids:
                if action.startswith("tag"):
                    cur = v.game_tags(gid)
                    new = cur + names if action == "tag-add" else \
                        [t for t in cur if t.lower() not in {n.lower() for n in names}]
                    v.set_tags(gid, v.parse_tags(",".join(new)))
                else:
                    cur = [c["name"] for c in v.manual_categories(gid)]
                    new = cur + names if action == "cat-add" else \
                        [c for c in cur if c.lower() not in {n.lower() for n in names}]
                    v.set_manual_categories(gid, v.parse_categories(",".join(new)))
            kind = "tag" if action.startswith("tag") else "category"
            msg = f"{'Added' if action.endswith('add') else 'Removed'} {kind} {', '.join(names)} " \
                  f"{'to' if action.endswith('add') else 'from'} {len(ids)} game(s)."
    except UserError as e:
        db.rollback()
        flash(str(e), "error")
        return redirect(back)
    db.execute(f"UPDATE games SET updated_at=? WHERE id IN ({','.join('?' * len(ids))})", [v.now(), *ids])
    db.commit()
    flash(msg, "ok")
    return redirect(back)


# ------------------------------------------------------------- SteamGridDB

@bp.route("/settings/integrations", methods=["GET", "POST"])
def settings_integrations():
    env_key = bool(os.environ.get("STEAMGRIDDB_API_KEY", "").strip())
    if request.method == "POST":
        key = request.form.get("steamgriddb_key", "").strip()
        if request.form.get("clear"):
            key = ""
        if key and not re.fullmatch(r"[A-Za-z0-9]{16,128}", key):
            flash("That doesn't look like a SteamGridDB API key.", "error")
        else:
            set_setting("steamgriddb_key", key)
            get_db().commit()
            flash("SteamGridDB key saved." if key else "SteamGridDB key removed.", "ok")
        return redirect(url_for("main.settings_integrations"))
    return render_template("settings_integrations.html", has_key=bool(get_setting("steamgriddb_key")),
                           env_key=env_key)


@bp.get("/games/<int:gid>/steamgriddb")
def game_sgdb(gid):
    game = v.get_game(gid)
    key = sgdb_key()
    if not key:
        flash("Add a SteamGridDB API key in Settings → Integrations first.", "error")
        return redirect(url_for("main.game_edit", gid=gid))
    term = request.args.get("q", game["title"]).strip() or game["title"]
    sid = request.args.get("sid", "")
    results, covers, error = [], [], None
    try:
        if sid.isdigit():
            covers = sgdb.grids(key, int(sid))
        else:
            results = sgdb.search(key, term)
    except sgdb.SGDBError as e:
        error = str(e)
    return render_template("game_sgdb.html", game=game, term=term, sid=sid, results=results,
                           covers=covers, error=error, sid_name=request.args.get("name", ""))


@bp.post("/games/<int:gid>/steamgriddb")
def game_sgdb_pick(gid):
    game = v.get_game(gid)
    try:
        data = sgdb.download(request.form.get("url", ""))
        cover = v.save_cover(io.BytesIO(data), SGDB_PREFIX)
    except (sgdb.SGDBError, UserError) as e:
        flash(str(e), "error")
        return redirect(url_for("main.game_sgdb", gid=gid))
    db = get_db()
    db.execute("UPDATE games SET cover=?, updated_at=? WHERE id=?", (cover, v.now(), gid))
    db.commit()
    v.remove_cover(game["cover"])
    flash("Cover set from SteamGridDB.", "ok")
    return redirect(url_for("main.game_detail", gid=gid))


SGDB_PREFIX = "sgdb-"   # not "auto-": a folder-artwork refresh leaves these alone


@bp.post("/tools/steamgriddb")
def tools_sgdb():
    if not sgdb_key():
        flash("Add a SteamGridDB API key in Settings → Integrations first.", "error")
        return redirect(url_for("main.tools_hub"))
    scope = request.form.get("scope", "missing")
    if scope not in ("missing", "auto"):
        abort(400, "Unknown scope.")
    label = "SteamGridDB covers for games without a cover" if scope == "missing" else \
        "SteamGridDB covers for games without a cover or with an icon card"
    job_id = jobs.enqueue("steamgriddb", {"scope": scope}, label, exclusive=True)
    return redirect(url_for("main.job_page", job_id=job_id))


# Light lane: network-bound, doesn't touch the share. Only ever started from
# the button above, and never resumed after a restart.
@handler("steamgriddb", lane="light", retry_on_restart=False, version=1)
def run_sgdb(ctx):
    key = sgdb_key()
    if not key:
        raise UserError("No SteamGridDB API key is set.")
    where = "cover IS NULL" if ctx.params.get("scope") == "missing" else "cover IS NULL OR cover LIKE 'auto-%'"
    db = get_db()
    games = db.execute(f"SELECT id, title, cover FROM games WHERE {where} ORDER BY title COLLATE NOCASE").fetchall()
    found = 0
    for i, g in enumerate(games):
        ctx.check_cancel()
        ctx.progress(i, len(games), f"Searching {g['title']}")
        try:
            hits = sgdb.search(key, g["title"])
            covers = sgdb.grids(key, hits[0]["id"]) if hits else []
            if not covers:
                continue
            cover = v.save_cover(io.BytesIO(sgdb.download(covers[0]["url"])), SGDB_PREFIX)
        except UserError:
            continue
        except sgdb.SGDBError as e:
            if "API key" in str(e):
                raise
            continue
        db.execute("UPDATE games SET cover=? WHERE id=?", (cover, g["id"]))
        db.commit()
        v.remove_cover(g["cover"])
        found += 1
    return {"redirect": "/", "message": f"Found SteamGridDB covers for {found} of {len(games)} game(s)."}


# -------------------------------------------------------------- duplicates

def norm_title(t):
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", (t or "").lower())
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"[^a-z0-9]+", "", t)


def duplicate_groups():
    groups = defaultdict(list)
    for g in get_db().execute(
            "SELECT g.id, g.title, g.platform, g.cover, g.created_at, "
            "(SELECT COUNT(*) FROM versions WHERE game_id=g.id) nversions, "
            "(SELECT COALESCE(SUM(CASE WHEN size_bytes > 0 THEN size_bytes END), 0) FROM versions "
            " WHERE game_id=g.id) bytes FROM games g ORDER BY g.created_at, g.id"):
        key = norm_title(g["title"])
        if key:
            groups[key].append(dict(g))
    return [grp for grp in groups.values() if len(grp) > 1]


@bp.get("/tools/duplicates")
def tools_duplicates():
    return render_template("tools_duplicates.html", groups=duplicate_groups())


@bp.post("/games/<int:gid>/merge")
def game_merge(gid):
    """Move another game's versions, tags and categories into this one, then
    delete the other game (files are not touched)."""
    keep = v.get_game(gid)
    other_id = int(request.form.get("other", "0") or 0)
    if other_id == gid:
        abort(400, "Can't merge a game into itself.")
    other = v.get_game(other_id)
    db = get_db()
    db.execute("UPDATE versions SET game_id=? WHERE game_id=?", (gid, other_id))
    db.execute("INSERT OR IGNORE INTO saves(game_id, key, value, updated_at) "
               "SELECT ?, key, value, updated_at FROM saves WHERE game_id=?", (gid, other_id))
    db.execute("INSERT OR IGNORE INTO game_tags(game_id, tag_id, source) "
               "SELECT ?, tag_id, source FROM game_tags WHERE game_id=?", (gid, other_id))
    db.execute("INSERT OR IGNORE INTO game_categories(game_id, category_id, source) "
               "SELECT ?, category_id, source FROM game_categories WHERE game_id=? AND source != 'folder'",
               (gid, other_id))
    notes = "\n\n".join(n for n in (keep["notes"], other["notes"]) if n)
    hours = (keep["hours"] or 0) + (other["hours"] or 0) if (keep["hours"] or other["hours"]) else None
    cover = keep["cover"] or other["cover"]
    db.execute("UPDATE games SET rating=?, favorite=?, hours=?, notes=?, cover=?, description=?, updated_at=? "
               "WHERE id=?", (keep["rating"] or other["rating"], keep["favorite"] or other["favorite"], hours, notes,
                              cover, keep["description"] or other["description"], v.now(), gid))
    db.execute("DELETE FROM games WHERE id=?", (other_id,))
    v.sync_folder_categories(gid)
    v.prune_categories()
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM game_tags)")
    v.invalidate_layout()
    db.commit()
    if other["cover"] and other["cover"] != cover:
        v.remove_cover(other["cover"])
    flash(f"Merged {other['title']} into {keep['title']}.", "ok")
    back = request.form.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//")
                    else url_for("main.game_detail", gid=gid))


# ------------------------------------------------------------ old versions

def old_versions():
    """Every version except each game's default (top) one, biggest first per game."""
    out = []
    db = get_db()
    for g in db.execute("SELECT g.id, g.title FROM games g WHERE (SELECT COUNT(*) FROM versions "
                        "WHERE game_id=g.id) > 1 ORDER BY g.title COLLATE NOCASE"):
        vers = db.execute("SELECT * FROM versions WHERE game_id=? ORDER BY (position IS NULL) DESC, position, added_at DESC, id DESC",
                          (g["id"],)).fetchall()
        olds = [dict(r) for r in vers[1:]]
        out.append({"id": g["id"], "title": g["title"], "latest": vers[0], "old": olds,
                    "bytes": sum(r["size_bytes"] for r in olds if r["size_bytes"] > 0)})
    out.sort(key=lambda x: -x["bytes"])
    return out


def old_versions_summary():
    rows = old_versions()
    return {"games": len(rows), "versions": sum(len(r["old"]) for r in rows), "bytes": sum(r["bytes"] for r in rows)}


@bp.get("/tools/versions")
def tools_versions():
    rows = old_versions()
    return render_template("tools_versions.html", rows=rows, summary=old_versions_summary())


@bp.post("/tools/versions/delete")
def tools_versions_delete():
    v.require_confirm()
    ids = [int(i) for i in request.form.getlist("vid") if i.isdigit()]
    delete_files = bool(request.form.get("delete_files"))
    db = get_db()
    latest = {r["game_id"]: r["id"] for r in db.execute(
        "SELECT game_id, id FROM versions v WHERE id = (SELECT id FROM versions WHERE game_id=v.game_id "
        "ORDER BY (position IS NULL) DESC, position, added_at DESC, id DESC LIMIT 1)")}
    folders, removed, games = [], 0, set()
    for vid in ids:
        r = db.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        if r is None or latest.get(r["game_id"]) == vid:      # never the default one
            continue
        if delete_files and v.deletable(r["folder"]):
            folders.append(r["folder"])
        db.execute("DELETE FROM versions WHERE id=?", (vid,))
        removed += 1
        games.add(r["game_id"])
    for gid in games:
        v.sync_folder_categories(gid)
    v.invalidate_layout()
    db.commit()
    if folders:
        jobs.enqueue("delete", {"folders": folders}, f"Delete {len(folders)} old version folder(s)")
    flash(f"Removed {removed} old version(s)" +
          (f"; deleting {len(folders)} folder(s) from the share in the background." if folders else "."), "ok")
    return redirect(url_for("main.tools_versions"))


# ----------------------------------------------------------------- storage

@bp.get("/tools/storage")
def tools_storage():
    db = get_db()
    size = "COALESCE(SUM(CASE WHEN v.size_bytes > 0 THEN v.size_bytes END), 0)"
    by_status = db.execute(
        f"SELECT s.label, s.color, COUNT(DISTINCT g.id) games, {size} bytes FROM statuses s "
        f"LEFT JOIN games g ON g.status_id=s.id LEFT JOIN versions v ON v.game_id=g.id "
        f"GROUP BY s.id ORDER BY bytes DESC").fetchall()
    by_platform = [dict(r, label=PLATFORMS.get(r["platform"], "Other")) for r in db.execute(
        f"SELECT g.platform, COUNT(DISTINCT g.id) games, {size} bytes FROM games g "
        f"LEFT JOIN versions v ON v.game_id=g.id GROUP BY g.platform ORDER BY bytes DESC")]
    by_group = db.execute(
        f"SELECT COALESCE(c.name, '(no group)') name, COUNT(DISTINCT g.id) games, {size} bytes FROM games g "
        f"LEFT JOIN game_categories gc ON gc.game_id=g.id AND gc.category_id IN (SELECT id FROM categories "
        f"WHERE is_group=1) LEFT JOIN categories c ON c.id=gc.category_id "
        f"LEFT JOIN versions v ON v.game_id=g.id GROUP BY c.id ORDER BY bytes DESC").fetchall()
    largest = db.execute(
        f"SELECT g.id, g.title, COUNT(v.id) nversions, {size} bytes FROM games g "
        f"JOIN versions v ON v.game_id=g.id GROUP BY g.id ORDER BY bytes DESC LIMIT 25").fetchall()
    totals = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN size_bytes > 0 THEN size_bytes END), 0) bytes, "
        "SUM(size_bytes < 0) unmeasured, COUNT(*) versions FROM versions").fetchone()
    try:
        st = os.statvfs(v.games_dir())
        disk = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        disk = None
    return render_template("tools_storage.html", by_status=by_status, by_platform=by_platform,
                           by_group=by_group, largest=largest, totals=totals, disk=disk)

