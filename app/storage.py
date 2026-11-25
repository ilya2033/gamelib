"""Filesystem helpers: folder scanning, platform detection, safe zip
extraction and streaming zip downloads. No Flask imports here."""
import os
import re
import shutil
import stat
import time
import uuid
import zipfile

PLATFORMS = {
    "windows": "Windows",
    "linux": "Linux",
    "macos": "macOS",
    "html": "HTML",
    "unity_webgl": "Unity WebGL",
    "other": "Other",
}

STAGING_PREFIX = ".gamelib-staging-"
_STAGING_RE = re.compile(r"^\.gamelib-staging-[0-9a-f]{32}$")


class UnsafeZip(ValueError):
    pass


class BadPath(ValueError):
    pass


# ---------------------------------------------------------------- paths

def resolve_in(base, rel):
    """Return the real absolute path of `rel` inside `base`.
    Raises BadPath if it escapes `base` (via .., absolute paths or symlinks)."""
    if not rel or "\x00" in rel:
        raise BadPath("Empty path")
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, rel))
    if target == base_real or not target.startswith(base_real + os.sep):
        raise BadPath("Path is outside the games share")
    return target


_BAD_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


def safe_folder_name(name):
    name = _BAD_CHARS.sub("_", name).strip(" .")
    name = name[:200].strip(" .")
    return name or "game"


def resolve_no_links(base, rel):
    """Like resolve_in, but also refuses a path that goes through any
    symlink inside the share. Used before destructive operations."""
    target = resolve_in(base, rel)
    lexical = os.path.normpath(os.path.join(os.path.realpath(base), rel))
    if target != lexical:
        raise BadPath("Path goes through a symlink")
    return target


EXEC_SUFFIXES = (".x86_64", ".x86", ".sh", ".appimage", ".run", ".bin")


def _restore_mode(info, target):
    """Zip members created on Unix carry their permission bits. Restore the
    execute bit (only), so Linux/macOS game binaries stay runnable. Known
    launcher extensions get it even when the zip was made on Windows."""
    mode = (info.external_attr >> 16) & 0o777
    want_exec = (info.create_system == 3 and mode & 0o111) or info.filename.lower().endswith(EXEC_SUFFIXES)
    if want_exec:
        cur = os.stat(target).st_mode & 0o777
        os.chmod(target, cur | ((cur & 0o444) >> 2))


def unique_folder_name(base, name):
    candidate, n = name, 2
    while os.path.lexists(os.path.join(base, candidate)):
        candidate = f"{name} ({n})"
        n += 1
    return candidate


def list_folders(games_dir):
    """Top-level, non-hidden, non-symlink directories on the share."""
    out = []
    try:
        with os.scandir(games_dir) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                if e.is_dir(follow_symlinks=False):
                    out.append(e.name)
    except FileNotFoundError:
        return []
    return sorted(out, key=natural_key)


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ------------------------------------------------- nested share layout

SKIP_DIRS = {"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found"}
IGNORED_FILES = {"folder.jpg", "folder.jpeg", "folder.png", "desktop.ini", "thumbs.db", ".ds_store"}


def _skipped(name):
    return name.startswith(".") or name in SKIP_DIRS


def looks_like_version(path, name):
    """A version folder has a version in its name or at least one real file
    directly inside it. Anything else is a group/category folder."""
    if parse_folder_name(name)[1]:
        return True
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith(".") or e.name.lower() in IGNORED_FILES:
                    continue
                if e.is_file(follow_symlinks=False):
                    return True
    except OSError:
        return True
    return False


def walk_share(games_dir, rules=None, depth=3, known_games=()):
    """Yield (kind, relpath) for every folder on the share, kind being
    'game' (a version folder) or 'group' (a folder that holds games).

    - `rules` ({path: kind}) override the automatic detection.
    - `known_games` (tracked version paths) are never probed or entered.
    - Folders at level `depth` are always games, so nothing is hidden.
    - Hidden folders, symlinks and NAS metadata folders are skipped.
    Paths use '/' and are relative to the share."""
    rules = rules or {}
    known = set(known_games)

    def rec(rel, level):
        base = os.path.join(games_dir, rel) if rel else games_dir
        try:
            with os.scandir(base) as it:
                entries = sorted((e for e in it if not _skipped(e.name)), key=lambda e: natural_key(e.name))
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            p = f"{rel}/{e.name}" if rel else e.name
            kind = rules.get(p)
            if kind is None:
                if p in known or level >= depth:
                    kind = "game"
                else:
                    kind = "game" if looks_like_version(e.path, e.name) else "group"
            yield kind, p
            if kind == "group":
                yield from rec(p, level + 1)

    yield from rec("", 1)


