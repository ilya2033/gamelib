"""Name, version and platform guessed from uploaded archives."""
import io
import json
import plistlib
import struct
import zipfile

import pytest

from app import archive_meta, artwork
from conftest import make_zip


# ------------------------------------------------------------ a PE with a version resource

def _blk(key, value=b"", vtype=1, children=b"", vlen=None):
    k = key.encode("utf-16-le") + b"\0\0"
    head = 6 + len(k)
    pad1 = b"\0" * (-head % 4)
    if vtype == 1 and value:
        v = value.encode("utf-16-le") + b"\0\0"
        vl = len(v) // 2
    else:
        v = value if isinstance(value, bytes) else b""
        vl = len(v)
    pad2 = b"\0" * (-(head + len(pad1) + len(v)) % 4)
    body = k + pad1 + v + pad2 + children
    return struct.pack("<HHH", 6 + len(body), vl if vlen is None else vlen, vtype) + body


def version_resource(strings):
    table = b"".join(_blk(k, v) for k, v in strings.items())
    sfi = _blk("StringFileInfo", children=_blk("040904b0", children=table))
    vfi = _blk("VarFileInfo", vtype=0, children=_blk("Translation", struct.pack("<HH", 0x409, 1200), vtype=0))
    fixed = struct.pack("<I", 0xFEEF04BD) + b"\0" * 48
    return _blk("VS_VERSION_INFO", fixed, vtype=0, children=vfi + sfi)


def make_pe(resource_type, data):
    sect_rva, sect_raw = 0x1000, 0x200

    def hdr(n):
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, n)
    r = bytearray()
    r += hdr(1) + struct.pack("<II", resource_type, 0x80000000 | 24)       # type dir -> name dir @24
    r += hdr(1) + struct.pack("<II", 1, 0x80000000 | 48)                   # name dir -> lang dir @48
    r += hdr(1) + struct.pack("<II", 0x409, 72)                            # lang dir -> data entry @72
    r += struct.pack("<IIII", sect_rva + 88, len(data), 0, 0)              # data entry -> blob @88
    r += data
    rsrc = bytes(r)
    opt = bytearray(224)
    struct.pack_into("<H", opt, 0, 0x10B)
    struct.pack_into("<I", opt, 92, 16)
    struct.pack_into("<II", opt, 96 + 16, sect_rva, len(rsrc))
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 224, 0x22)
    section = struct.pack("<8sIIIIIIHHI", b".rsrc", len(rsrc), sect_rva, len(rsrc), sect_raw, 0, 0, 0, 0, 0x40000040)
    head = bytearray(b"MZ" + b"\0" * 58 + struct.pack("<I", 0x40)) + b"PE\0\0" + coff + opt + section
    return bytes(head + b"\0" * (sect_raw - len(head))) + rsrc


def test_exe_version_info():
    exe = make_pe(16, version_resource({"CompanyName": "Team Cherry", "ProductName": "Hollow Knight",
                                        "ProductVersion": "1.5.78.0", "FileVersion": "1.5.78.11833"}))
    vi = artwork.exe_version_info(io.BytesIO(exe))
    assert vi["ProductName"] == "Hollow Knight" and vi["ProductVersion"] == "1.5.78.0"
    assert artwork.exe_version_info(io.BytesIO(b"MZ" + b"\0" * 100)) == {}
    assert artwork.exe_version_info(io.BytesIO(make_pe(3, b"\0" * 40))) == {}     # icons only


# ------------------------------------------------------------ helpers

@pytest.mark.parametrize("name,title,ver", [
    ("Hollow_Knight_v1.5.78-win64.zip", "Hollow Knight", "1.5.78"),
    ("Celeste 1.4.0.0 (Windows).zip", "Celeste", "1.4.0.0"),
    ("stardew-valley-linux.zip", "stardew-valley", ""),
    ("Baba.Is.You.zip", "Baba Is You", ""),
    ("My Game.zip", "My Game", ""),
    ("story.html", "story", ""),
])
def test_from_name(name, title, ver):
    assert archive_meta.from_name(name) == (title, ver)


def test_good_values():
    assert archive_meta.good_version("1.2.0.0") == "1.2.0"
    assert archive_meta.good_version("v2.1") == "2.1"
    assert archive_meta.good_version("0.0.0") == "" and archive_meta.good_version("beta") == ""
    assert archive_meta.good_title("Unity WebGL Player | Tiny Tower") == "Tiny Tower"
    assert archive_meta.good_title("RPG Maker MV") == "" and archive_meta.good_title("Untitled") == ""
    assert archive_meta.prettify("my-cool_game") == "My Cool Game" and archive_meta.prettify("FTL") == "FTL"


def guess(tmp_path, filename, files):
    p = tmp_path / filename
    p.write_bytes(make_zip(files))
    return archive_meta.inspect_upload(str(p), filename)


# ------------------------------------------------------------ archives

def test_windows_exe_metadata(tmp_path):
    exe = make_pe(16, version_resource({"ProductName": "Hollow Knight", "ProductVersion": "1.5.78.0"}))
    g = guess(tmp_path, "hk.zip", {"Hollow Knight/hollow_knight.exe": exe, "Hollow Knight/UnityCrashHandler64.exe": b"MZ",
                                   "Hollow Knight/hollow_knight_Data/x": b"1"})
    assert g == {"title": "Hollow Knight", "title_from": "hollow_knight.exe", "version": "1.5.78",
                 "version_from": "hollow_knight.exe", "platform": "windows"}


