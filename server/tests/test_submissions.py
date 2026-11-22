import io

from PIL import Image

from app import admin, checkin, config, db

from .conftest import ADMIN_LOGIN, admin_code, login


def _img(fmt="PNG", color=(10, 200, 30), size=(300, 200), **kw):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, **kw)
    return buf.getvalue()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, token, data=None, name="a.png"):
    return client.post("/api/submissions", headers=bearer(token),
                       files={"file": (name, data or _img(), "image/png")})


def admin_token(client, fake_clock):
    r = client.post("/api/auth/verify", json={"email": ADMIN_LOGIN, "code": admin_code(fake_clock)})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_upload_and_daily_quota(client, sent, fake_clock):
    tok = login(client, sent)["token"]
    r = upload(client, tok)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["item"]["status"] == "pending" and data["remaining"] == 4 and data["limit"] == 5
    for _ in range(4):
        assert upload(client, tok).status_code == 200
    r = upload(client, tok)
    assert r.status_code == 429 and r.json()["detail"] == "quota_exceeded"

    # 台灣時間隔天 03:59 還是同一天，04:00 才重置
    fake_clock.set("2026-11-22T03:59:00+08:00")
    assert upload(client, tok).status_code == 429
    fake_clock.set("2026-11-22T04:00:00+08:00")
    assert upload(client, tok).json()["remaining"] == 4

    mine = client.get("/api/submissions", headers=bearer(tok)).json()
    assert mine["used"] == 1 and len(mine["items"]) == 6


def test_rejected_still_counts(client, sent, fake_clock):
    tok = login(client, sent)["token"]
    adm = admin_token(client, fake_clock)
    for _ in range(5):
        sid = upload(client, tok).json()["item"]["id"]
        assert client.post(f"/api/admin/submissions/{sid}/reject", headers=bearer(adm)).status_code == 200
    assert upload(client, tok).json()["detail"] == "quota_exceeded"


def test_upload_validation(client, sent):
    tok = login(client, sent)["token"]
    r = upload(client, tok, b"<script>alert(1)</script>")
    assert r.status_code == 400 and r.json()["detail"] == "invalid_image"
    r = upload(client, tok, b"x" * (config.SUBMIT_MAX_BYTES + 1))
    assert r.status_code == 413 and r.json()["detail"] == "file_too_large"
    # 失敗的不算額度
    assert client.get("/api/submissions", headers=bearer(tok)).json()["used"] == 0


def test_upload_strips_exif_and_shrinks(client, sent):
    tok = login(client, sent)["token"]
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    item = upload(client, tok, _img("JPEG", size=(4000, 1000), exif=exif), "a.jpg").json()["item"]
    saved = config.DATA_DIR / "submissions" / f"{item['id']}.jpg"
    with Image.open(saved) as im:
        assert im.format == "JPEG" and len(im.getexif()) == 0
        assert im.size == (2048, 512)


def test_png_with_alpha_stays_png(client, sent):
    tok = login(client, sent)["token"]
    buf = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(buf, "PNG")
    item = upload(client, tok, buf.getvalue()).json()["item"]
    assert (config.DATA_DIR / "submissions" / f"{item['id']}.png").is_file()


def test_permissions(client, sent, fake_clock):
    a = login(client, sent, "a@example.com")["token"]
    b = login(client, sent, "b@example.com")["token"]
    sid = upload(client, a).json()["item"]["id"]
    url = f"/api/submissions/{sid}/image"
    assert client.get(url, headers=bearer(a)).status_code == 200
    assert client.get(url, headers=bearer(b)).status_code == 404  # 別人的投稿當作不存在
    for method, path in (("get", "/api/admin/submissions"),
                         ("post", f"/api/admin/submissions/{sid}/approve"),
                         ("post", f"/api/admin/submissions/{sid}/reject")):
        assert getattr(client, method)(path, headers=bearer(b)).status_code == 403
    adm = admin_token(client, fake_clock)
    assert client.get(url, headers=bearer(adm)).status_code == 200
    assert "pending_reviews" not in client.get("/api/me", headers=bearer(a)).json()
    assert client.get("/api/me", headers=bearer(adm)).json()["pending_reviews"] == 1


