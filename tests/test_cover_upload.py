"""Cover images on Add game: any PNG kind, any file name or content type."""
import io
import os
import sqlite3

import pytest
from PIL import Image


def png(mode):
    b = io.BytesIO()
    img = Image.new(mode, (40, 40))
    if mode == "P":
        img.info["transparency"] = 0
    img.save(b, "PNG")
    b.seek(0)
    return b


@pytest.mark.parametrize("mode,name,ctype", [
    ("RGBA", "cover.png", "image/png"),
    ("RGB", "MY COVER.PNG", "application/octet-stream"),   # upper-case name, unknown type
    ("P", "logo", ""),                                     # no extension at all
    ("LA", "a.png", "image/png"),
    ("I;16", "deep.png", "image/png"),                     # 16-bit
])
def test_new_game_png_cover(client, mode, name, ctype):
    r = client.post("/games/new", {"title": "T " + name, "platform": "other", "source": "none",
                                   "cover": (png(mode), name, ctype or None)}, content_type="multipart/form-data")
    assert r.status_code == 302, r.data
    con = sqlite3.connect(client.app.config["DB_PATH"])
    cover = con.execute("SELECT cover FROM games WHERE title=?", ("T " + name,)).fetchone()[0]
    assert cover and os.path.exists(os.path.join(client.app.config["COVERS_DIR"], cover))


def test_cover_input_accepts_images_by_extension_too(client):
    page = client.get("/games/new").get_data(as_text=True)
    assert 'accept="image/*,.png,.jpg,.jpeg,.webp,.gif" data-cover-input' in page
