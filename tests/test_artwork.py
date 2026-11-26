"""Artwork lookup: .exe icons (synthetic PE files), engine icons, favicons."""
import io
import os
import sqlite3
import struct

import pytest
from PIL import Image

from app import artwork


def png(size=64, color=(220, 40, 40, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def dib_from_ico(size=32):
    """Bitmap (DIB) icon data as stored in RT_ICON, taken from a Pillow .ico."""
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (40, 200, 40, 255)).save(buf, "ICO", sizes=[(size, size)], bitmap_format="bmp")
    ico = buf.getvalue()
    length, offset = struct.unpack("<II", ico[6 + 8:6 + 16])
    return ico[offset:offset + length]


def make_pe(icon_images, pe32plus=False, group_first=True):
    """Minimal PE with one .rsrc section holding RT_ICON entries 1..n and
    one RT_GROUP_ICON pointing at them. icon_images: [(w, bpp, bytes)]."""
    sect_rva, sect_raw = 0x1000, 0x200
    n = len(icon_images)

    def dir_hdr(count):
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, count)

    # layout (offsets inside .rsrc)
    root = 0
    t_icon = root + 16 + 2 * 8
    t_group = t_icon + 16 + n * 8
    icon_names = t_group + 16 + 8
    group_name = icon_names + n * (16 + 8)
    data_entries = group_name + 16 + 8
    blobs = data_entries + (n + 1) * 16
    images, off = [], blobs
    for _w, _bpp, b in icon_images:
        images.append((off, b))
        off += len(b) + (-len(b) % 8)
    grp = struct.pack("<HHH", 0, 1, n) + b"".join(
        struct.pack("<BBBBHHIH", w % 256, w % 256, 0, 0, 1, bpp, len(b), i + 1)
        for i, (w, bpp, b) in enumerate(icon_images))
    grp_off = off

    r = bytearray()
    r += dir_hdr(2)
    types = [(RT_GROUP, t_group), (RT_ICON_, t_icon)] if group_first else [(RT_ICON_, t_icon), (RT_GROUP, t_group)]
    types.sort()                                              # PE requires ascending IDs
    for tid, toff in types:
        r += struct.pack("<II", tid, 0x80000000 | toff)
    assert len(r) == t_icon
    r += dir_hdr(n) + b"".join(struct.pack("<II", i + 1, 0x80000000 | (icon_names + i * 24)) for i in range(n))
    assert len(r) == t_group
    r += dir_hdr(1) + struct.pack("<II", 1, 0x80000000 | group_name)
    assert len(r) == icon_names
    for i in range(n):
        r += dir_hdr(1) + struct.pack("<II", 0x409, data_entries + i * 16)
    assert len(r) == group_name
    r += dir_hdr(1) + struct.pack("<II", 0x409, data_entries + n * 16)
    assert len(r) == data_entries
    for o, b in images:
        r += struct.pack("<IIII", sect_rva + o, len(b), 0, 0)
    r += struct.pack("<IIII", sect_rva + grp_off, len(grp), 0, 0)
    assert len(r) == blobs
    for o, b in images:
        r += b + b"\0" * (-len(b) % 8)
    r += grp
    rsrc = bytes(r)

    opt_size = 240 if pe32plus else 224
    dd_off = 112 if pe32plus else 96
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if pe32plus else 0x10B)
    struct.pack_into("<I", opt, dd_off - 4, 16)                      # NumberOfRvaAndSizes
    struct.pack_into("<II", opt, dd_off + 16, sect_rva, len(rsrc))    # resource directory
    coff = struct.pack("<HHIIIHH", 0x8664 if pe32plus else 0x14C, 1, 0, 0, 0, opt_size, 0x22)
    section = struct.pack("<8sIIIIIIHHI", b".rsrc", len(rsrc), sect_rva, len(rsrc), sect_raw, 0, 0, 0, 0, 0x40000040)
    head = bytearray(b"MZ" + b"\0" * 58 + struct.pack("<I", 0x40))
    head += b"PE\0\0" + coff + opt + section
    head += b"\0" * (sect_raw - len(head))
    return bytes(head) + rsrc


RT_ICON_, RT_GROUP = 3, 14


@pytest.mark.parametrize("pe32plus", [False, True])
def test_exe_icon_png_picks_largest(tmp_path, pe32plus):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(make_pe([(16, 32, png(16)), (256, 32, png(256)), (48, 32, png(48))], pe32plus))
    data = artwork.exe_icon(str(exe))
    assert data.startswith(artwork.PNG_MAGIC)
    assert Image.open(io.BytesIO(data)).size == (256, 256)


