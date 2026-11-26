"""Find artwork for a game folder: explicit covers, engine icons, HTML
favicons, and the application icon inside Windows .exe files.

The .exe reader is a minimal PE resource parser (stdlib only). It seeks to
the few structures it needs, so it is safe to point at very large files."""
import os
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


def _read_icon(f):
    def read(off, n):
        f.seek(off)
        b = f.read(n)
        if len(b) != n:
            raise ValueError("truncated")
        return b

    def u16(off):
        return struct.unpack("<H", read(off, 2))[0]

    def u32(off):
        return struct.unpack("<I", read(off, 4))[0]

    if read(0, 2) != b"MZ":
        return None
    pe = u32(0x3C)
    if read(pe, 4) != b"PE\0\0":
        return None
    nsec, optsize = u16(pe + 6), u16(pe + 20)
    opt = pe + 24
    magic = u16(opt)
    if magic == 0x10B:
        nrva_off, dd = opt + 92, opt + 96
    elif magic == 0x20B:
        nrva_off, dd = opt + 108, opt + 112
    else:
        return None
    if u32(nrva_off) < 3:
        return None
    res_rva = u32(dd + 16)
    if not res_rva:
        return None
    sections = []
    for i in range(min(nsec, 96)):
        vsize, va, rawsize, raw = struct.unpack("<IIII", read(opt + optsize + 40 * i + 8, 16))
        sections.append((va, max(vsize, rawsize), raw))

    def to_off(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError("RVA outside sections")

    base = to_off(res_rva)

    def entries(off):
        named, ids = struct.unpack("<HH", read(base + off + 12, 4))
        n = min(named + ids, 1024)
        data = read(base + off + 16, 8 * n)
        return [struct.unpack("<II", data[8 * i:8 * i + 8]) for i in range(n)]

    def subdir(off, wanted):
        for name, to in entries(off):
            if name == wanted and to & 0x80000000:
                return to & 0x7FFFFFFF
        return None

    def leaves(off):
        """(id, data-entry offset) per resource, first language only."""
        for name, to in entries(off):
            if to & 0x80000000:
                for _lang, lto in entries(to & 0x7FFFFFFF):
                    if not lto & 0x80000000:
                        yield name, lto
                        break

    def data(entry_off):
        rva, size = struct.unpack("<II", read(base + entry_off, 8))
        if size > MAX_RESOURCE:
            raise ValueError("resource too large")
        return read(to_off(rva), size)

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


def html_favicon(folder):
    """Icon referenced by <link rel="icon"> in index.html (relative paths only)."""
    index = os.path.join(folder, "index.html")
    try:
        with open(index, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
    except OSError:
        return None
    for tag in _LINK_ICON.findall(head):
        m = _HREF.search(tag)
        if m and not re.match(r"^(?:[a-z]+:|//)", m.group(1), re.I):
            p = _inside(folder, m.group(1).split("?")[0].split("#")[0].lstrip("./") or "x")
            if p:
                return p
    return None


def find_artwork(folder, title=""):
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
    fav = html_favicon(folder)
    if fav:
        return "icon", fav
    exe = pick_exe(folder, title)
    if exe:
        icon = exe_icon(exe)
        if icon:
            return "exe", icon
    return None
