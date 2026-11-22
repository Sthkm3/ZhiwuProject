"""管理員指定 UID 的顏色與稱號。"""
import sqlite3

from app import admin, config, db
from app.titles import text_on

from .conftest import ADMIN_LOGIN, admin_code, login


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def admin_token(client, fake_clock):
    client.post("/api/auth/request-code", json={"email": ADMIN_LOGIN, "lang": "zh-TW"})
    r = client.post("/api/auth/verify", json={"email": ADMIN_LOGIN, "code": admin_code(fake_clock), "lang": "zh-TW"})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["token"]


def test_default_has_no_title(client, sent):
    u = login(client, sent)["user"]
    assert u["title"] == "" and u["title_color"] is None and u["uid_tier"] is None
    assert client.get("/api/users/0").json()["uid_tier"] == "admin"   # 管理員預設樣式還在


def test_admin_sets_and_clears_title(client, sent, fake_clock):
    login(client, sent)
    card_url = client.post("/api/checkin").json()["card_url"]
    plain = client.get(card_url).content
    adm = admin_token(client, fake_clock)

    r = client.put("/api/admin/users/1/title", json={"title": " 首席鼠黑 ", "color": "#3366FF"}, headers=bearer(adm))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "首席鼠黑" and r.json()["title_color"] == "#3366ff"
    # 公開資料看得到，小卡也重畫了
    u = client.get("/api/users/1").json()
    assert u["title"] == "首席鼠黑" and u["title_color"] == "#3366ff"
    titled = client.get(card_url).content
    assert titled != plain

    # 清除
    r = client.put("/api/admin/users/1/title", json={"title": "", "color": ""}, headers=bearer(adm))
    assert r.json()["title"] == "" and r.json()["title_color"] is None
    assert client.get(card_url).content == plain


def test_title_or_color_alone(client, sent, fake_clock):
    login(client, sent)
    adm = admin_token(client, fake_clock)
    # 只給稱號：顏色用主色
    r = client.put("/api/admin/users/1/title", json={"title": "吱吱"}, headers=bearer(adm)).json()
    assert r["title"] == "吱吱" and r["title_color"] == "#ff8a1f"
    # 只給顏色：沒有稱號
    r = client.put("/api/admin/users/1/title", json={"color": "#000000"}, headers=bearer(adm)).json()
    assert r["title"] == "" and r["title_color"] == "#000000"
    # 管理員自己也可以改
    r = client.put("/api/admin/users/0/title", json={"title": "鼠王", "color": "#ff0000"}, headers=bearer(adm)).json()
    assert r["title"] == "鼠王" and r["uid_tier"] == "admin"


def test_title_validation_and_permission(client, sent, fake_clock):
    tok = login(client, sent)["token"]
    client.cookies.clear()
    # 一般使用者不能設（自己的也不行），沒登入也不行
    assert client.put("/api/admin/users/1/title", json={"title": "x"}, headers=bearer(tok)).status_code == 403
    assert client.put("/api/admin/users/1/title", json={"title": "x"}).status_code == 401
    adm = admin_token(client, fake_clock)
    for body, code in (({"title": "字" * (config.TITLE_MAX + 1)}, "title_too_long"),
                       ({"color": "red"}, "invalid_color"), ({"color": "#12345"}, "invalid_color"),
                       ({"color": "#12345g"}, "invalid_color")):
        r = client.put("/api/admin/users/1/title", json=body, headers=bearer(adm))
        assert r.status_code == 400 and r.json()["detail"] == code, body
    r = client.put("/api/admin/users/999/title", json={"title": "x"}, headers=bearer(adm))
    assert r.status_code == 404 and r.json()["detail"] == "user_not_found"
    assert client.get("/api/users/1").json()["title"] == ""


def test_review_page_shows_uploader_title(client, sent, fake_clock):
    from .test_submissions import upload

    tok = login(client, sent)["token"]
    client.cookies.clear()
    upload(client, tok)
    adm = admin_token(client, fake_clock)
    client.put("/api/admin/users/1/title", json={"title": "投稿王", "color": "#00aa55"}, headers=bearer(adm))
    up = client.get("/api/admin/submissions", headers=bearer(adm)).json()["item"]["uploader"]
    assert up["title"] == "投稿王" and up["title_color"] == "#00aa55"


def test_set_title_cli(client, sent, capsys):
    login(client, sent)
    admin.main(["set-title", "1", "--title", "測試", "--color", "#ABCDEF"])
    assert client.get("/api/users/1").json()["title_color"] == "#abcdef"
    admin.main(["set-title", "1", "--clear"])
    u = client.get("/api/users/1").json()
    assert u["title"] == "" and u["title_color"] is None


def test_text_colour_follows_brightness():
    assert text_on((255, 209, 102)) == (42, 20, 0)      # 亮底用深色字
    assert text_on((0, 0, 0)) == (255, 255, 255)        # 暗底用白字
    assert text_on((51, 102, 255)) == (255, 255, 255)


def test_old_database_gets_new_columns(client, sent):
    """已經在跑的資料庫（沒有 title 欄位）重啟後自動補欄位，資料不動。"""
    login(client, sent)
    conn = sqlite3.connect(db.db_path(), isolation_level=None)
    conn.execute("ALTER TABLE users DROP COLUMN title")
    conn.execute("ALTER TABLE users DROP COLUMN title_color")
    conn.close()
    db.init()
    u = client.get("/api/me").json()
    assert u["title"] == "" and u["title_color"] is None and u["uid_str"] == "00000001"
