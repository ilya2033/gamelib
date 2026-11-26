"""Background job handlers: everything slow that touches the share."""
import json
import os
from datetime import datetime, timezone

from . import jobs, storage
from .db import get_db
from .jobs import handler
from .storage import BadPath, UnsafeZip
from . import views as v


def _fmt(n):
    return v.human_size(n)


# --------------------------------------------------------------- discovery

def build_row(folder, existing, rules, cache, cache_rows, seen_at):
    """One Scan row for an untracked version folder (platform cached by mtime)."""
    path = os.path.join(v.games_dir(), folder)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    c = cache.get(folder)
    if c is not None and c["mtime"] == mtime and c["platform"]:
        platform = c["platform"]
    else:
        platform = storage.detect_platform(path)
    cache_rows.append((folder, mtime, platform, c["size_bytes"] if c else None, seen_at))
    title, label = storage.parse_folder_name(os.path.basename(folder))
    match = existing.get(title.lower())
    if not label:
        label = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d")
    group, category = storage.location(folder)
    rule = rules.get(folder)
    return {
        "folder": folder, "title": match["title"] if match else title, "label": label,
        "platform": match["platform"] if match else platform, "existing": bool(match),
        "group": group, "category": category.split("/", 1)[1] if category else None,
        "rule": rule,
        # a "game" holding several game-like subfolders is probably a group
        "suspect": "group" if rule is None and storage.nested_versions(path) >= 2 else None,
    }


def save_cache(db, cache_rows):
    db.executemany("INSERT INTO scan_cache(path, mtime, platform, size_bytes, last_seen) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, platform=excluded.platform, "
                   "last_seen=excluded.last_seen", cache_rows)
    db.commit()


def _under(path, roots):
    return any(path == r or path.startswith(r + "/") for r in roots)


@handler("reclassify", lane="light", version=1)
def run_reclassify(ctx):
    """After a rule change: re-check only the changed folders' subtrees and
    patch the saved Scan result in place (no full walk)."""
    db = get_db()
    roots = sorted(set(ctx.params["paths"]), key=len)
    roots = [r for i, r in enumerate(roots) if not _under(r, roots[:i])]   # drop nested duplicates
    base = jobs.latest(["discover", "rescan"])
    result = (base or {}).get("result") or {"rows": [], "relinks": [], "group_rows": []}
    rules = v.folder_rules()
    tracked = v.tracked_folders()
    existing = {r["title"].lower(): r for r in db.execute("SELECT id, title, platform FROM games")}
    cache = {r["path"]: r for r in db.execute("SELECT * FROM scan_cache")}
    moved = {r["new"] for r in result.get("relinks", [])}
    removed = [r["folder"] for r in result["rows"] if _under(r["folder"], roots)] + \
              [g["path"] for g in result["group_rows"] if _under(g["path"], roots)]
    result["rows"] = [r for r in result["rows"] if not _under(r["folder"], roots)]
    result["group_rows"] = [g for g in result["group_rows"] if not _under(g["path"], roots)]
    new_rows, new_groups, cache_rows, seen_at = [], [], [], v.now()
    for root in roots:
        for kind, p in storage.walk_share(v.games_dir(), rules, v.cfg("SCAN_DEPTH"), tracked, start=root):
            ctx.check_cancel()
            if kind == "group":
                new_groups.append({"path": p, "rule": rules.get(p)})
            elif p not in tracked and p not in moved:
                ctx.progress(len(new_rows), 0, f"Checking {p}")
                row = build_row(p, existing, rules, cache, cache_rows, seen_at)
                if row:
                    new_rows.append(row)
    save_cache(db, cache_rows)
    result["rows"] = sorted(result["rows"] + new_rows,
                            key=lambda r: (r["title"].lower(), storage.natural_key(r["label"])))
    result["group_rows"] = sorted(result["group_rows"] + new_groups, key=lambda g: storage.natural_key(g["path"]))
    if base:
        con = db
        con.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(result), base["id"]))
        con.commit()
    v.invalidate_layout()
    return {"removed": removed, "rows": new_rows, "group_rows": new_groups, "roots": roots}

