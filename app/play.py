"""Start files per version, and HTML games played in the browser.

A game runs in a sandboxed iframe (an opaque origin, so its scripts can't
reach GameLib's cookies or pages). Its files are served under a signed,
expiring URL instead of the login session. A small script is put in front
of every HTML page; it replaces localStorage with one backed by the server,
so saves are kept per game in the database (and in its backups) instead of
in one browser."""
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import re
import time

from flask import (Blueprint, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, send_file, url_for)

from . import artwork, storage
from .db import get_db
from .storage import BadPath

bp = Blueprint("play", __name__)

HTML_EXTS = (".html", ".htm")
# what can be picked as a version's start file
RUN_EXTS = HTML_EXTS + (".exe", ".bat", ".cmd", ".lnk", ".sh", ".command", ".x86_64", ".x86", ".appimage",
                        ".jar", ".apk", ".swf", ".py", ".love")
HTML_DIRS = ("www", "html", "html5", "web", "game", "build", "dist", "public")   # RPG Maker, exports...
SKIP_DIRS = {"__macosx", "node_modules", ".git"}
WALK_DEPTH = 4
WALK_FILES = 20000          # entries looked at when listing start files
MAX_CANDIDATES = 300
TOKEN_DAYS = 2              # a play link works until the end of tomorrow (UTC), stable within a day
MAX_INJECT = 64 * 1024 * 1024    # bigger HTML is served as is (no saves)
MAX_SAVE_REQUEST = 32 * 1024 * 1024
MAX_GAME_SAVES = 200 * 1024 * 1024
MAX_KEY = 1024
SAVES_FORMAT = "gamelib-saves"
RESERVED = "__gamelib__"
SANDBOX = ("allow-scripts allow-pointer-lock allow-forms allow-modals allow-popups allow-downloads "
           "allow-orientation-lock allow-presentation")
EXTRA_TYPES = {".wasm": "application/wasm", ".data": "application/octet-stream", ".pck": "application/octet-stream",
               ".mem": "application/octet-stream", ".unityweb": "application/octet-stream",
               ".json": "application/json", ".js": "text/javascript", ".mjs": "text/javascript",
               ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".webm": "video/webm", ".rpgmvp": "application/octet-stream",
               ".rpgmvo": "application/octet-stream", ".rpgmvm": "application/octet-stream"}
ENCODINGS = {".br": "br", ".gz": "gzip"}    # Unity/Godot pre-compressed builds: game.wasm.br etc.


class EntryError(Exception):
    pass


# ------------------------------------------------------------ start files

def is_html(rel):
    return bool(rel) and rel.lower().endswith(HTML_EXTS)


def kind_of(rel):
    if not rel:
        return None
    return "html" if is_html(rel) else "exe" if rel.lower().endswith(".exe") else "file"


def clean_entry(rel):
    """A relative path inside a version folder, with / separators."""
    rel = (rel or "").strip().replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or len(rel) > 1000 or "\x00" in rel or ".." in parts or rel.startswith("/"):
        raise EntryError("That start file is not inside the version folder.")
    return "/".join(parts)


def folder_path(folder):
    return storage.resolve_in(current_app.config["GAMES_DIR"], folder)


def entry_file(base, rel):
    """Real path of a start file inside `base`, or None."""
    try:
        p = storage.resolve_in(base, clean_entry(rel))
    except (BadPath, EntryError):
        return None
    return p if os.path.isfile(p) else None


def candidates(base):
    """Files that can be picked as a version's start file: pages first, then
    executables near the top, helpers (installers, crash handlers) last."""
    found, seen = [], 0
    base = os.path.realpath(base)
    for root, dirs, files in os.walk(base):
        depth = 0 if root == base else os.path.relpath(root, base).count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS
                         and not os.path.islink(os.path.join(root, d)))
        if depth >= WALK_DEPTH:
            dirs[:] = []
        for name in files:
            seen += 1
            if name.lower().endswith(RUN_EXTS):
                rel = os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/")
                helper = bool(artwork.EXE_SKIP.match(name))
                found.append((helper, not is_html(name), depth, rel.lower(), rel))
        if seen > WALK_FILES:
            break
    return [f[-1] for f in sorted(found)[:MAX_CANDIDATES]]


