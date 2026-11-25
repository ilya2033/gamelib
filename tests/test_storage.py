"""Unit tests for name parsing, platform detection and unsafe zips."""
import io
import os
import zipfile

import pytest

from app import storage
from app.storage import UnsafeZip
from conftest import make_zip


@pytest.mark.parametrize("name, title, ver", [
    ("Hollow Knight v1.5.78", "Hollow Knight", "1.5.78"),
    ("Celeste (v1.4.0.0)", "Celeste", "1.4.0.0"),
    ("Stardew_Valley_v1.6", "Stardew Valley", "1.6"),
    ("Game-1.2.3", "Game", "1.2.3"),
    ("My RPG ver 0.9b", "My RPG", "0.9b"),
    ("Dead Cells [1.24]", "Dead Cells", "1.24"),
    ("Portal 2", "Portal 2", ""),
    ("Untitled", "Untitled", ""),
])
def test_parse_folder_name(name, title, ver):
    assert storage.parse_folder_name(name) == (title, ver)


def _touch(base, *paths):
    for p in paths:
        full = os.path.join(base, p)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").close()


@pytest.mark.parametrize("files, expected", [
    (["index.html", "Build/game.loader.js", "Build/game.wasm.br"], "unity_webgl"),
    (["MyGame.exe", "UnityPlayer.dll", "MyGame_Data/x"], "windows"),
    (["game.x86_64"], "linux"),
    (["Game.app/Contents/Info.plist"], "macos"),
    (["index.html", "js/main.js"], "html"),
    (["top/story.html"], "html"),
    (["readme.txt"], "other"),
])
def test_detect_platform(tmp_path, files, expected):
    _touch(tmp_path, *files)
    assert storage.detect_platform(str(tmp_path)) == expected


def _extract(tmp_path, data, max_bytes=10**9):
    src = tmp_path / "in.zip"
    src.write_bytes(data)
    dest = tmp_path / "out"
    dest.mkdir()
    storage.safe_extract(str(src), str(dest), max_bytes)
    return dest


def test_extract_strips_single_top_folder(tmp_path):
    dest = _extract(tmp_path, make_zip({"MyGame/": b"", "MyGame/index.html": b"hi", "MyGame/js/a.js": b"x",
                                        "__MACOSX/MyGame/._index.html": b"junk"}))
    assert sorted(os.listdir(dest)) == ["index.html", "js"]


def test_extract_keeps_multiple_top_entries(tmp_path):
    dest = _extract(tmp_path, make_zip({"a/x.txt": b"1", "b.txt": b"2"}))
    assert sorted(os.listdir(dest)) == ["a", "b.txt"]


@pytest.mark.parametrize("bad", ["../evil.txt", "ok/../../evil.txt", "/etc/evil", "C:/evil.txt", "..\\evil.txt"])
def test_extract_rejects_traversal(tmp_path, bad):
    with pytest.raises(UnsafeZip):
        _extract(tmp_path, make_zip({"fine.txt": b"ok", bad: b"pwned"}))
    assert not (tmp_path / "evil.txt").exists()
    # nothing is extracted when any member is unsafe
    assert os.listdir(tmp_path / "out") == []


def test_extract_rejects_symlink(tmp_path):
    with pytest.raises(UnsafeZip, match="Symlink"):
        _extract(tmp_path, make_zip({"a.txt": b"1"}, symlinks=[("link", "/etc/passwd")]))


def test_extract_rejects_oversize(tmp_path):
    with pytest.raises(UnsafeZip, match="allowed size"):
        _extract(tmp_path, make_zip({"big.bin": b"0" * 5000}), max_bytes=1000)


def test_extract_rejects_non_zip(tmp_path):
    with pytest.raises(UnsafeZip, match="valid zip"):
        _extract(tmp_path, b"this is not a zip")


def test_resolve_in_blocks_escape(tmp_path):
    (tmp_path / "games").mkdir()
    (tmp_path / "games" / "ok").mkdir()
    os.symlink(tmp_path, tmp_path / "games" / "sneaky")
    base = str(tmp_path / "games")
    assert storage.resolve_in(base, "ok").endswith("ok")
    for rel in ("../", "..", "/etc", "sneaky", "ok/../..", ""):
        with pytest.raises(storage.BadPath):
            storage.resolve_in(base, rel)


def test_stream_zip_roundtrip(tmp_path):
    _touch(tmp_path / "g", "a.txt", "sub/b.txt")
    (tmp_path / "g" / "a.txt").write_bytes(b"A" * 3_000_000)
    (tmp_path / "g" / "empty").mkdir()
    data = b"".join(storage.stream_folder_zip(str(tmp_path / "g"), "Game v1", chunk=65536))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        assert {"Game v1/a.txt", "Game v1/sub/b.txt", "Game v1/empty/"} <= names
        assert zf.read("Game v1/a.txt") == b"A" * 3_000_000
