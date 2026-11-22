"""訪客吱吱：伺服器不記任何訪客資料，小卡照網址上的代號和次數即時畫。另外：每個 UID 都有底色、頁尾連結。"""
import io

import pytest
from PIL import Image

from app import card, config, db
from app.titles import TIER_COLORS, badge_colors

from .conftest import login

YUZUMI = "official-website.meridianproject.tw/talents/yuzumi"


@pytest.fixture(autouse=True)
def fresh_guest_state():
    card._guest_hits.clear()
    card._guest_cache.clear()


def test_guest_card_renders_without_login(client):
    r = client.get("/card/guest.png", params={"code": "AB12", "n": 67})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, max-age=60"
    assert Image.open(io.BytesIO(r.content)).size == (1080, 1350)
    # 次數不同、代號不同、語言不同，畫出來都不一樣
    assert client.get("/card/guest.png", params={"code": "AB12", "n": 68}).content != r.content
    assert client.get("/card/guest.png", params={"code": "CD34", "n": 67}).content != r.content
    assert client.get("/card/guest.png", params={"code": "AB12", "n": 67, "lang": "en"}).content != r.content
    # 同樣的參數是同一張（從快取拿）
    assert client.get("/card/guest.png", params={"code": "AB12", "n": 67}).content == r.content


def test_guest_leaves_nothing_on_server(client):
    conn = db.connect()
    counts = lambda: [conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                      for tbl in ("users", "sessions", "checkins", "login_codes")]
    before = counts()
    assert client.get("/card/guest.png", params={"code": "AB12", "n": 5}).status_code == 200
    assert counts() == before
    assert not any((config.DATA_DIR / "cards").iterdir())      # 也不寫硬碟
    assert "set-cookie" not in client.get("/card/guest.png", params={"code": "AB12", "n": 5}).headers


@pytest.mark.parametrize("params", [
    {}, {"code": "ab12"}, {"code": "ABC"}, {"code": "ABCDE"}, {"code": "AB-2"}, {"code": "../x"},
    {"code": "AB12", "n": "-1"}, {"code": "AB12", "n": "abc"}, {"code": "AB12", "n": "1.5"},
    {"code": "AB12", "n": str(config.GUEST_CLICKS_MAX + 1)}, {"code": "AB12", "n": "９"},
])
def test_guest_card_rejects_bad_params(client, params):
    assert client.get("/card/guest.png", params=params).status_code == 404


def test_guest_card_unknown_lang_falls_back(client):
    a = client.get("/card/guest.png", params={"code": "AB12", "n": 1, "lang": "xx"})
    b = client.get("/card/guest.png", params={"code": "AB12", "n": 1, "lang": "zh-TW"})
    assert a.status_code == 200 and a.content == b.content


def test_guest_meme_is_stable_per_day(client, fake_clock):
    for i in range(6):
        Image.new("RGB", (64, 64), (i * 40, 80, 80)).save(config.MEME_DIR / f"m{i}.png")
    day = "2026-11-21"
    assert card.guest_meme("AB12", day) == card.guest_meme("AB12", day)
    picks = {card.guest_meme(code, day) for code in ("AB12", "CD34", "EF56", "GH78", "JK90", "LM23", "NP45", "QR67")}
    assert len(picks) > 1                                     # 不同訪客會抽到不同的圖
    assert len({card.guest_meme("AB12", f"2026-11-{d:02d}") for d in range(1, 29)}) > 1   # 換日會換圖


def test_guest_card_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(config, "GUEST_CARD_LIMIT", 3)
    for n in range(3):
        assert client.get("/card/guest.png", params={"code": "AB12", "n": n}).status_code == 200
    r = client.get("/card/guest.png", params={"code": "AB12", "n": 99})
    assert r.status_code == 429 and r.json()["detail"] == "too_many_requests"
    # 已經畫過的從快取拿，不算次數
    assert client.get("/card/guest.png", params={"code": "AB12", "n": 0}).status_code == 200
    # 別的 IP 不受影響
    other = client.get("/card/guest.png", params={"code": "AB12", "n": 99}, headers={"CF-Connecting-IP": "203.0.113.9"})
    assert other.status_code == 200


def test_guest_cache_is_bounded(client, monkeypatch):
    monkeypatch.setattr(config, "GUEST_CARD_CACHE", 2)
    for n in range(4):
        client.get("/card/guest.png", params={"code": "AB12", "n": n})
    assert len(card._guest_cache) == 2


def test_guest_route_does_not_shadow_user_cards(client, sent):
    login(client, sent)
    r = client.post("/api/checkin").json()
    assert client.get(r["card_url"]).status_code == 200
    assert client.get("/card/guest/2026-11-21.png").status_code == 422     # uid 必須是數字


def test_every_uid_has_a_background(client, sent):
    u = login(client, sent)["user"]
    assert u["uid_tier"] is None and u["title_color"] is None             # API 不變：前端自己套預設底色
    conn = db.connect()
    conn.row_factory = db.sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE uid = 1").fetchone()
    assert badge_colors(row) == TIER_COLORS["default"] == ((201, 162, 122), (42, 20, 0))
    admin = conn.execute("SELECT * FROM users WHERE uid = 0").fetchone()
    assert badge_colors(admin) == TIER_COLORS["admin"]
    conn.execute("UPDATE users SET title_color = '#000000' WHERE uid = 1")
    row = conn.execute("SELECT * FROM users WHERE uid = 1").fetchone()
    assert badge_colors(row) == ((0, 0, 0), (255, 255, 255))              # 指定的顏色優先
    css = client.get("/assets/style.css").text
    assert "#c9a27a" in css


def test_footer_links_to_yuzumi(client, sent):
    for path in ("/", "/upload", "/u/00000001"):
        html = client.get(path).text
        assert f'href="https://{YUZUMI}"' in html and 'class="site-footer"' in html
    login(client, sent)
    r = client.post("/api/checkin").json()
    share = client.get(f"/c/00000001/{r['day']}").text
    assert f'href="https://{YUZUMI}"' in share and "關於Yuzumi" in share


def test_new_copy(client):
    zh = client.get("/assets/i18n/zh-TW.json").json()
    assert zh["checkin.login_first"] == "不是老鼠，是鼯鼠。每天吱吱後領一張吱咪小卡。"
    assert zh["guest.button"] == "訪客吱吱" and zh["about.yuzumi"] == "關於Yuzumi"
    for lang in config.LANGS:   # 每個語系都要有新加的字
        d = client.get(f"/assets/i18n/{lang}.json").json()
        assert set(zh) == set(d), (lang, set(zh) ^ set(d))
