"""Find artwork for a game folder: explicit covers, engine icons, HTML
favicons, and the application icon inside Windows .exe files.

The .exe reader is a minimal PE resource parser (stdlib only). It seeks to
the few structures it needs, so it is safe to point at very large files."""
import os
import posixpath
import re
import struct

from .storage import BadPath, find_cover_file, resolve_no_links

RT_ICON, RT_GROUP_ICON = 3, 14
MAX_RESOURCE = 4 << 20          # an icon image larger than this is not an icon
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ENGINE_ICONS = (
    "www/icon/icon.png", "icon/icon.png",          # RPG Maker MV / MZ
    "game/gui/window_icon.png",                     # Ren'Py
    "TemplateData/favicon.ico",                     # Unity WebGL
    "icon.png", "icon.ico", "icon.jpg", "icon.webp",
    "favicon.png", "favicon.ico",
)
EXE_SKIP = re.compile(
    r"^(unins|uninstall|setup|install|crash|unitycrashhandler|vc_?redist|dxsetup|directx|dotnet|"
    r"ue4prereq|ueprereq|notification_helper|cefprocess|steamerrorreporter|quicksfv|7z|winrar)", re.I)


# ------------------------------------------------------------- PE icons

def exe_icon(path):
    """Return the application icon of a Windows executable as PNG bytes or
    as a single-image .ico, or None if there is none / the file is not PE."""
    try:
        with open(path, "rb") as f:
            return _read_icon(f)
    except (OSError, ValueError, struct.error, KeyError, TypeError, IndexError):
        return None