def test_file_name_version_beats_exe(tmp_path):
    exe = make_pe(16, version_resource({"ProductName": "Hollow Knight", "ProductVersion": "1.0.0.0"}))
    g = guess(tmp_path, "Hollow Knight v1.5.78.zip", {"hk.exe": exe})
    assert (g["title"], g["version"], g["version_from"]) == ("Hollow Knight", "1.5.78", "the file name")


def test_unity_rpgmaker_renpy_mac_html(tmp_path):
    g = guess(tmp_path, "build.zip", {"Game_Data/app.info": b"Studio\nNight in the Woods", "Game.exe": b"MZ"})
    assert (g["title"], g["title_from"], g["platform"]) == ("Night in the Woods", "Unity app.info", "windows")
    pk = {"name": "", "main": "index.html", "window": {"title": "Ruins of Ash"}}
    g = guess(tmp_path, "rpg.zip", {"www/index.html": b"<title>RPG Maker MV</title>", "package.json": json.dumps(pk).encode(),
                                    "Game.exe": b"MZ"})
    assert (g["title"], g["title_from"]) == ("Ruins of Ash", "package.json")
    rpy = b'define config.name = _("Doki Club")\ndefine config.version = "1.1.1"\n'
    g = guess(tmp_path, "dc-1.1.1-pc.zip", {"DC/game/options.rpy": rpy, "DC/DC.exe": b"MZ", "DC/DC.sh": b"#!"})
    assert (g["title"], g["version"]) == ("Doki Club", "1.1.1")
    plist = plistlib.dumps({"CFBundleName": "Celeste", "CFBundleShortVersionString": "1.4.0"})
    g = guess(tmp_path, "c-mac.zip", {"Celeste.app/Contents/Info.plist": plist, "Celeste.app/Contents/MacOS/Celeste": b"x"})
    assert (g["title"], g["version"], g["platform"]) == ("Celeste", "1.4.0", "macos")
    g = guess(tmp_path, "tt.zip", {"index.html": b"<html><title>Unity WebGL Player | Tiny Tower</title>",
                                   "Build/tt.loader.js": b""})
    assert (g["title"], g["platform"]) == ("Tiny Tower", "unity_webgl")


def test_falls_back_to_folder_and_file_name(tmp_path):
    g = guess(tmp_path, "download.zip", {"Baba Is You v471/Baba Is You.exe": b"MZ not really"})
    assert (g["title"], g["version"], g["version_from"]) == ("Baba Is You", "471", "the folder “Baba Is You v471”")
    g = guess(tmp_path, "Cool_Game_v2.zip", {"readme.txt": b"x"})
    assert (g["title"], g["version"], g["platform"]) == ("Cool Game", "2", "")


def test_bad_and_hostile_files(tmp_path):
    p = tmp_path / "Broken v3.zip"
    p.write_bytes(b"not a zip")
    assert archive_meta.inspect_upload(str(p), "Broken v3.zip")["version"] == "3"
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("package.json", b'{"window": {"title": "' + b"x" * (2 << 20) + b'"}}')
    assert archive_meta.inspect_upload(str(big), "big.zip")["title"] == "big"    # oversized member skipped


# ------------------------------------------------------------ the forms

def test_add_game_from_upload_only(client):
    exe = make_pe(16, version_resource({"ProductName": "Hollow Knight", "ProductVersion": "1.5.78.0"}))
    up = client.upload("hk-win64.zip", make_zip({"HK/hollow_knight.exe": exe})).json
    meta = client.get(f"/api/uploads/{up['id']}/meta").json
    assert meta["title"] == "Hollow Knight" and meta["version"] == "1.5.78" and meta["platform"] == "windows"
    r = client.post("/games/new", {"title": "", "version": "", "platform": "windows", "source": "upload",
                                   "upload_id": up["id"]})
    assert r.status_code == 302 and "/games/" in r.headers["Location"], r.data
    import sqlite3
    con = sqlite3.connect(client.app.config["DB_PATH"])
    row = con.execute("SELECT g.title, v.label FROM games g JOIN versions v ON v.game_id=g.id").fetchone()
    assert row == ("Hollow Knight", "1.5.78")


def test_add_game_needs_a_name_when_nothing_found(client):
    up = client.upload("x.zip", make_zip({"a/b.txt": b"1", "c.txt": b"2"})).json
    meta = client.get(f"/api/uploads/{up['id']}/meta").json
    assert meta["title"] == "x"                              # the file name is the last resort
    up = client.upload("game.zip", make_zip({"a.txt": b"1"})).json
    r = client.post("/games/new", {"title": "", "version": "", "source": "upload", "upload_id": up["id"]})
    assert r.status_code == 400 and "Type a title" in r.get_data(as_text=True)


def test_new_version_takes_version_from_upload(client, dirs):
    from test_tools import add
    _, games = dirs
    gid = add(client, games, "Celeste v1", "Celeste")
    up = client.upload("Celeste-1.4.0.0-win.zip", make_zip({"Celeste.exe": b"MZ"})).json
    r = client.post(f"/games/{gid}/versions/new", {"version": "", "source": "upload", "upload_id": up["id"]})
    assert r.status_code == 302
    import sqlite3
    labels = [x[0] for x in sqlite3.connect(client.app.config["DB_PATH"]).execute(
        "SELECT label FROM versions WHERE game_id=? ORDER BY id", (gid,))]
    assert labels == ["1", "1.4.0"]


def test_meta_endpoint_checks_id(client):
    assert client.get("/api/uploads/nothex/meta").status_code == 404
    assert client.get("/api/uploads/" + "0" * 32 + "/meta").status_code == 404