def discover(ctx, measure_changed=False):
    """Walk the share, detect platforms (cached by folder mtime), find moved
    folders. With measure_changed, also re-measure tracked versions whose
    folder changed since the last run (scheduled rescans)."""
    db = get_db()
    ctx.progress(0, 0, "Walking the share…", force=True)
    untracked, groups = v.share_layout(fresh=True)
    rules = v.folder_rules()
    existing = {r["title"].lower(): r for r in db.execute("SELECT id, title, platform FROM games")}
    cache = {r["path"]: r for r in db.execute("SELECT * FROM scan_cache")}
    relinks = v.relink_suggestions(untracked)
    moved = {r["new"] for r in relinks}
    seen_at = v.now()
    rows, cache_rows = [], []
    todo = [f for f in untracked if f not in moved]
    for i, folder in enumerate(todo):
        ctx.check_cancel()
        ctx.progress(i, len(todo), f"Checking {folder}")
        row = build_row(folder, existing, rules, cache, cache_rows, seen_at)
        if row:
            rows.append(row)
    save_cache(db, cache_rows)                    # one short write, after all the slow reading
    rows.sort(key=lambda r: (r["title"].lower(), storage.natural_key(r["label"])))

    measured = 0
    if measure_changed:
        versions = db.execute("SELECT id, folder, size_bytes FROM versions").fetchall()
        for i, ver in enumerate(versions):
            ctx.check_cancel()
            ctx.progress(i, len(versions), f"Checking sizes: {ver['folder']}")
            try:
                path = storage.resolve_no_links(v.games_dir(), ver["folder"])
                mtime = os.stat(path).st_mtime
            except (BadPath, OSError):
                continue
            c = cache.get(ver["folder"])
            if ver["size_bytes"] >= 0 and c is not None and c["mtime"] == mtime:
                continue
            size = storage.folder_size(path, check=ctx.check_cancel)
            db.execute("UPDATE versions SET size_bytes=? WHERE id=?", (size, ver["id"]))
            db.execute("INSERT INTO scan_cache(path, mtime, size_bytes, last_seen) VALUES (?,?,?,?) "
                       "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size_bytes=excluded.size_bytes, "
                       "last_seen=excluded.last_seen", (ver["folder"], mtime, size, seen_at))
            db.commit()
            measured += 1
    db.execute("DELETE FROM scan_cache WHERE last_seen < date('now', '-30 days')")
    db.commit()
    ctx.progress(len(todo), len(todo), f"Found {len(rows)} new folder(s)", force=True)
    return {"rows": rows, "relinks": relinks, "measured": measured,
            "group_rows": [{"path": p, "rule": rules.get(p)} for p in groups]}


@handler("discover")
def run_discover(ctx):
    return discover(ctx)


@handler("rescan")
def run_rescan(ctx):
    return discover(ctx, measure_changed=True)


# ------------------------------------------------------------------ import

@handler("import", retry_on_restart=False)
def run_import(ctx):
    db = get_db()
    rows = ctx.params["rows"]
    ctx.progress(0, len(rows), "Checking the share…", force=True)
    untracked = v.share_layout(fresh=True)[0]
    moved = {r["new"] for r in v.relink_suggestions(untracked)}
    available = set(untracked) - moved             # moved folders are relinked, never imported as new
    existing = {r["title"].lower(): r for r in db.execute("SELECT id, title FROM games")}
    groups = {}
    for r in rows:
        if r["folder"] in available:
            groups.setdefault(r["title"].lower(), []).append(r)
    done = added_games = added_versions = 0
    total = sum(len(g) for g in groups.values())
    for key, grp in groups.items():
        ctx.check_cancel()
        grp.sort(key=lambda r: storage.natural_key(r["label"]))
        measured = []                                 # slow part first, with no transaction open
        for r in grp:
            ctx.progress(done, total, f"Measuring {r['folder']}")
            path = storage.resolve_no_links(v.games_dir(), r["folder"])
            measured.append((r, storage.folder_size(path, check=ctx.check_cancel), os.stat(path).st_mtime))
            done += 1
        if key in existing:
            gid = existing[key]["id"]
        else:
            gid = v.create_game(grp[0]["title"], grp[0]["platform"])
            added_games += 1
        for r, size, mtime in measured:
            v.insert_version(gid, r["label"], r["folder"], "", v.iso_from_ts(mtime), size=size)
            added_versions += 1
        db.commit()
        if not db.execute("SELECT cover FROM games WHERE id=?", (gid,)).fetchone()["cover"]:
            for r in reversed(grp):
                cover = v.auto_cover(r["folder"], r["title"])
                if cover:
                    db.execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
                    break
        db.commit()                                   # per game: a cancel keeps finished games
    skipped = len(rows) - total
    jobs.enqueue("discover", {}, "Scan games share", exclusive=True)
    msg = f"Imported {added_versions} version(s), {added_games} new game(s)."
    if skipped:
        msg += f" Skipped {skipped} folder(s) that were already imported, moved or gone."
    return {"redirect": "/", "message": msg}


