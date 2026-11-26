"""1.1.0：管理圖片（新增、出處、下架）、小卡上的出處、透明圖、個人頁編輯按鈕。"""
import io
import sqlite3

import pytest
from PIL import Image

from app import card, checkin, config, db, memes

from .conftest import admin_code, admin_verify, login
from .test_submissions import _img, bearer, upload


@pytest.fixture
def adm(client, fake_clock):
    r = admin_verify(client, admin_code(fake_clock))
    assert r.status_code == 200
    client.cookies.clear()
    return bearer(r.json()["token"])


def png(color=(200, 80, 80, 255), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def add(client, adm, data=None, source=""):
    return client.post("/api/admin/memes", headers=adm, data={"source": source},
                       files={"file": ("a.png", data or _img(), "image/png")})


def put(client, adm, name, **body):
    return client.put(f"/api/admin/memes/{name}", headers=adm, json=body)


def card_png(client, url):
    r = client.get(url)
    assert r.status_code == 200
    return Image.open(io.BytesIO(r.content)).convert("RGB")


# ---------- 新增 ----------

def test_add_publishes_right_away(client, adm):
    r = add(client, adm, source="  x@sthkm3 ")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["name"] == "zhiwu_0.jpg" and m["source"] == "x@sthkm3" and m["hidden"] is False and m["used"] == 0
    assert "zhiwu_0.jpg" in checkin.list_memes()
    assert add(client, adm).json()["name"] == "zhiwu_1.jpg"                    # 取號不重複
    assert add(client, adm, png((0, 0, 0, 0))).json()["name"] == "zhiwu_2.png"  # 透明的存成 PNG
    with Image.open(config.MEME_DIR / "zhiwu_0.jpg") as im:
        assert len(im.getexif()) == 0


def test_add_validation_and_limits(client, adm, monkeypatch):
    assert add(client, adm, b"not an image").json()["detail"] == "invalid_image"
    assert add(client, adm, source="x" * 41).json()["detail"] == "source_too_long"
    assert add(client, adm, b"\0" * (config.SUBMIT_MAX_BYTES + 1)).status_code == 413
    monkeypatch.setattr(config, "ADMIN_ADD_LIMIT", 2)
    from app import limits
    for w in limits.Window._all:
        w.clear()
    assert [add(client, adm).status_code for _ in range(3)] == [200, 200, 429]


def test_admin_only(client, sent):
    tok = bearer(login(client, sent)["token"])
    client.cookies.clear()
    for method, path, kw in (("get", "/api/admin/memes", {}), ("put", "/api/admin/memes/test.png", {"json": {}}),
                             ("get", "/api/admin/memes/test.png/thumb", {}),
                             ("post", "/api/admin/memes", {"files": {"file": ("a.png", _img(), "image/png")}})):
        assert getattr(client, method)(path, headers=tok, **kw).status_code == 403
        assert getattr(client, method)(path, **kw).status_code == 401


# ---------- 清單、下架、出處 ----------

def test_list_newest_first(client, adm):
    for _ in range(3):
        add(client, adm)
    names = [m["name"] for m in client.get("/api/admin/memes", headers=adm).json()["items"]]
    assert names == ["zhiwu_2.jpg", "zhiwu_1.jpg", "zhiwu_0.jpg", "test.png"]


def test_hidden_is_never_drawn(client, sent, adm, monkeypatch):
    add(client, adm)
    assert put(client, adm, "test.png", hidden=True).json()["hidden"] is True
    assert checkin.list_memes() == ["zhiwu_0.jpg"]
    assert all(checkin.pick_meme() == "zhiwu_0.jpg" for _ in range(20))
    assert {card.guest_meme(c, "2026-11-21") for c in ("AB12", "CD34", "EF56", "GH78")} == {"zhiwu_0.jpg"}
    put(client, adm, "zhiwu_0.jpg", hidden=True)
    assert checkin.list_memes() == [] and checkin.pick_meme() is None
    assert put(client, adm, "test.png", hidden=False).json()["hidden"] is False
    assert checkin.list_memes() == ["test.png"]
    assert len(client.get("/api/admin/memes", headers=adm).json()["items"]) == 2   # 管理頁兩張都看得到


def test_hidden_meme_keeps_old_cards(client, sent, adm):
    tok = bearer(login(client, sent)["token"])
    client.cookies.clear()
    r = client.post("/api/checkin", headers=tok).json()
    before = card_png(client, r["card_url"])
    put(client, adm, "test.png", hidden=True)
    for p in (config.DATA_DIR / "cards").rglob("*.png"):
        p.unlink()
    assert card_png(client, r["card_url"]).tobytes() == before.tobytes()   # 重畫還是同一張圖
    assert client.get("/api/admin/memes", headers=adm).json()["items"][0]["used"] == 1


def test_source_is_cleaned(client, adm):
    assert put(client, adm, "test.png", source="‮x@​sthkm3\n").json()["source"] == "x@sthkm3"
    assert put(client, adm, "test.png", source="a" * 41).json()["detail"] == "source_too_long"
    assert put(client, adm, "test.png", source="").json()["source"] == ""


@pytest.mark.parametrize("name", ["../zhiwu.db", "..%2Fzhiwu.db", "nope.png", ".hidden.png", "a/b.png", "test.png.webp"])
def test_bad_names(client, adm, name):
    assert client.put(f"/api/admin/memes/{name}", headers=adm, json={"hidden": True}).status_code == 404
    assert client.get(f"/api/admin/memes/{name}/thumb", headers=adm).status_code == 404


def test_thumb(client, adm):
    Image.new("RGBA", (2000, 1000), (0, 0, 0, 0)).save(config.MEME_DIR / "big.png")
    url = next(m["thumb_url"] for m in client.get("/api/admin/memes", headers=adm).json()["items"] if m["name"] == "big.png")
    r = client.get(url, headers=adm)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    im = Image.open(io.BytesIO(r.content))
    assert im.size == (320, 160) and im.mode == "RGBA"                       # 縮圖保留透明
    put(client, adm, "big.png", hidden=True)
    assert client.get(url, headers=adm).status_code == 200                   # 下架的也看得到縮圖


# ---------- 投稿的出處 ----------

def test_submission_source_flows_to_gallery(client, sent, adm):
    tok = login(client, sent)["token"]
    client.cookies.clear()
    r = client.post("/api/submissions", headers=bearer(tok), data={"source": "youtube@p2p2p"},
                    files={"file": ("a.png", _img(), "image/png")})
    assert r.json()["item"]["source"] == "youtube@p2p2p"
    assert client.post("/api/submissions", headers=bearer(tok), data={"source": "x" * 41},
                       files={"file": ("a.png", _img(), "image/png")}).json()["detail"] == "source_too_long"
    item = client.get("/api/admin/submissions", headers=adm).json()["item"]
    assert item["source"] == "youtube@p2p2p"
    name = client.post(f"/api/admin/submissions/{item['id']}/approve", headers=adm).json()["meme"]
    assert memes.source_of(name) == "youtube@p2p2p"


def test_admin_can_change_source_when_approving(client, sent, adm):
    tok = login(client, sent)["token"]
    client.cookies.clear()
    ids = [upload(client, tok).json()["item"]["id"] for _ in range(2)]
    a = client.post(f"/api/admin/submissions/{ids[0]}/approve", headers=adm, json={"source": "x@fixed"}).json()["meme"]
    b = client.post(f"/api/admin/submissions/{ids[1]}/approve", headers=adm, json={}).json()["meme"]
    assert memes.source_of(a) == "x@fixed" and memes.source_of(b) == ""
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM memes WHERE name = ?", (b,)).fetchone()[0] == 0   # 沒出處就不建資料列
    conn.close()


# ---------- 小卡 ----------

def source_area(img):
    """出處畫在圖的右下角。"""
    return img.crop((card.W - card.PAD - 500, card.PAD + 700, card.W - card.PAD - 10, card.PAD + 750))


def test_source_drawn_on_card_and_redrawn_on_change(client, sent, adm):
    tok = bearer(login(client, sent)["token"])
    client.cookies.clear()
    r = client.post("/api/checkin", headers=tok).json()
    plain = card_png(client, r["card_url"])
    put(client, adm, "test.png", source="x@sthkm3")
    with_source = card_png(client, r["card_url"])                 # 改出處後快取被清掉，重畫
    assert source_area(plain).tobytes() != source_area(with_source).tobytes()
    assert plain.crop((0, 0, 1080, 600)).tobytes() == with_source.crop((0, 0, 1080, 600)).tobytes()   # 其他地方不變
    put(client, adm, "test.png", source="")
    assert card_png(client, r["card_url"]).tobytes() == plain.tobytes()


def test_guest_card_shows_source(client, adm):
    params = {"code": "AB12", "n": 1}
    plain = client.get("/card/guest.png", params=params).content
    put(client, adm, "test.png", source="x@sthkm3")
    assert client.get("/card/guest.png", params=params).content != plain


@pytest.mark.parametrize("lang", ["zh-TW", "zh-CN", "ja", "ko", "en"])
def test_long_source_fits(lang):
    img = card.draw(lang, "test.png", "名稱", None, None, "x", "y", 1, source="あ" * 40)
    assert img.size == (card.W, card.H)


# ---------- 透明 ----------

@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "GIF"])
def test_transparent_meme_uses_panel_colour(fmt, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEME_DIR", tmp_path)
    im = Image.new("RGBA", (400, 400), (255, 255, 255, 0))         # 透明處底下藏的是白色
    im.paste((255, 0, 0, 255), (150, 150, 250, 250))
    if fmt == "GIF":   # 調色盤透明：0 號色（白）是透明的
        gif = Image.new("P", (400, 400), 0)
        gif.putpalette([255, 255, 255, 255, 0, 0])
        gif.paste(1, (150, 150, 250, 250))
        gif.save(tmp_path / "t.gif", transparency=0)
    else:
        im.save(tmp_path / f"t.{fmt.lower()}", fmt, lossless=True) if fmt == "WEBP" else im.save(tmp_path / "t.png")
    out = card.draw("en", f"t.{fmt.lower()}", "n", None, None, "x", "y", 1)
    corner = out.getpixel((card.PAD + 400, card.PAD + 60))        # 圖的透明處
    assert max(corner) < 40, corner                                 # 是框的深色底，不是白
    center = out.getpixel((card.W // 2, card.PAD + 380))
    assert center[0] > 200 and center[1] < 60                       # 不透明的部分照常


def test_transparent_avatar_uses_accent():
    av = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
    out = card.draw("en", None, "n", av, None, "x", "y", 1)
    px = out.getpixel((card.PAD + 84, card.PAD + 760 + 40 + 84))
    assert abs(px[0] - card.ACCENT[0]) < 10 and abs(px[1] - card.ACCENT[1]) < 10


# ---------- 其他 ----------

def test_pages_served(client):
    for path in ("/images", "/review", "/upload"):
        assert client.get(path).status_code == 200


def test_edit_button_moved_to_profile(client):
    html = client.get("/").text
    assert 'id="edit-profile"' not in html and 'id="user-edit"' in html


def test_upgrade_from_102(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ADMIN_LOGIN", "")
    conn = sqlite3.connect(tmp_path / "zhiwu.db")
    conn.execute("CREATE TABLE submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, day TEXT NOT NULL, "
                 "ext TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', meme TEXT, created_at INTEGER NOT NULL, "
                 "reviewed_at INTEGER)")
    conn.execute("INSERT INTO submissions(uid, day, ext, created_at) VALUES (1, '2026-11-21', 'jpg', 1)")
    conn.commit()
    conn.close()
    db.init()
    conn = db.connect()
    assert conn.execute("SELECT source FROM submissions").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0] == 0
    conn.close()
