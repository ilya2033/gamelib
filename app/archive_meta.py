"""Guess a game's name, version and platform from an uploaded .zip or .html.

Looks at, in order of trust:
  * what the game declares about itself: macOS Info.plist, Unity app.info,
    Ren'Py options.rpy, NW.js/Electron/RPG Maker package.json, the version
    resource of the game's .exe, the <title> of its start page;
  * the archive's file name and its single top folder
    ("Hollow_Knight_v1.5.78-win64.zip" -> "Hollow Knight", "1.5.78").
A version written in the file name wins over one inside the game (it is
what the uploader or the developer chose to show); for the name it's the
other way round, since file names are often shortened or slugged.

Only the zip's directory and a few small files are read; nothing is
unpacked to disk. Member sizes are capped, so a hostile zip can't make
this slow or large."""
import io
import json
import os
import plistlib
import posixpath
import re
import zipfile

from . import artwork
from .storage import parse_folder_name

MAX_SMALL = 1 << 20           # json / plist / rpy / html read
MAX_EXE = 64 << 20            # exe loaded into memory for its version resource
MAX_MEMBERS = 50000
HTML_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
GENERIC = {"", "game", "untitled", "index", "unity", "unity webgl player", "unity player", "rpg maker", "rpg maker mv",
           "rpg maker mz", "nw.js", "nwjs", "electron", "godot", "godot engine", "my game", "new game", "project",
           "renpy", "ren'py", "html5 game", "download", "downloads", "archive", "files", "data", "bin", "win", "windows", "app", "application", "player", "launcher", "main", "default"}
# build/platform words often glued to file names: Game-1.2-win64.zip
JUNK_SUFFIX = re.compile(
    r"[\s_\-.(\[]+(win(dows)?(32|64)?|x86(_64)?|x64|amd64|i386|linux(64)?|mac(os)?|osx|universal|web(gl)?|html5?|"
    r"browser|pc|desktop|build|release|portable|setup|installer|itch(io)?|steam|gog|drm[\s_\-]?free|"
    r"public|demo-?build)[)\]]?$", re.I)
RENPY_NAME = re.compile(r"""^\s*define\s+config\.name\s*=\s*_?\(?\s*(['"])(.+?)\1""", re.M)
RENPY_VER = re.compile(r"""^\s*define\s+config\.version\s*=\s*(['"])(.+?)\1""", re.M)


def clean_stem(name):
    """'Hollow_Knight_v1.5.78-win64.zip' -> 'Hollow_Knight_v1.5.78'"""
    stem = re.sub(r"\.(zip|html?)$", "", os.path.basename(name), flags=re.I).strip()
    for _ in range(4):
        new = JUNK_SUFFIX.sub("", stem)
        if new == stem or not new:
            break
        stem = new
    return stem


def from_name(name):
    """(title, version) from a file or folder name."""
    stem = clean_stem(name)
    if not stem:
        return "", ""
    title, ver = parse_folder_name(stem)
    title = re.sub(r"\s+", " ", title.replace(".", " ") if " " not in title and "." in title else title)
    return title.strip(), ver


def prettify(name):
    """'my-cool_game' -> 'My Cool Game'; names with capitals stay as written."""
    name = (name or "").strip()
    if re.fullmatch(r"[a-z0-9][a-z0-9\-_]*", name) and re.search(r"[a-z]", name):
        return " ".join(w.capitalize() for w in re.split(r"[\-_]+", name) if w)
    return name


def good_title(t):
    t = re.sub(r"\s+", " ", (t or "")).strip()
    t = re.sub(r"^unity web ?gl player\s*[|\-:]\s*", "", t, flags=re.I)
    return t if t.lower() not in GENERIC and len(t) <= 200 and not t.lower().startswith("unitycrash") else ""


def good_version(v):
    v = (v or "").strip().lstrip("vV").strip()
    if not v or len(v) > 40 or not re.match(r"^\d", v):
        return ""
    # 1.2.0.0 -> 1.2.0 (Windows pads to four numbers)
    while re.fullmatch(r"\d+(\.\d+){3,}", v) and v.endswith(".0"):
        v = v[:-2]
    return "" if v in ("0", "0.0", "0.0.0") else v


class _Zip:
    def __init__(self, zf):
        self.zf = zf
        infos = [i for i in zf.infolist()[:MAX_MEMBERS] if not i.is_dir()]
        names = [i.filename.replace("\\", "/").lstrip("/") for i in infos]
        tops = {n.split("/", 1)[0] for n in names}
        self.top = ""
        if len(tops) == 1 and all("/" in n for n in names) and not next(iter(tops)).lower().endswith(".app"):
            self.top = tops.pop()               # one shared folder: stripped on unpack, like safe_extract
        cut = len(self.top) + 1 if self.top else 0
        self.files = {}
        for i, n in zip(infos, names):
            rel = n[cut:]
            if rel and ".." not in rel.split("/"):
                self.files[rel] = i
        self.lower = {k.lower(): k for k in self.files}

    def find(self, rel):
        return self.files.get(self.lower.get(rel.lower(), ""))

    def read(self, info, cap=MAX_SMALL):
        if info is None or info.file_size > cap:
            return None
        try:
            with self.zf.open(info) as f:
                return f.read(cap + 1)[:cap]
        except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError):
            return None   # encrypted, damaged or unsupported member

    def glob(self, pattern, depth=3):
        rx = re.compile(pattern, re.I)
        return [self.files[k] for k in sorted(self.files, key=lambda k: (k.count("/"), k))
                if k.count("/") < depth and rx.search(k)]