# ------------------------------------------------------------------- sizes

@handler("measure")
def run_measure(ctx):
    db = get_db()
    vids = ctx.params["vids"]
    for i, vid in enumerate(vids):
        ctx.check_cancel()
        row = db.execute("SELECT folder FROM versions WHERE id=?", (vid,)).fetchone()
        if row is None:
            continue
        ctx.progress(i, len(vids), f"Measuring {row['folder']}")
        try:
            path = storage.resolve_no_links(v.games_dir(), row["folder"])
        except BadPath:
            continue
        size = storage.folder_size(path, check=ctx.check_cancel)
        db.execute("UPDATE versions SET size_bytes=? WHERE id=?", (size, vid))
        db.commit()
    return {"message": f"Measured {len(vids)} folder(s)."}


# ------------------------------------------------------------------ upload

@handler("install", retry_on_restart=False)
def run_install(ctx):
    p = ctx.params
    data_path, meta_path = v.upload_paths(p["upload_id"])
    try:
        if not os.path.exists(data_path):
            raise ValueError("The upload has expired. Upload the file again.")
        with open(meta_path) as f:
            meta = json.load(f)

        def progress(done, total):
            ctx.check_cancel()
            ctx.progress(done, total, f"Unpacking {_fmt(done)} of {_fmt(total)}")

        try:
            folder = storage.install_upload(data_path, meta["filename"], v.games_dir(), f"{p['title']} v{p['label']}",
                                            v.cfg("MAX_UNCOMPRESSED_BYTES"), p.get("parent", ""), progress)
        except UnsafeZip as e:
            raise ValueError(f"Rejected zip: {e}") from None
    except BaseException:
        _install_cleanup(p)
        raise
    v.discard_upload(p["upload_id"])
    db = get_db()
    size = storage.folder_size(storage.resolve_no_links(v.games_dir(), folder))
    if p["mode"] == "new_game":
        gid = v.create_game(p["title"], p["platform"], p.get("description", ""), p.get("cover"),
                            p.get("status"), p.get("tags", ""), p.get("categories", ""))
    else:
        gid = p["gid"]
        if db.execute("SELECT 1 FROM games WHERE id=?", (gid,)).fetchone() is None:
            raise ValueError(f"The game was deleted while uploading. The files are in {folder} on the share; "
                             "use Scan to import them.")
    v.insert_version(gid, p["label"], folder, p.get("notes", ""), size=size)
    if not db.execute("SELECT cover FROM games WHERE id=?", (gid,)).fetchone()["cover"]:
        cover = v.auto_cover(folder, p["title"])
        if cover:
            db.execute("UPDATE games SET cover=? WHERE id=?", (cover, gid))
    db.commit()
    v.invalidate_layout()
    what = p["title"] if p["mode"] == "new_game" else f"version {p['label']}"
    return {"redirect": f"/games/{gid}", "message": f"Added {what}."}


def _install_cleanup(p):
    v.discard_upload(p["upload_id"])
    if p.get("mode") == "new_game":
        v.remove_cover(p.get("cover"))


jobs.CLEANUP["install"] = _install_cleanup


# ------------------------------------------------------------------ delete

@handler("delete")
def run_delete(ctx):
    folders = ctx.params["folders"]
    removed = 0
    for i, folder in enumerate(folders):
        ctx.check_cancel()
        ctx.progress(i, len(folders), f"Deleting {folder}")
        removed += v.delete_folder(folder)
    return {"message": f"Deleted {removed} folder(s) from the share."}


# ----------------------------------------------------------------- artwork

@handler("artwork")
def run_artwork(ctx):
    db = get_db()
    games = db.execute("SELECT id, title FROM games WHERE cover IS NULL").fetchall()
    found = 0
    for i, g in enumerate(games):
        ctx.check_cancel()
        ctx.progress(i, len(games), f"Looking in {g['title']}")
        for ver in db.execute("SELECT folder FROM versions WHERE game_id=? ORDER BY added_at DESC, id DESC",
                              (g["id"],)).fetchall():
            cover = v.auto_cover(ver["folder"], g["title"])
            if cover:
                db.execute("UPDATE games SET cover=? WHERE id=?", (cover, g["id"]))
                db.commit()
                found += 1
                break
    return {"redirect": "/", "message": f"Found artwork for {found} of {len(games)} game(s) without a cover."}
