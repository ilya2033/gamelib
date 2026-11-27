"""Minimal SteamGridDB client for cover art (https://www.steamgriddb.com/api/v2).

Only used when someone clicks a SteamGridDB button and an API key is set in
Settings. Game titles are sent to SteamGridDB; nothing else is."""
import json
import urllib.error
import urllib.parse
import urllib.request

from .version import __version__

API = "https://www.steamgriddb.com/api/v2"
IMAGE_HOSTS = ("steamgriddb.com",)          # cdn2.steamgriddb.com etc.
MAX_IMAGE = 15 * 1024 * 1024
TIMEOUT = 20
# portrait grids, best first (600x900 is the Steam library capsule size)
DIMENSIONS = "600x900,660x930,342x482"


class SGDBError(Exception):
    pass


def _open(url, key=None):
    headers = {"User-Agent": f"GameLib/{__version__}"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SGDBError("SteamGridDB refused the API key. Check it in Settings.") from None
        if e.code == 404:
            return None
        raise SGDBError(f"SteamGridDB answered with HTTP {e.code}.") from None
    except (urllib.error.URLError, OSError) as e:
        raise SGDBError(f"Can't reach SteamGridDB: {getattr(e, 'reason', e)}") from None


def _get(path, key):
    resp = _open(API + path, key)
    if resp is None:
        return []
    with resp:
        try:
            data = json.load(resp)
        except ValueError:
            raise SGDBError("SteamGridDB sent an unreadable answer.") from None
    if not data.get("success", False):
        raise SGDBError("SteamGridDB: " + "; ".join(data.get("errors") or ["request failed"]))
    return data.get("data") or []


def search(key, title):
    """[{id, name, year}] for a title, best match first."""
    term = urllib.parse.quote(title.strip()[:120], safe="")
    if not term:
        return []
    out = []
    for g in _get(f"/search/autocomplete/{term}", key)[:12]:
        year = None
        if g.get("release_date"):
            try:
                import datetime
                year = datetime.datetime.fromtimestamp(int(g["release_date"]), datetime.timezone.utc).year
            except (ValueError, OverflowError, OSError):
                pass
        out.append({"id": int(g["id"]), "name": str(g.get("name", "")), "year": year})
    return out


def grids(key, game_id):
    """[{url, thumb, width, height}] portrait covers for a SteamGridDB game id."""
    data = _get(f"/grids/game/{int(game_id)}?dimensions={DIMENSIONS}&types=static&nsfw=false&humor=false", key)
    out = []
    for g in data[:24]:
        url, thumb = g.get("url", ""), g.get("thumb") or g.get("url", "")
        if allowed_image(url) and allowed_image(thumb):
            out.append({"url": url, "thumb": thumb, "width": g.get("width"), "height": g.get("height")})
    return out


def allowed_image(url):
    p = urllib.parse.urlparse(url or "")
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS)


def download(url):
    """Image bytes from SteamGridDB's CDN (refuses any other host)."""
    if not allowed_image(url):
        raise SGDBError("That image is not on SteamGridDB.")
    resp = _open(url)
    if resp is None:
        raise SGDBError("The image is no longer on SteamGridDB.")
    with resp:
        data = resp.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise SGDBError("The image is too large.")
    return data