def platform_of(paths):
    found = set()
    for p in paths:
        parts = p.lower().split("/")
        name, depth = parts[-1], len(parts) - 1
        if any(x.endswith(".app") for x in parts[:-1]):
            found.add("macos")
        elif name == "unityloader.js" or (len(parts) > 1 and parts[-2] == "build" and name.endswith(".loader.js")):
            found.add("unity_webgl")
        elif name.endswith(".exe"):
            found.add("windows")
        elif name.endswith((".x86_64", ".x86", ".appimage")):
            found.add("linux")
        elif name.endswith((".html", ".htm")) and depth <= 1:
            found.add("html")
    for p in ("unity_webgl", "windows", "linux", "macos", "html"):
        if p in found:
            return p
    return ""


def inspect_zip(path, filename):
    titles, versions = [], []          # (title, where it came from)
    with zipfile.ZipFile(path) as zf:
        z = _Zip(zf)
        # --- what the game says about itself
        for info in z.glob(r"(^|/)[^/]+\.app/Contents/Info\.plist$", depth=4)[:1]:
            try:
                pl = plistlib.loads(z.read(info) or b"")
            except Exception:       # plistlib raises several kinds on bad input
                pl = {}
            if isinstance(pl, dict):
                titles.append((str(pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or ""), "Info.plist"))
                versions.append((str(pl.get("CFBundleShortVersionString") or ""), "Info.plist"))
        for info in z.glob(r"(^|/)[^/]+_Data/app\.info$")[:1]:
            lines = (z.read(info) or b"").decode("utf-8", "replace").splitlines()
            if len(lines) >= 2:
                titles.append((lines[1], "Unity app.info"))
        for info in z.glob(r"(^|/)game/options\.rpy$")[:1]:
            text = (z.read(info) or b"").decode("utf-8", "replace")
            m, v = RENPY_NAME.search(text), RENPY_VER.search(text)
            titles.append((m.group(2) if m else "", "Ren'Py options.rpy"))
            versions.append((v.group(2) if v else "", "Ren'Py options.rpy"))
        for info in z.glob(r"(^|/)package\.json$", depth=3)[:1]:
            try:
                pk = json.loads((z.read(info) or b"{}").decode("utf-8", "replace"))
            except ValueError:
                pk = {}
            if isinstance(pk, dict):
                win = pk.get("window") if isinstance(pk.get("window"), dict) else {}
                name = pk.get("productName") or win.get("title") or ""
                if not name and isinstance(pk.get("name"), str) and "/" not in pk["name"]:
                    name = prettify(pk["name"])
                titles.append((str(name), "package.json"))
                if pk.get("version") not in ("1.0.0", "0.1.0"):           # tool defaults say nothing
                    versions.append((str(pk.get("version") or ""), "package.json"))
        exes = [i for i in z.glob(r"\.exe$", depth=3)
                if not artwork.EXE_SKIP.match(posixpath.basename(i.filename))]
        if exes:
            stem = artwork._norm(clean_stem(filename))
            exe = min(exes, key=lambda i: (not (stem and artwork._norm(posixpath.basename(i.filename)[:-4]) in stem),
                                           i.filename.count("/"), -i.file_size))
            data = z.read(exe, MAX_EXE)
            vi = artwork.exe_version_info(io.BytesIO(data)) if data else {}
            label = posixpath.basename(exe.filename)
            titles.append((vi.get("ProductName", ""), label))
            versions.append((vi.get("ProductVersion", ""), label))
            versions.append((vi.get("FileVersion", ""), label))
        for rel in ("index.html", "www/index.html", "index.htm"):
            info = z.find(rel)
            if info:
                m = HTML_TITLE.search(z.read(info) or b"")
                titles.append((_html_text(m.group(1)) if m else "", rel))
                break
        top, paths = z.top, list(z.files)
    # --- names: the archive, then its top folder
    file_title, file_ver = from_name(filename)
    top_title, top_ver = from_name(top) if top else ("", "")
    return _choose(titles, versions, [(top_title, f"the folder “{top}”"), (file_title, "the file name")],
                   [(file_ver, "the file name"), (top_ver, f"the folder “{top}”")], platform_of(paths))


def _html_text(raw):
    import html
    return html.unescape(raw.decode("utf-8", "replace"))


def inspect_html(path, filename):
    with open(path, "rb") as f:
        m = HTML_TITLE.search(f.read(MAX_SMALL))
    file_title, file_ver = from_name(filename)
    return _choose([(_html_text(m.group(1)) if m else "", "the page title")], [],
                   [(file_title, "the file name")], [(file_ver, "the file name")], "html")


def _choose(titles, versions, name_titles, name_versions, platform):
    title = next(((good_title(t), src) for t, src in titles + name_titles if good_title(t)), ("", ""))
    version = next(((good_version(v), src) for v, src in name_versions + versions if good_version(v)), ("", ""))
    return {"title": title[0], "title_from": title[1], "version": version[0], "version_from": version[1],
            "platform": platform}


def inspect_upload(path, filename):
    """{'title', 'version', 'platform', 'title_from', 'version_from'}; empty
    strings for what can't be told. Never raises for a bad file."""
    try:
        if filename.lower().endswith(".zip"):
            return inspect_zip(path, filename)
        return inspect_html(path, filename)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        t, v = from_name(filename)
        return {"title": good_title(t), "title_from": "the file name" if good_title(t) else "",
                "version": good_version(v), "version_from": "the file name" if good_version(v) else "",
                "platform": ""}