def location(path):
    """'PC/Strategy/Game v1' -> ('PC', 'PC/Strategy'); 'PC/Game v1' -> ('PC', None);
    'Game v1' -> (None, None)."""
    parts = path.split("/")
    group = parts[0] if len(parts) > 1 else None
    category = "/".join(parts[:-1]) if len(parts) > 2 else None
    return group, category


def clean_subpath(text):
    """User-typed 'New/Sub folder' -> safe relative path ('' if empty)."""
    segs = [safe_folder_name(s) for s in re.split(r"[/\\]+", text or "") if s.strip(" .")]
    return "/".join(segs)


def make_dirs_in(base, rel):
    """Create rel (and parents) inside base, refusing symlinked ancestors.
    Returns the list of relative paths that were newly created."""
    base_real = os.path.realpath(base)
    created, cur = [], ""
    for seg in rel.split("/") if rel else []:
        cur = f"{cur}/{seg}" if cur else seg
        full = os.path.join(base_real, cur)
        if os.path.islink(full):
            raise BadPath("Destination goes through a symlink")
        if not os.path.isdir(full):
            if os.path.lexists(full):
                raise BadPath(f"{cur} exists and is not a folder")
            os.mkdir(full)
            created.append(cur)
    resolve_no_links(base, rel) if rel else None
    return created


# ------------------------------------------------------- name parsing

_VER_PREFIXED = re.compile(
    r"^(?P<title>.+?)[\s_\-.]*[(\[]?\s*(?:v|ver\.?|version)\s*(?P<ver>\d[\w.\-+]*?)\s*[)\]]?$",
    re.I,
)
_VER_DOTTED = re.compile(r"^(?P<title>.+?)[\s_\-]+[(\[]?(?P<ver>\d+(?:\.\d+)+[\w.\-+]*?)[)\]]?$")


def parse_folder_name(name):
    """'Hollow Knight v1.5.78' -> ('Hollow Knight', '1.5.78').
    Returns (name, '') when no version is recognisable."""
    for rx in (_VER_PREFIXED, _VER_DOTTED):
        m = rx.match(name.strip())
        if m:
            title = m.group("title").replace("_", " ").strip(" -._")
            if title:
                return title, m.group("ver")
    return name.replace("_", " ").strip(), ""


# ------------------------------------------------- platform detection

def detect_platform(path, max_depth=2, max_entries=5000):
    found, seen = set(), 0
    base_depth = os.path.normpath(path).count(os.sep)
    for root, dirs, files in os.walk(path):
        depth = os.path.normpath(root).count(os.sep) - base_depth
        here = os.path.basename(root).lower()
        for d in dirs:
            if d.lower().endswith(".app"):
                found.add("macos")
        for f in files:
            seen += 1
            fl = f.lower()
            if fl == "unityloader.js" or (here == "build" and fl.endswith(".loader.js")):
                found.add("unity_webgl")
            elif fl.endswith(".exe"):
                found.add("windows")
            elif fl.endswith((".x86_64", ".x86", ".appimage")):
                found.add("linux")
            elif fl.endswith((".html", ".htm")) and depth <= 1:
                found.add("html")
        dirs[:] = [d for d in dirs if not d.lower().endswith(".app")]
        if depth >= max_depth or seen > max_entries:
            dirs[:] = []
    for p in ("unity_webgl", "windows", "linux", "macos", "html"):
        if p in found:
            return p
    return "other"


COVER_NAMES = ("cover", "folder", "logo", "poster")
COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def find_cover_file(path):
    """A cover.* / folder.* / logo.* image at the top of a game folder."""
    try:
        names = os.listdir(path)
    except OSError:
        return None
    lookup = {n.lower(): n for n in names}
    for base in COVER_NAMES:
        for ext in COVER_EXTS:
            real = lookup.get(base + ext)
            if real and os.path.isfile(os.path.join(path, real)):
                return os.path.join(path, real)
    return None


# ------------------------------------------------------ zip extraction