class _PE:
    """Resource directory of a PE file (seeks only to what is needed)."""

    def __init__(self, f):
        self.f = f
        if self.read(0, 2) != b"MZ":
            raise ValueError("not PE")
        pe = self.u32(0x3C)
        if self.read(pe, 4) != b"PE\0\0":
            raise ValueError("not PE")
        nsec, optsize = self.u16(pe + 6), self.u16(pe + 20)
        opt = pe + 24
        magic = self.u16(opt)
        if magic == 0x10B:
            nrva_off, dd = opt + 92, opt + 96
        elif magic == 0x20B:
            nrva_off, dd = opt + 108, opt + 112
        else:
            raise ValueError("not PE")
        if self.u32(nrva_off) < 3:
            raise ValueError("no resources")
        res_rva = self.u32(dd + 16)
        if not res_rva:
            raise ValueError("no resources")
        self.sections = []
        for i in range(min(nsec, 96)):
            vsize, va, rawsize, raw = struct.unpack("<IIII", self.read(opt + optsize + 40 * i + 8, 16))
            self.sections.append((va, max(vsize, rawsize), raw))
        self.base = self.to_off(res_rva)

    def read(self, off, n):
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError("truncated")
        return b

    def u16(self, off):
        return struct.unpack("<H", self.read(off, 2))[0]

    def u32(self, off):
        return struct.unpack("<I", self.read(off, 4))[0]

    def to_off(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError("RVA outside sections")

    def entries(self, off):
        named, ids = struct.unpack("<HH", self.read(self.base + off + 12, 4))
        n = min(named + ids, 1024)
        data = self.read(self.base + off + 16, 8 * n)
        return [struct.unpack("<II", data[8 * i:8 * i + 8]) for i in range(n)]

    def subdir(self, off, wanted):
        for name, to in self.entries(off):
            if name == wanted and to & 0x80000000:
                return to & 0x7FFFFFFF
        return None

    def leaves(self, off):
        """(id, data-entry offset) per resource, first language only."""
        for name, to in self.entries(off):
            if to & 0x80000000:
                for _lang, lto in self.entries(to & 0x7FFFFFFF):
                    if not lto & 0x80000000:
                        yield name, lto
                        break

    def data(self, entry_off):
        rva, size = struct.unpack("<II", self.read(self.base + entry_off, 8))
        if size > MAX_RESOURCE:
            raise ValueError("resource too large")
        return self.read(self.to_off(rva), size)


def _read_icon(f):
    try:
        pe = _PE(f)
    except ValueError:
        return None
    subdir, leaves, data = pe.subdir, pe.leaves, pe.data
    icons_dir, groups_dir = subdir(0, RT_ICON), subdir(0, RT_GROUP_ICON)
    if icons_dir is None or groups_dir is None:
        return None
    icons = dict(leaves(icons_dir))
    best = None
    for _gid, goff in leaves(groups_dir):            # first group = the application icon
        g = data(goff)
        count = struct.unpack("<H", g[4:6])[0]
        for i in range(min(count, 64)):
            e = g[6 + 14 * i:20 + 14 * i]
            if len(e) < 14:
                break
            w, h, cc, rsv, planes, bpp, size, nid = struct.unpack("<BBBBHHIH", e)
            score = (w or 256, bpp)
            if nid in icons and (best is None or score > best[0]):
                best = (score, nid, (w, h, cc, rsv, planes, bpp))
        break
    if best is None:
        return None
    img = data(icons[best[1]])
    if img.startswith(PNG_MAGIC):
        return img
    w, h, cc, rsv, planes, bpp = best[2]
    return (struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", w, h, cc, rsv, planes, bpp, len(img), 22) + img)


# --------------------------------------------------------- PE version info

RT_VERSION = 16


def exe_version_info(f):
    """String fields of a Windows executable's version resource, such as
    {'ProductName': ..., 'ProductVersion': ..., 'FileVersion': ...}.
    `f` is a seekable binary file. Returns {} when there is none."""
    try:
        pe = _PE(f)
        vdir = pe.subdir(0, RT_VERSION)
        if vdir is None:
            return {}
        for _id, off in pe.leaves(vdir):
            return _version_strings(pe.data(off))
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        pass
    return {}


def _version_strings(data):
    out = {}

    def align(n):
        return (n + 3) & ~3

    def block(off, depth):
        if depth > 4 or off + 6 > len(data):
            return len(data)
        length, vlen, vtype = struct.unpack_from("<HHH", data, off)
        if length < 6:
            return len(data)
        end = min(off + length, len(data))
        k = off + 6
        while k + 1 < end and data[k:k + 2] != b"\0\0":
            k += 2
        key = data[off + 6:k].decode("utf-16-le", "replace")
        pos = align(k + 2)
        size = vlen * 2 if vtype == 1 else vlen
        if depth == 3 and vtype == 1 and vlen:       # VS_VERSIONINFO > StringFileInfo > table > String
            val = data[pos:pos + size].decode("utf-16-le", "replace").split("\0")[0].strip()
            if val and key not in out:
                out[key] = val
        pos = align(pos + size)
        if depth < 3 and (depth != 1 or key == "StringFileInfo"):
            while pos < end:
                nxt = block(pos, depth + 1)
                if nxt <= pos:
                    break
                pos = align(nxt)
        return end

    block(0, 0)
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick_exe(folder, title=""):
    """The game's own executable in a version folder (top level or one
    level down), skipping installers, crash handlers and redistributables."""
    want = _norm(title)
    found = []
    for depth, root in ((0, folder),) + tuple((1, e.path) for e in _subdirs(folder)):
        try:
            with os.scandir(root) as it:
                for e in it:
                    if not e.name.lower().endswith(".exe") or EXE_SKIP.match(e.name):
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    stem = _norm(e.name[:-4])
                    match = bool(want) and stem and (stem in want or want in stem)
                    found.append((not match, depth, -e.stat().st_size, e.name, e.path))
        except OSError:
            continue
    return min(found)[-1] if found else None


def _subdirs(folder):
    try:
        with os.scandir(folder) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")][:20]
    except OSError:
        return []


# ---------------------------------------------------- folder artwork

_LINK_ICON = re.compile(r"""<link\b[^>]*\brel=["']?(?:shortcut\s+)?icon["']?[^>]*>""", re.I)
_HREF = re.compile(r"""\bhref=["']?([^"'\s>]+)""", re.I)


def _inside(folder, rel):
    try:
        p = resolve_no_links(folder, rel)
    except BadPath:
        return None
    return p if os.path.isfile(p) else None


def html_favicon(folder, page="index.html"):
    """Icon referenced by <link rel="icon"> in a page (relative paths only)."""
    index = _inside(folder, page)
    if not index:
        return None
    try:
        with open(index, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
    except OSError:
        return None
    for tag in _LINK_ICON.findall(head):
        m = _HREF.search(tag)
        if m and not re.match(r"^(?:[a-z]+:|//)", m.group(1), re.I):
            href = m.group(1).split("?")[0].split("#")[0]
            p = _inside(folder, posixpath.normpath(posixpath.join(posixpath.dirname(page), href)).lstrip("/") or "x")
            if p:
                return p
    return None


def find_artwork(folder, title="", entry=None):
    """Best artwork for a game folder, as one of:
    ('cover', path) - an image meant as a cover/logo, use as is
    ('icon', path)  - a small square icon image, render onto a card
    ('exe', bytes)  - icon extracted from the game's .exe (PNG or .ico)
    or None."""
    cover = find_cover_file(folder)
    if cover:
        return "cover", cover
    for rel in ENGINE_ICONS:
        p = _inside(folder, rel)
        if p:
            return "icon", p
    entry = (entry or "").replace("\\", "/")
    fav = html_favicon(folder, entry if entry.lower().endswith((".html", ".htm")) else "index.html")
    if fav:
        return "icon", fav
    exe = _inside(folder, entry) if entry.lower().endswith(".exe") else None
    exe = exe or pick_exe(folder, title)
    if exe:
        icon = exe_icon(exe)
        if icon:
            return "exe", icon
    return None