def test_exe_icon_bitmap_becomes_ico(tmp_path):
    exe = tmp_path / "Old.exe"
    exe.write_bytes(make_pe([(32, 32, dib_from_ico(32))]))
    data = artwork.exe_icon(str(exe))
    img = Image.open(io.BytesIO(data))
    assert img.format == "ICO" and img.size == (32, 32)
    assert img.convert("RGBA").getpixel((16, 16))[:3] == (40, 200, 40)


@pytest.mark.parametrize("blob", [b"", b"MZ", b"not a pe file at all" * 10, b"MZ" + b"\xff" * 200])
def test_exe_icon_garbage_is_none(tmp_path, blob):
    f = tmp_path / "bad.exe"
    f.write_bytes(blob)
    assert artwork.exe_icon(str(f)) is None


def test_exe_icon_truncated_is_none(tmp_path):
    f = tmp_path / "cut.exe"
    f.write_bytes(make_pe([(64, 32, png(64))])[:0x260])
    assert artwork.exe_icon(str(f)) is None


def test_pick_exe_prefers_title_and_skips_helpers(tmp_path):
    for name, size in (("UnityCrashHandler64.exe", 5000), ("unins000.exe", 9000),
                       ("vc_redist.x64.exe", 90000), ("Starfall.exe", 100), ("Tool.exe", 800)):
        (tmp_path / name).write_bytes(b"x" * size)
    assert os.path.basename(artwork.pick_exe(str(tmp_path), "Starfall Tactics")) == "Starfall.exe"
    assert os.path.basename(artwork.pick_exe(str(tmp_path), "Something Else")) == "Tool.exe"   # largest non-helper
    sub = tmp_path / "Bin"
    sub.mkdir()
    (tmp_path / "Starfall.exe").unlink()
    (sub / "Starfall.exe").write_bytes(b"x")
    assert artwork.pick_exe(str(tmp_path), "Starfall").endswith("Bin/Starfall.exe")


def test_find_artwork_order(tmp_path):
    g = tmp_path / "g"
    (g / "www" / "icon").mkdir(parents=True)
    (g / "Game.exe").write_bytes(make_pe([(64, 32, png(64))]))
    assert artwork.find_artwork(str(g), "Game")[0] == "exe"
    (g / "www" / "icon" / "icon.png").write_bytes(png(48))
    assert artwork.find_artwork(str(g), "Game") == ("icon", str(g / "www" / "icon" / "icon.png"))
    (g / "cover.jpg").write_bytes(b"jpg")
    assert artwork.find_artwork(str(g), "Game")[0] == "cover"


def test_html_favicon_link_and_escape(tmp_path):
    g = tmp_path / "web"
    (g / "img").mkdir(parents=True)
    (g / "img" / "fav.png").write_bytes(png(32))
    (g / "index.html").write_text('<html><head><link rel="shortcut icon" href="./img/fav.png?v=2"></head>')
    assert artwork.html_favicon(str(g)) == str(g / "img" / "fav.png")
    (tmp_path / "secret.png").write_bytes(png(8))
    for href in ("../secret.png", "/etc/passwd", "https://example.com/x.png", "data:image/png;base64,AAA"):
        (g / "index.html").write_text(f'<link rel="icon" href="{href}">')
        assert artwork.html_favicon(str(g)) is None, href


# ------------------------------------------------------------ in the app

def test_scan_import_uses_exe_icon(client, dirs):
    _, games = dirs
    d = games / "Icon Quest v1"
    d.mkdir()
    (d / "IconQuest.exe").write_bytes(make_pe([(64, 32, png(64, (10, 120, 250, 255)))]))
    client.post("/scan", {"count": "1", "include-0": "1", "folder-0": "Icon Quest v1", "title-0": "Icon Quest",
                          "label-0": "1", "platform-0": "windows"})
    cover = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT cover FROM games").fetchone()[0]
    assert cover and cover.endswith(".webp")
    img = Image.open(os.path.join(client.app.config["COVERS_DIR"], cover))
    assert img.size == (600, 800)
    px = img.getpixel((300, 400))[:3]                              # the icon is centred on the card
    assert all(abs(a - b) <= 6 for a, b in zip(px, (10, 120, 250))), px


def test_find_artwork_button(client, dirs):
    _, games = dirs
    d = games / "Later v1"
    d.mkdir()
    (d / "readme.txt").write_text("no art yet")
    client.post("/scan", {"count": "1", "include-0": "1", "folder-0": "Later v1", "title-0": "Later",
                          "label-0": "1", "platform-0": "windows"})
    gid = sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT id FROM games").fetchone()[0]
    r = client.post(f"/games/{gid}/artwork")
    assert r.status_code == 302 and "/edit" in r.headers["Location"]        # nothing found
    (d / "Later.exe").write_bytes(make_pe([(32, 32, png(32))]))
    r = client.post(f"/games/{gid}/artwork")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/games/{gid}")
    assert sqlite3.connect(client.app.config["DB_PATH"]).execute("SELECT cover FROM games").fetchone()[0]
