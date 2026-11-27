"""Search-as-you-type suggestions for the top-bar search box.

Games are ranked in Python, so matching can ignore case, accents and
punctuation ("ff vii" finds "Final Fantasy VII", "witcher3" finds
"The Witcher 3") and fall back to close spellings when nothing matches."""
import difflib
import re
import unicodedata

from flask import jsonify, request, url_for

from .db import get_db
from .storage import PLATFORMS
from .views import bp, version_label

MAX_GAMES = 8
MAX_OTHER = 4
MAX_QUERY = 100


def norm(text):
    """lower case, no accents, punctuation as spaces: 'Pokémon: Red!' -> 'pokemon red'"""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _initials(words):
    return "".join(w[0] for w in words)


def score(query, title):
    """Lower is better; None if the title doesn't match.
    0 exact, 1 starts with, 2 every query word starts a title word (any order),
    3 initials ("ffvii"/"ff vii"), 4 contained anywhere (spaces ignored)."""
    q, t = norm(query), norm(title)
    if not q or not t:
        return None
    if t == q:
        return 0
    if t.startswith(q):
        return 1
    words, qwords = t.split(), q.split()
    if all(any(w.startswith(qw) for w in words) for qw in qwords):
        return 2
    # initials of the leading words, then the rest: "ff vii" / "ffvii" -> Final Fantasy VII
    qs = q.replace(" ", "")
    for i in range(2, len(words) + 1):
        head = _initials(words[:i])
        if qs == head or (qs.startswith(head) and " ".join(words[i:]).replace(" ", "").startswith(qs[len(head):])):
            return 3
    if qs in t.replace(" ", ""):
        return 4
    return None


def suggest_games(q):
    db = get_db()
    rows = db.execute(
        "SELECT g.id, g.title, g.platform, g.cover, g.favorite, s.label AS status, s.color AS status_color, "
        "(SELECT label FROM versions v WHERE v.game_id=g.id ORDER BY added_at DESC, id DESC LIMIT 1) AS latest "
        "FROM games g LEFT JOIN statuses s ON s.id=g.status_id").fetchall()
    ranked = []
    for r in rows:
        s = score(q, r["title"])
        if s is not None:
            ranked.append((s, len(r["title"]), r["title"].lower(), r))
    fuzzy = False
    if not ranked and len(norm(q)) >= 3:
        # nothing matched: offer close spellings ("witchr" -> "The Witcher")
        by_norm = {}
        for r in rows:
            by_norm.setdefault(norm(r["title"]), []).append(r)
        nq = norm(q)
        close = difflib.get_close_matches(nq, list(by_norm), n=MAX_GAMES, cutoff=0.6)
        # also compare with the start of each title, so a short query can match a long title
        starts = {}
        for t in by_norm:
            starts.setdefault(t[:len(nq) + 1], []).append(t)
        for c in difflib.get_close_matches(nq, list(starts), n=MAX_GAMES, cutoff=0.7):
            close += [t for t in starts[c] if t not in close]
        for i, t in enumerate(close[:MAX_GAMES]):
            ranked += [(10 + i, 0, t, r) for r in by_norm[t]]
        fuzzy = bool(ranked)
    ranked.sort(key=lambda x: x[:3])
    return [{
        "id": r["id"], "title": r["title"],
        "url": url_for("main.game_detail", gid=r["id"]),
        "cover": url_for("main.cover", name=r["cover"]) if r["cover"] else None,
        "platform": PLATFORMS.get(r["platform"], "Other"),
        "status": r["status"], "status_color": r["status_color"],
        "latest": version_label(r["latest"]) if r["latest"] else None,
        "favorite": bool(r["favorite"]),
    } for *_, r in ranked[:MAX_GAMES]], fuzzy


def suggest_named(q, rows, make_url):
    out = []
    for r in rows:
        s = score(q, r["name"])
        if s is not None and s <= 2:
            out.append((s, r["name"].lower(), r))
    out.sort(key=lambda x: x[:2])
    return [{"name": r["name"], "count": r["n"], "url": make_url(r),
             "kind": ("group" if r["is_group"] else "category") if "is_group" in r.keys() else "tag"}
            for *_, r in out[:MAX_OTHER]]


@bp.get("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()[:MAX_QUERY]
    if not norm(q):
        return jsonify(q=q, games=[], tags=[], categories=[], fuzzy=False)
    db = get_db()
    games, fuzzy = suggest_games(q)
    tags = suggest_named(q, db.execute(
        "SELECT t.name, COUNT(*) AS n FROM tags t JOIN game_tags gt ON gt.tag_id=t.id GROUP BY t.id"),
        lambda r: url_for("main.index", tag=r["name"]))
    cats = suggest_named(q, db.execute(
        "SELECT c.id, c.name, c.is_group, COUNT(gc.game_id) AS n FROM categories c "
        "LEFT JOIN game_categories gc ON gc.category_id=c.id GROUP BY c.id HAVING n > 0"),
        lambda r: url_for("main.index", **{"group" if r["is_group"] else "category": r["id"]}))
    resp = jsonify(q=q, games=games, tags=tags, categories=cats, fuzzy=fuzzy,
                   all_url=url_for("main.index", q=q))
    resp.headers["Cache-Control"] = "no-store"
    return resp
