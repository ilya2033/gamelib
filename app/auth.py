"""Single-user login, CSRF token and same-origin checks."""
import hmac
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__)

FAIL_WINDOW = 300   # seconds
FAIL_LIMIT = 5      # failed logins per window per IP
FORM_LIMIT = 64 * 1024 * 1024  # non-upload requests (cover images etc.)
LOGIN_LIMIT = 64 * 1024        # login form
SAFE_NEXT = re.compile(r"/(?![/\\])[^\s\\\x00-\x1f\x7f]*")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_ENDPOINTS = ("auth.login", "static", "main.healthz")
# Signed play links: checked by the view itself, used from a sandboxed (cookie-less) frame
TOKEN_ENDPOINTS = ("play.files", "play.boot", "play.save")

_failures = {}
_lock = threading.Lock()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _same_origin():
    src = request.headers.get("Origin") or request.headers.get("Referer")
    if not src or src == "null":
        return False
    netloc = urlsplit(src).netloc
    return netloc == request.host or netloc in current_app.config["TRUSTED_ORIGINS"]


def _eq(a, b):
    """Constant-time compare that also accepts non-ASCII input."""
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def _reserve_attempt(ip):
    """Atomically check the throttle and count this attempt as a failure.
    The slot is released again on success, so parallel guesses can't race
    past the limit."""
    now = time.time()
    with _lock:
        hits = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
        if len(hits) >= FAIL_LIMIT:
            _failures[ip] = hits
            return None
        hits.append(now)
        _failures[ip] = hits
        return now


def _release_attempt(ip, stamp):
    with _lock:
        try:
            _failures.get(ip, []).remove(stamp)
        except ValueError:
            pass


def safe_next(nxt):
    if nxt and SAFE_NEXT.fullmatch(nxt) and not urlsplit(nxt).netloc:
        return nxt
    return url_for("main.index")


def init_app(app):
    app.config["PASSWORD_HASH"] = generate_password_hash(app.config["PASSWORD"])
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.register_blueprint(bp)

    @app.before_request
    def guard():
        if request.endpoint == "static" or request.endpoint in TOKEN_ENDPOINTS:
            return None
        is_upload = request.endpoint == "main.upload"
        public = request.endpoint in PUBLIC_ENDPOINTS
        logged_in = bool(session.get("user"))
        if request.method not in SAFE_METHODS:
            # Everything here is decided from headers only: no request body
            # is read until the origin, size and login checks have passed.
            if not _same_origin():
                abort(403, "Cross-origin request rejected")
            length = request.content_length
            if length is None:
                abort(411, "Content-Length required")  # no chunked bodies
            limit = None if is_upload else (LOGIN_LIMIT if request.endpoint == "auth.login" else FORM_LIMIT)
            if limit is not None and length > limit:
                abort(413)
            if not public and not logged_in:
                abort(401)
            token = request.headers.get("X-CSRF-Token")
            if token is None and not is_upload:
                token = request.form.get("csrf")
            if not token or not _eq(token, session.get("csrf", "")):
                abort(403, "Missing or invalid CSRF token")
            return None
        if public or logged_in:
            return None
        if not request.path.startswith("/api/"):
            return redirect(url_for("auth.login", next=request.full_path))
        abort(401)

    @app.after_request
    def security_headers(resp):
        if not getattr(resp, "frameable", False):     # game files are shown in the player's frame
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


@bp.route("/login", methods=["GET", "POST"])
def login():
    csrf_token()
    if request.method == "POST":
        ip = request.remote_addr or "?"
        stamp = _reserve_attempt(ip)
        if stamp is None:
            flash("Too many failed attempts. Wait a few minutes.", "error")
            return render_template("login.html"), 429
        user = request.form.get("username", "")
        pw = request.form.get("password", "")
        ok_user = _eq(user, current_app.config["USERNAME"])
        ok_pw = check_password_hash(current_app.config["PASSWORD_HASH"], pw)
        if ok_user and ok_pw:
            _release_attempt(ip, stamp)
            session.clear()
            session.permanent = True
            session["user"] = user
            csrf_token()
            return redirect(safe_next(request.args.get("next", "")))
        flash("Wrong username or password.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
