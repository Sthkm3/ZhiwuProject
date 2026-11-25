from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import auth, checkin, clock, config, db, limits, mailer, totp

ADMIN_LOGIN = "boss-7f3a@admin.invalid"
ADMIN_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # 測試用密鑰


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)  # 台灣時間 11:00

    def set(self, iso: str):
        self.value = datetime.fromisoformat(iso).astimezone(timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def fake_clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(clock, "now", c)
    return c


@pytest.fixture
def sent(monkeypatch):
    """攔截寄信，回傳 {email: 最後一個驗證碼}。"""
    box = {}
    monkeypatch.setattr(mailer, "send_code", lambda email, code, lang: box.__setitem__(email, code))
    return box


@pytest.fixture
def client(tmp_path, monkeypatch, fake_clock, sent):
    memes = tmp_path / "memes"
    memes.mkdir()
    Image.new("RGB", (640, 480), (200, 80, 80)).save(memes / "test.png")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MEME_DIR", memes)
    monkeypatch.setattr(config, "ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setattr(config, "ADMIN_TOTP", ADMIN_TOTP)
    for w in limits.Window._all:
        w.clear()
    for d in (checkin._next, checkin._rejects, checkin._cool):
        d.clear()
    from app.main import app
    with TestClient(app, headers={"X-ZHIWU": "1"}) as c:
        yield c


def login(client, sent, email="a@example.com", lang="zh-TW"):
    r = client.post("/api/auth/request-code", json={"email": email, "lang": lang})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/verify", json={"email": email, "code": sent[email.lower()], "lang": lang})
    assert r.status_code == 200, r.text
    return r.json()


def admin_verify(client, code, email=ADMIN_LOGIN, headers=None):
    """管理員登入：和一般人一樣要先要驗證碼（這裡略過 60 秒限制），再輸入 TOTP。"""
    conn = db.connect()
    conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
    conn.close()
    auth._ip_mail.clear()
    r = client.post("/api/auth/request-code", json={"email": email})
    assert r.status_code == 200, r.text
    return client.post("/api/auth/verify", json={"email": email, "code": code}, headers=headers)


def admin_code(fake_clock, offset_steps=0):
    """用假時鐘算出管理員的 TOTP。"""
    step = int(fake_clock.value.timestamp()) // totp.PERIOD + offset_steps
    return totp.code_at(totp.decode_secret(ADMIN_TOTP), step)
