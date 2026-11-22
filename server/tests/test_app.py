import io
from datetime import timedelta

from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.main import app

from .conftest import login


# ---------- 登入 ----------

def test_register_and_login(client, sent):
    data = login(client, sent, "A@Example.com")
    assert data["user"]["uid"] == 1
    assert data["user"]["uid_str"] == "00000001"
    assert data["user"]["display_name"] == "吱鼯_1"
    assert data["user"]["is_admin"] is False
    assert data["user"]["email"] == "a@example.com"
    assert data["token"]
    assert client.get("/api/me").json()["uid"] == 1

    # 同一個信箱再登入一次是同一個帳號
    client.cookies.clear()
    client.post("/api/auth/request-code", json={"email": "b@example.com"})  # 別人註冊不影響
    assert login(client, sent, "a@example.com")["user"]["uid"] == 1


def test_bearer_token(client, sent):
    token = login(client, sent)["token"]
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    # Bearer 不需要 X-ZHIWU
    bare = TestClient(app)
    r = bare.post("/api/checkin", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_csrf_header_required(client, sent):
    login(client, sent)
    r = client.post("/api/checkin", headers={"X-ZHIWU": ""})
    assert r.status_code == 403


def test_resend_limit(client, sent, fake_clock):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    r = client.post("/api/auth/request-code", json={"email": "a@example.com"})
    assert r.status_code == 429
    fake_clock.value += timedelta(seconds=61)
    assert client.post("/api/auth/request-code", json={"email": "a@example.com"}).status_code == 200


def test_wrong_code_five_times(client, sent):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    right = sent["a@example.com"]
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(4):
        r = client.post("/api/auth/verify", json={"email": "a@example.com", "code": wrong})
        assert r.json()["detail"] == "wrong_code"
    r = client.post("/api/auth/verify", json={"email": "a@example.com", "code": wrong})
    assert r.json()["detail"] == "expired"
    # 作廢後正確的碼也不能用
    r = client.post("/api/auth/verify", json={"email": "a@example.com", "code": right})
    assert r.json()["detail"] == "not_requested"


def test_code_expires(client, sent, fake_clock):
    client.post("/api/auth/request-code", json={"email": "a@example.com"})
    fake_clock.value += timedelta(minutes=5, seconds=1)
    r = client.post("/api/auth/verify", json={"email": "a@example.com", "code": sent["a@example.com"]})
    assert r.json()["detail"] == "expired"


def test_invalid_email(client):
    r = client.post("/api/auth/request-code", json={"email": "not-an-email"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_email"


def test_logout(client, sent):
    login(client, sent)
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401


# ---------- 簽到 ----------

def test_checkin_once_per_jst_day(client, sent, fake_clock):
    login(client, sent)
    fake_clock.set("2026-11-21T12:00:00+08:00")
    r = client.post("/api/checkin").json()
    assert r["already"] is False and r["total_days"] == 1 and r["streak"] == 1
    assert r["day"] == "2026-11-21"
    assert r["card_url"] == "/card/00000001/2026-11-21.png"
    assert r["share_url"].endswith("/c/00000001/2026-11-21")

    r = client.post("/api/checkin").json()
    assert r["already"] is True and r["total_days"] == 1

    # 台灣時間隔天 03:59 還算同一天
    fake_clock.set("2026-11-22T03:59:59+08:00")
    assert client.post("/api/checkin").json()["already"] is True

    # 日本時間 00:00 換日
    fake_clock.set("2026-11-22T04:00:00+08:00")
    r = client.post("/api/checkin").json()
    assert r["already"] is False and r["total_days"] == 2 and r["streak"] == 2


def test_streak_breaks(client, sent, fake_clock):
    login(client, sent)
    for day in ("2026-11-01", "2026-11-02", "2026-11-04"):
        fake_clock.set(f"{day}T09:00:00+08:00")
        client.post("/api/checkin")
    r = client.get("/api/checkin/today").json()
    assert r["total_days"] == 3 and r["streak"] == 1

    # 隔天還沒簽到時，連續天數保留昨天的紀錄
    fake_clock.set("2026-11-05T09:00:00+08:00")
    r = client.get("/api/checkin/today").json()
    assert r["checked_in"] is False and r["streak"] == 1
    assert r["next_reset"] == "2026-11-06T04:00:00+08:00"
    # 凌晨 02:00 時還沒換日，下次換日是當天 04:00
    fake_clock.set("2026-11-06T02:00:00+08:00")
    r = client.get("/api/checkin/today").json()
    assert r["day"] == "2026-11-05" and r["next_reset"] == "2026-11-06T04:00:00+08:00"


def test_checkin_requires_login(client):
    assert client.post("/api/checkin").status_code == 401


# ---------- UID ----------

def test_uid_sequence_skips_granted(client, sent):
    from app import admin

    login(client, sent, "a@example.com")
    admin.main(["grant-uid", "a@example.com", "99999999"])
    # 下一位仍然拿到正常的下一號
    assert login(client, sent, "b@example.com")["user"]["uid"] == 2


def test_only_admin_has_uid_tier(client, sent):
    conn = db.connect()
    conn.execute("UPDATE meta SET value = 114514 WHERE key = 'next_uid'")
    u = login(client, sent, "c@example.com")["user"]
    assert u["uid_str"] == "00114514" and u["uid_tier"] is None
    assert client.get("/api/users/0").json()["uid_tier"] == "admin"


def test_grant_uid_range(client, sent):
    import pytest
    from app import admin

    login(client, sent, "a@example.com")
    for bad in ("0", "100000000"):
        with pytest.raises(SystemExit):
            admin.main(["grant-uid", "a@example.com", bad])


def test_granted_uid_keeps_checkins(client, sent):
    from app import admin

    login(client, sent, "a@example.com")
    client.post("/api/checkin")
    admin.main(["grant-uid", "a@example.com", "114514"])
    me = client.get("/api/me").json()  # session 也跟著改 uid
    assert me["uid_str"] == "00114514" and me["total_days"] == 1


def _png(w, h, color=(10, 200, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_avatar_resized(client, sent):
    login(client, sent)
    r = client.put("/api/me/avatar", files={"file": ("a.png", _png(1600, 900), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["avatar_url"]
    img = Image.open(io.BytesIO(client.get(url).content))
    assert img.format == "WEBP" and img.size == (1024, 1024)


def test_avatar_rejects_non_image(client, sent):
    login(client, sent)
    r = client.put("/api/me/avatar", files={"file": ("a.png", b"<script>alert(1)</script>", "image/png")})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_image"


def test_profile_patch_and_markdown_escape(client, sent):
    login(client, sent)
    r = client.patch("/api/me", json={"display_name": "  野獸\n先輩 ", "bio_md": "**hi** <script>x</script>"})
    data = r.json()
    assert data["display_name"] == "野獸先輩"
    assert "<strong>hi</strong>" in data["bio_html"]
    assert "<script>" not in data["bio_html"]
    assert client.patch("/api/me", json={"display_name": "x" * 33}).json()["detail"] == "name_invalid"
    assert client.patch("/api/me", json={"bio_md": "x" * 2001}).json()["detail"] == "bio_too_long"
    assert client.patch("/api/me", json={"lang": "fr"}).json()["detail"] == "invalid_lang"


def test_public_profile_hides_email(client, sent):
    login(client, sent)
    data = client.get("/api/users/1").json()
    assert "email" not in data and data["uid"] == 1
    assert client.get("/api/users/999").status_code == 404


# ---------- 小卡與分享 ----------

def test_card_and_share_page(client, sent):
    login(client, sent)
    r = client.post("/api/checkin").json()
    png = client.get(r["card_url"])
    assert png.status_code == 200
    img = Image.open(io.BytesIO(png.content))
    assert img.size == (1080, 1350)

    page = client.get(f"/c/1/{r['day']}")
    assert page.status_code == 200 and 'property="og:image"' in page.text
    assert client.get("/card/1/2000-01-01.png").status_code == 404
    assert client.get("/c/1/not-a-day").status_code == 404


def test_card_rerendered_after_rename(client, sent):
    login(client, sent)
    r = client.post("/api/checkin").json()
    first = client.get(r["card_url"]).content
    client.patch("/api/me", json={"display_name": "New Name"})
    assert client.get(r["card_url"]).content != first


def test_index_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/u/1").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
