"""吱吱（點擊）次數：和簽到天數分開記錄，簽到那一下也算一次。"""
import pytest

from app import card, checkin, config

from .conftest import login


@pytest.fixture
def frozen(monkeypatch):
    """固定限流用的時鐘，測試自己決定時間前進多少。"""
    now = [1000.0]
    monkeypatch.setattr(checkin, "_now", lambda: now[0])
    for d in (checkin._next, checkin._rejects, checkin._cool):
        d.clear()
    return now


def test_checkin_counts_one_click_per_day(client, sent, frozen, fake_clock):
    login(client, sent)
    assert client.get("/api/checkin/today").json()["clicks"] == 0
    r = client.post("/api/checkin").json()
    assert r["already"] is False and r["total_days"] == 1 and r["clicks"] == 1
    # 同一天再簽到：不會再加
    r = client.post("/api/checkin").json()
    assert r["already"] is True and r["total_days"] == 1 and r["clicks"] == 1
    # 隔天簽到再加一次
    fake_clock.set("2026-11-22T12:00:00+08:00")
    r = client.post("/api/checkin").json()
    assert r["total_days"] == 2 and r["clicks"] == 2


def test_zhi_adds_clicks_but_not_days(client, sent, frozen):
    login(client, sent)
    client.post("/api/checkin")
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 2, "added": 1}
    frozen[0] += config.ZHI_GAP
    assert client.post("/api/zhi", json={}).json()["clicks"] == 3   # 預設一下
    today = client.get("/api/checkin/today").json()
    assert today["clicks"] == 3 and today["total_days"] == 1 and today["streak"] == 1
    me = client.get("/api/me").json()
    assert me["clicks"] == 3 and me["total_days"] == 1
    assert client.get("/api/users/1").json()["clicks"] == 3


def test_zhi_works_before_checkin(client, sent, frozen):
    """次數和簽到互相獨立：沒簽到也能加（API 不擋，前端只在簽到後才開放）。"""
    login(client, sent)
    assert client.post("/api/zhi", json={"n": 1}).json()["clicks"] == 1
    today = client.get("/api/checkin/today").json()
    assert today["checked_in"] is False and today["total_days"] == 0


@pytest.mark.parametrize("n", [0, -1, config.ZHI_BATCH_MAX + 1])
def test_zhi_rejects_bad_batch(client, sent, frozen, n):
    login(client, sent)
    r = client.post("/api/zhi", json={"n": n})
    assert r.status_code == 400 and r.json()["detail"] == "bad_request"
    assert client.get("/api/me").json()["clicks"] == 0


def test_zhi_once_per_gap(client, sent, frozen):
    """一個帳號 1.5 秒只能吱一次：太快的、一次送很多下的，都只算一下。"""
    login(client, sent)
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 1, "added": 1}
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 1, "added": 0}
    frozen[0] += 1.1
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 1, "added": 0}
    frozen[0] += 0.4
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 2, "added": 1}
    # 等再久也不會累積；一次送 200 下也只加 1
    frozen[0] += 3600
    assert client.post("/api/zhi", json={"n": 200}).json() == {"clicks": 3, "added": 1}
    assert client.post("/api/zhi", json={"n": 200}).json() == {"clicks": 3, "added": 0}


def test_zhi_jitter_does_not_raise_average(frozen):
    """可以提早 0.3 秒，但提早的會扣回來：長期平均還是 1.5 秒一下。"""
    ok = 0
    for _ in range(1000):
        frozen[0] += 1.25
        ok += checkin._take(1)[0] == "ok"
    assert ok <= 1250 / config.ZHI_GAP + 1


def test_zhi_limit_is_per_account(client, sent, frozen):
    login(client, sent, "a@example.com")
    assert client.post("/api/zhi", json={"n": 1}).json()["added"] == 1
    client.cookies.clear()
    login(client, sent, "b@example.com")
    assert client.post("/api/zhi", json={"n": 1}).json() == {"clicks": 1, "added": 1}


def test_zhi_requires_login_and_csrf(client, sent, frozen):
    assert client.post("/api/zhi", json={"n": 1}).status_code == 401
    login(client, sent)
    assert client.post("/api/zhi", json={"n": 1}, headers={"X-ZHIWU": ""}).status_code == 403


def test_card_redrawn_when_clicks_change(client, sent, frozen):
    login(client, sent)
    r = client.post("/api/checkin").json()
    cards = config.DATA_DIR / "cards" / "1"
    first = client.get(r["card_url"])
    assert first.status_code == 200
    assert [p.name for p in cards.iterdir()] == [f"{r['day']}.v{card.CARD_VERSION}.c1.png"]

    client.post("/api/zhi", json={"n": 1})
    second = client.get(r["card_url"] + "?c=2")
    assert second.status_code == 200 and second.content != first.content
    # 舊次數的快取會被清掉，只留最新的一張
    assert [p.name for p in cards.iterdir()] == [f"{r['day']}.v{card.CARD_VERSION}.c2.png"]
    # 次數沒變就直接用快取
    assert client.get(r["card_url"] + "?c=2").content == second.content


def test_card_uses_default_image_when_gallery_empty(client, sent, frozen):
    for p in config.MEME_DIR.iterdir():
        p.unlink()
    login(client, sent)
    r = client.post("/api/checkin").json()
    assert client.get(r["card_url"]).status_code == 200


def test_reset_progress_clears_clicks(client, sent, frozen):
    from app import admin

    login(client, sent)
    client.post("/api/checkin")
    client.post("/api/zhi", json={"n": 1})
    admin.main(["reset-progress", "--yes"])
    today = client.get("/api/checkin/today").json()
    assert today["clicks"] == 0 and today["total_days"] == 0


def test_video_served_with_range(client):
    r = client.get("/assets/media/zhizhi.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.headers["content-type"] == "video/mp4" and len(r.content) == 100
    assert "media-src 'self'" in r.headers["content-security-policy"]