def test_approve_names_like_image_folder(client, sent, fake_clock):
    for name in ("zhiwu_0.png", "zhiwu_7.jpg"):
        Image.new("RGB", (10, 10)).save(config.MEME_DIR / name)
    tok = login(client, sent)["token"]
    first = upload(client, tok).json()["item"]["id"]
    second = upload(client, tok).json()["item"]["id"]
    adm = admin_token(client, fake_clock)

    # 一張一張審：先拿到最舊的
    r = client.get("/api/admin/submissions", headers=bearer(adm)).json()
    assert r["pending"] == 2 and r["item"]["id"] == first
    assert r["item"]["uploader"]["uid_str"] == "00000001"

    r = client.post(f"/api/admin/submissions/{first}/approve", headers=bearer(adm))
    assert r.status_code == 200 and r.json()["meme"] == "zhiwu_8.jpg" and r.json()["pending"] == 1
    assert (config.MEME_DIR / "zhiwu_8.jpg").is_file()
    assert not (config.DATA_DIR / "submissions" / f"{first}.jpg").exists()
    assert "zhiwu_8.jpg" in checkin.list_memes()
    assert client.post(f"/api/admin/submissions/{first}/approve", headers=bearer(adm)).status_code == 409
    assert client.post(f"/api/admin/submissions/{first}/reject", headers=bearer(adm)).status_code == 409

    assert client.post(f"/api/admin/submissions/{second}/approve",
                       headers=bearer(adm)).json()["meme"] == "zhiwu_9.jpg"
    assert client.get("/api/admin/submissions", headers=bearer(adm)).json() == {"item": None, "pending": 0}
    # 已同意的投稿，本人還是看得到圖
    assert client.get(f"/api/submissions/{first}/image", headers=bearer(tok)).status_code == 200
    # 沒有暫存檔留在迷因資料夾
    assert not [p for p in config.MEME_DIR.iterdir() if p.name.startswith(".")]


def test_approved_meme_shows_on_card(client, sent, fake_clock, monkeypatch):
    for p in config.MEME_DIR.iterdir():
        p.unlink()
    tok = login(client, sent)["token"]
    sid = upload(client, tok).json()["item"]["id"]
    adm = admin_token(client, fake_clock)
    client.post(f"/api/admin/submissions/{sid}/approve", headers=bearer(adm))
    assert checkin.list_memes() == ["zhiwu_0.jpg"]
    r = client.post("/api/checkin", headers=bearer(tok)).json()
    conn = db.connect()
    assert conn.execute("SELECT meme FROM checkins WHERE uid = 1").fetchone()[0] == "zhiwu_0.jpg"
    assert client.get(r["card_url"]).status_code == 200


def test_reject_deletes_file(client, sent, fake_clock):
    tok = login(client, sent)["token"]
    sid = upload(client, tok).json()["item"]["id"]
    adm = admin_token(client, fake_clock)
    assert client.post(f"/api/admin/submissions/{sid}/reject", headers=bearer(adm)).json()["status"] == "rejected"
    assert not (config.DATA_DIR / "submissions" / f"{sid}.png").exists()
    item = client.get("/api/submissions", headers=bearer(tok)).json()["items"][0]
    assert item["status"] == "rejected" and item["image_url"] is None
    assert client.get(f"/api/submissions/{sid}/image", headers=bearer(tok)).status_code == 404
    assert client.post("/api/admin/submissions/9999/reject", headers=bearer(adm)).status_code == 404


def test_reset_progress_keeps_submissions(client, sent):
    tok = login(client, sent)["token"]
    upload(client, tok)
    admin.main(["reset-progress", "--yes"])
    assert client.get("/api/submissions", headers=bearer(tok)).json()["used"] == 1


def test_upload_and_review_pages_served(client):
    assert client.get("/upload").status_code == 200
    assert client.get("/review").status_code == 200


def test_media_content_types(client):
    for path, ctype in (("/assets/media/zhizhi-poster.webp", "image/webp"), ("/assets/media/zhizhi.mp4", "video/mp4")):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