def auto_entry(base, title=""):
    """The start file GameLib guesses: index.html (top level, or in www/,
    html/... one level down), a lone page, else the game's .exe."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    files = [n for n in names if os.path.isfile(os.path.join(base, n))]
    for n in files:
        if n.lower() in ("index.html", "index.htm"):
            return n
    pages = [n for n in files if is_html(n)]
    if len(pages) == 1:
        return pages[0]
    dirs = [n for n in names if os.path.isdir(os.path.join(base, n)) and not n.startswith(".")
            and not os.path.islink(os.path.join(base, n))]
    dirs.sort(key=lambda d: (d.lower() not in HTML_DIRS, d.lower()))
    for d in dirs[:20]:
        for idx in ("index.html", "index.htm"):
            if os.path.isfile(os.path.join(base, d, idx)):
                return d + "/" + idx
    exe = artwork.pick_exe(base, title)
    if exe:
        return os.path.relpath(exe, os.path.realpath(base)).replace(os.sep, "/")
    return None


def resolve_entry(version, title=""):
    """(start file, automatic?) for a version row; (None, True) if nothing fits.
    A chosen file that is gone falls back to the automatic guess."""
    try:
        base = folder_path(version["folder"])
    except BadPath:
        return None, True
    if not os.path.isdir(base):
        return None, True
    if version["entry"] and entry_file(base, version["entry"]):
        return version["entry"], False
    return auto_entry(base, title), True


def entry_for_folder(folder):
    """The chosen start file of the version in `folder` (for artwork), or None."""
    row = get_db().execute("SELECT entry FROM versions WHERE folder=?", (folder,)).fetchone()
    return row["entry"] if row else None


# ------------------------------------------------------------ play links

def _key():
    return hashlib.sha256(("play:" + current_app.secret_key).encode()).digest()


def _sign(msg):
    return hmac.new(_key(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(vid):
    exp = (int(time.time()) // 86400 + TOKEN_DAYS) * 86400
    msg = f"{vid}.{exp}"
    return f"{msg}.{_sign(msg)}"


def check_token(token):
    """The version row a play link is for; 404 for bad or expired links."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.([0-9a-f]{32})", token or "")
    if not m or not hmac.compare_digest(_sign(f"{m[1]}.{m[2]}"), m[3]) or int(m[2]) < time.time():
        abort(404)
    row = get_db().execute("SELECT v.*, g.title FROM versions v JOIN games g ON g.id=v.game_id WHERE v.id=?",
                           (int(m[1]),)).fetchone()
    if row is None:
        abort(404)
    return row


def file_url(token, rel):
    return url_for("play.files", token=token, path=rel)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    resp.frameable = True
    return resp


def _sandboxed(resp):
    # Also applies when a page is opened outside the player (in its own tab).
    resp.headers["Content-Security-Policy"] = "sandbox " + SANDBOX
    return _cors(resp)


# ------------------------------------------------------------ saves

def load_saves(gid):
    return {r["key"]: r["value"] for r in get_db().execute(
        "SELECT key, value FROM saves WHERE game_id=? ORDER BY key", (gid,))}


def saves_summary(gid):
    r = get_db().execute("SELECT COUNT(*) AS n, COALESCE(SUM(length(key) + length(value)), 0) AS bytes, "
                         "MAX(updated_at) AS at FROM saves WHERE game_id=?", (gid,)).fetchone()
    return dict(r)