def _member_parts(info):
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UnsafeZip(f"Absolute path in zip: {info.filename!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeZip(f"Path traversal in zip: {info.filename!r}")
    if any("\x00" in p for p in parts):
        raise UnsafeZip("NUL byte in zip member name")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise UnsafeZip(f"Symlink in zip: {info.filename!r}")
    return parts


def safe_extract(zip_path, dest, max_bytes):
    """Validate every member first, then extract. Strips a single shared
    top-level folder and ignores macOS metadata (__MACOSX, .DS_Store)."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UnsafeZip(f"Not a valid zip file: {e}") from None
    with zf:
        entries, total = [], 0
        for info in zf.infolist():
            parts = _member_parts(info)
            if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
                continue
            total += info.file_size
            if total > max_bytes:
                raise UnsafeZip("Zip expands beyond the allowed size")
            entries.append((info, parts))
        if not entries:
            raise UnsafeZip("Zip is empty")

        tops = {p[0] for _, p in entries}
        if len(tops) == 1 and all(len(p) > 1 or i.is_dir() for i, p in entries):
            entries = [(i, p[1:]) for i, p in entries if len(p) > 1]

        dest_real = os.path.realpath(dest)
        try:
            _write_entries(zf, entries, dest_real)
        except UnsafeZip:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError,
                NotImplementedError, RuntimeError) as e:
            # corrupt data, CRC errors, encrypted members, file/dir clashes,
            # names too long for the filesystem, ...
            raise UnsafeZip(f"Could not unpack: {e.__class__.__name__}: {e}") from None


def _write_entries(zf, entries, dest_real):
    for info, parts in entries:
        target = os.path.realpath(os.path.join(dest_real, *parts))
        if not target.startswith(dest_real + os.sep):
            raise UnsafeZip(f"Path escapes destination: {info.filename!r}")
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        _restore_mode(info, target)


def install_upload(src, filename, games_dir, folder_name, max_bytes, parent=""):
    """Unpack (or copy) an uploaded file into a new folder on the share,
    optionally inside the existing share subfolder `parent`. Works in a
    hidden staging folder, then renames it into place. Returns the new
    folder's share-relative path."""
    parent_abs = resolve_no_links(games_dir, parent) if parent else os.path.realpath(games_dir)
    name = unique_folder_name(parent_abs, safe_folder_name(folder_name))
    staging = os.path.join(games_dir, STAGING_PREFIX + uuid.uuid4().hex)
    os.makedirs(staging)
    try:
        if filename.lower().endswith(".zip"):
            safe_extract(src, staging, max_bytes)
        else:
            shutil.copyfile(src, os.path.join(staging, safe_folder_name(filename)))
        os.rename(staging, os.path.join(parent_abs, name))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return f"{parent}/{name}" if parent else name


def cleanup_stale(tmp_dir, games_dir, max_age=86400):
    cutoff = time.time() - max_age
    for d, is_staging in ((tmp_dir, False), (games_dir, True)):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if is_staging and not _STAGING_RE.match(e.name):
                continue
            try:
                if e.stat(follow_symlinks=False).st_mtime > cutoff:
                    continue
                if e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path, ignore_errors=True)
                else:
                    os.unlink(e.path)
            except OSError:
                pass


# ------------------------------------------------- streaming zip download

class _Sink:
    """Write-only, non-seekable buffer. zipfile then uses data descriptors,
    so the archive can be streamed without knowing sizes in advance."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def take(self):
        data = bytes(self.buf)
        self.buf.clear()
        return data


def stream_folder_zip(path, arcroot, chunk=1 << 20):
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for root, dirs, files in os.walk(path):
            dirs.sort(key=natural_key)
            rel_root = os.path.relpath(root, path)
            arc_dir = arcroot if rel_root == "." else f"{arcroot}/{rel_root.replace(os.sep, '/')}"
            if not dirs and not files:
                zf.writestr(zipfile.ZipInfo(arc_dir + "/"), b"")
            for f in sorted(files, key=natural_key):
                full = os.path.join(root, f)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                zi = zipfile.ZipInfo.from_file(full, f"{arc_dir}/{f}", strict_timestamps=False)
                zi.compress_type = zipfile.ZIP_STORED
                with open(full, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                    while True:
                        data = src.read(chunk)
                        if not data:
                            break
                        dst.write(data)
                        if len(sink.buf) >= chunk:
                            yield sink.take()
                if sink.buf:
                    yield sink.take()
    if sink.buf:
        yield sink.take()