def apply_saves(gid, changes):
    """Apply {"clear": bool, "del": [keys], "set": {key: value}} in that order.
    Raises EntryError (nothing applied) when it is malformed or too big."""
    if not isinstance(changes, dict):
        raise EntryError("Malformed saves.")
    sets, dels = changes.get("set") or {}, changes.get("del") or []
    if not isinstance(sets, dict) or not isinstance(dels, list):
        raise EntryError("Malformed saves.")
    for k, val in sets.items():
        if len(k) > MAX_KEY or not isinstance(val, str):
            raise EntryError("Malformed saves.")
    if any(not isinstance(k, str) for k in dels):
        raise EntryError("Malformed saves.")
    db = get_db()
    now = _now()
    if changes.get("clear"):
        db.execute("DELETE FROM saves WHERE game_id=?", (gid,))
    db.executemany("DELETE FROM saves WHERE game_id=? AND key=?", [(gid, k) for k in dels])
    db.executemany("INSERT INTO saves(game_id, key, value, updated_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(game_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   [(gid, k, val, now) for k, val in sets.items()])
    total = db.execute("SELECT COALESCE(SUM(length(key) + length(value)), 0) FROM saves WHERE game_id=?",
                       (gid,)).fetchone()[0]
    if total > MAX_GAME_SAVES:
        db.rollback()
        raise EntryError("Saves for this game would exceed the size limit.")
    db.commit()
    return now


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ routes

@bp.get("/play/<int:vid>")
def player(vid):
    row = get_db().execute("SELECT v.*, g.title FROM versions v JOIN games g ON g.id=v.game_id WHERE v.id=?",
                           (vid,)).fetchone()
    if row is None:
        abort(404)
    entry, auto = resolve_entry(row, row["title"])
    if not is_html(entry):
        abort(404, "This version has no web page to play. Pick an .html start file in Edit version.")
    token = make_token(vid)
    return render_template("player.html", v=row, entry=entry, auto=auto, src=file_url(token, entry),
                           saves=saves_summary(row["game_id"]))


@bp.get("/play/t/<token>/", defaults={"path": ""})
@bp.get("/play/t/<token>/<path:path>")
def files(token, path):
    row = check_token(token)
    try:
        base = folder_path(row["folder"])
        target = storage.resolve_in(base, path) if path else base
    except BadPath:
        abort(404)
    if os.path.isdir(target):
        target = next((os.path.join(target, n) for n in ("index.html", "index.htm")
                       if os.path.isfile(os.path.join(target, n))), None)
        if target is None:
            abort(404)
    if not os.path.isfile(target):
        abort(404)
    name = os.path.basename(target).lower()
    if name.endswith(HTML_EXTS) and os.path.getsize(target) <= MAX_INJECT:
        with open(target, "rb") as f:
            page = inject(f.read(), url_for("play.boot", token=token))
        resp = Response(page, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return _sandboxed(resp)
    stem, ext = os.path.splitext(name)
    encoding = ENCODINGS.get(ext) if os.path.splitext(stem)[1] else None
    inner = os.path.splitext(stem)[1] if encoding else ext
    mime = EXTRA_TYPES.get(inner) or mimetypes.guess_type("x" + inner)[0] or "application/octet-stream"
    resp = send_file(target, mimetype=mime, conditional=True, max_age=3600, etag=True)
    if encoding:
        resp.headers["Content-Encoding"] = encoding
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return _sandboxed(resp)


_HEAD = re.compile(rb"<head\b[^>]*>", re.I)
_CHARSET = re.compile(rb"<meta\b[^>]*charset[^>]*>", re.I)
_DOCTYPE = re.compile(rb"^\s*(?:<!doctype[^>]*>\s*)?(?:<html\b[^>]*>)?", re.I)


def inject(page, boot_url):
    """Put the save script in front of the page's own scripts. After a
    <meta charset> if there is one in the head, so it stays in the first 1 KB."""
    tag = b'<script src="' + boot_url.encode() + b'"></script>'
    sample = page[:4096]
    for rx in (_CHARSET, _HEAD):
        m = rx.search(sample)
        if m:
            return page[:m.end()] + tag + page[m.end():]
    m = _DOCTYPE.match(page)
    return page[:m.end()] + tag + page[m.end():]


@bp.get("/play/t/<token>/" + RESERVED + "/boot.js")
def boot(token):
    row = check_token(token)
    data = json.dumps(load_saves(row["game_id"]), ensure_ascii=True).replace("<", "\\u003c")
    js = BOOT_JS.replace("__SAVES__", data).replace("__URL__", json.dumps(url_for("play.save", token=token)))
    resp = Response(js, mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return _sandboxed(resp)


@bp.route("/play/t/<token>/" + RESERVED + "/save", methods=["POST", "OPTIONS"])
def save(token):
    if request.method == "OPTIONS":
        resp = Response(status=204)
        resp.headers["Access-Control-Allow-Methods"] = "POST"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return _cors(resp)
    row = check_token(token)
    length = request.content_length
    if length is None:
        abort(411)
    if length > MAX_SAVE_REQUEST:
        abort(413)
    try:
        changes = json.loads(request.get_data(cache=False).decode("utf-8"))
        at = apply_saves(row["game_id"], changes)
    except (ValueError, UnicodeDecodeError, EntryError) as e:
        resp = jsonify(ok=False, error=str(e) if isinstance(e, EntryError) else "Malformed saves.")
        resp.status_code = 413 if "limit" in str(e) else 400
        return _cors(resp)
    return _cors(jsonify(ok=True, at=at))


@bp.get("/games/<int:gid>/saves.json")
def saves_download(gid):
    game = get_db().execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if game is None:
        abort(404)
    body = json.dumps({"format": SAVES_FORMAT, "version": 1, "game": game["title"], "exported_at": _now(),
                       "saves": load_saves(gid)}, ensure_ascii=False, indent=1)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", game["title"]).strip("-")[:60] or "game"
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{safe}-saves-{_now()[:10]}.json"',
        "Cache-Control": "no-store"})


@bp.post("/games/<int:gid>/saves/import")
def saves_import(gid):
    if get_db().execute("SELECT 1 FROM games WHERE id=?", (gid,)).fetchone() is None:
        abort(404)
    back = redirect(url_for("main.game_detail", gid=gid) + "#saves")
    up = request.files.get("file")
    try:
        data = json.loads(up.read(MAX_GAME_SAVES + 1).decode("utf-8")) if up else None
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or data.get("format") != SAVES_FORMAT or not isinstance(data.get("saves"), dict):
        flash("That isn't a GameLib saves file.", "error")
        return back
    try:
        apply_saves(gid, {"clear": True, "set": data["saves"]})
    except EntryError as e:
        flash(str(e), "error")
        return back
    flash(f"Restored {len(data['saves'])} save entries. They replace what was there.", "ok")
    return back


@bp.post("/games/<int:gid>/saves/delete")
def saves_delete(gid):
    if request.form.get("confirm") != "yes":
        abort(400, "Deletion was not confirmed.")
    get_db().execute("DELETE FROM saves WHERE game_id=?", (gid,))
    get_db().commit()
    flash("Browser saves deleted.", "ok")
    return redirect(url_for("main.game_detail", gid=gid) + "#saves")


# ------------------------------------------------------------ the page script

BOOT_JS = r"""/* GameLib: saves for this game are kept on the server. */
(function () {
  "use strict";
  var SAVE_URL = __URL__;
  var data = __SAVES__;
  var store = Object.create(null);
  Object.keys(data).forEach(function (k) { store[k] = String(data[k]); });
  var pending = { set: {}, del: {}, clear: false }, dirty = false, timer = null, retry = 1000;

  function tell(state, at) {
    try { if (window.parent !== window) window.parent.postMessage({ gamelib: "save", state: state, at: at || null }, "*"); } catch (e) {}
  }
  function schedule(ms) {
    dirty = true;
    if (!timer) timer = setTimeout(function () { flush(false); }, ms || 700);
  }
  function requeue(sent) {
    if (sent.clear && !pending.clear) {
      pending = { set: Object.assign({}, sent.set, pending.set), del: pending.del, clear: true };
    } else {
      Object.keys(sent.set).forEach(function (k) { if (!(k in pending.set) && !(k in pending.del)) pending.set[k] = sent.set[k]; });
      sent.del.forEach(function (k) { if (!(k in pending.set)) pending.del[k] = 1; });
    }
    retry = Math.min(retry * 2, 30000);
    schedule(retry);
  }
  function flush(leaving) {
    clearTimeout(timer); timer = null;
    if (!dirty) return;
    dirty = false;
    var sent = { set: pending.set, del: Object.keys(pending.del), clear: pending.clear };
    pending = { set: {}, del: {}, clear: false };
    var body = JSON.stringify(sent);
    var opts = { method: "POST", body: body, mode: "cors", credentials: "omit", headers: { "Content-Type": "text/plain" } };
    if (leaving && body.length < 60000) opts.keepalive = true;
    tell("saving");
    fetch(SAVE_URL, opts).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; });
    }).then(function (j) { retry = 1000; tell("saved", j.at); }, function (e) {
      tell("error", String(e && e.message || e));
      if (!/limit/.test(String(e && e.message))) requeue(sent);
    });
  }

  function makeStorage(persist) {
    var s = persist ? store : Object.create(null);
    var api = {
      getItem: function (k) { k = String(k); return k in s ? s[k] : null; },
      setItem: function (k, v) {
        k = String(k); v = String(v);
        if (s[k] === v) return;
        s[k] = v;
        if (persist) { delete pending.del[k]; pending.set[k] = v; schedule(); }
      },
      removeItem: function (k) {
        k = String(k);
        if (!(k in s)) return;
        delete s[k];
        if (persist) { delete pending.set[k]; pending.del[k] = 1; schedule(); }
      },
      clear: function () {
        Object.keys(s).forEach(function (k) { delete s[k]; });
        if (persist) { pending = { set: {}, del: {}, clear: true }; schedule(); }
      },
      key: function (i) { var ks = Object.keys(s); return i >= 0 && i < ks.length ? ks[i] : null; },
      get length() { return Object.keys(s).length; }
    };
    return new Proxy(api, {
      get: function (t, p) { return (typeof p === "symbol" || p in t) ? t[p] : (p in s ? s[p] : undefined); },
      set: function (t, p, v) { if (typeof p !== "symbol" && !(p in t)) api.setItem(p, v); return true; },
      deleteProperty: function (t, p) { if (typeof p !== "symbol" && !(p in t)) api.removeItem(p); return true; },
      has: function (t, p) { return p in t || p in s; },
      ownKeys: function () { return Object.keys(s); },
      getOwnPropertyDescriptor: function (t, p) {
        return (typeof p !== "symbol" && p in s) ? { value: s[p], writable: true, enumerable: true, configurable: true } : undefined;
      }
    });
  }
  function define(obj, name, value) {
    try { Object.defineProperty(obj, name, { configurable: true, enumerable: true, get: function () { return value; } }); } catch (e) {}
  }
  define(window, "localStorage", makeStorage(true));
  define(window, "sessionStorage", makeStorage(false));
  // IndexedDB can't be used in the sandbox; hiding it makes libraries such as
  // localForage fall back to localStorage, which is saved.
  ["indexedDB", "webkitIndexedDB", "mozIndexedDB"].forEach(function (n) { define(window, n, undefined); });
  var jar = Object.create(null);
  try {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      get: function () { return Object.keys(jar).map(function (k) { return k + "=" + jar[k]; }).join("; "); },
      set: function (v) {
        var kv = String(v).split(";")[0], i = kv.indexOf("=");
        if (i > 0) { var k = kv.slice(0, i).trim(), val = kv.slice(i + 1).trim(); if (/max-age=0|expires=thu, 01 jan 1970/i.test(v)) delete jar[k]; else jar[k] = val; }
      }
    });
  } catch (e) {}
  // The game's origin is opaque, so its own images and sounds count as
  // cross-origin and would taint canvas/WebGL. Load same-host ones in CORS
  // mode (the server allows it); other hosts are left alone.
  function cors(el, url) {
    try {
      if (el.crossOrigin == null && new URL(String(url), document.baseURI).host === location.host) el.crossOrigin = "anonymous";
    } catch (e) {}
  }
  [window.HTMLImageElement, window.HTMLMediaElement].forEach(function (C) {
    var d = C && Object.getOwnPropertyDescriptor(C.prototype, "src");
    if (!d || !d.set) return;
    Object.defineProperty(C.prototype, "src", { configurable: true, enumerable: d.enumerable, get: d.get,
      set: function (v) { cors(this, v); d.set.call(this, v); } });
  });
  var setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (String(name).toLowerCase() === "src" && (this instanceof HTMLImageElement || this instanceof HTMLMediaElement)) cors(this, value);
    return setAttr.apply(this, arguments);
  };
  window.addEventListener("pagehide", function () { flush(true); });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(true); });
  window.__gamelib = { flush: function () { flush(false); } };
  tell("loaded");
})();
"""
