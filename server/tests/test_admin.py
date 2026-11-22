from datetime import timedelta

import pytest

from app import admin, config, db, totp

from .conftest import ADMIN_LOGIN, admin_code, login


def admin_login(client, code, login_name=ADMIN_LOGIN):
    return client.post("/api/auth/verify", json={"email": login_name, "code": code})


def test_totp_rfc6238_vector():
    # RFC 6238 附錄 B（SHA1，密鑰 "12345678901234567890"），取後 6 碼
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.code_at(totp.decode_secret(secret), 59 // 30) == "287082"
    assert totp.code_at(totp.decode_secret(secret), 1111111109 // 30) == "081804"
    assert totp.verify(secret, "287082", 59) == 1
    assert totp.verify(secret, "287082", 59 + 30) == 1      # 前一個時間窗也接受
    assert totp.verify(secret, "287082", 59 + 90) is None   # 太舊
    assert totp.verify(secret, "abc", 59) is None


def test_admin_request_code_does_not_send(client, sent):
    r = client.post("/api/auth/request-code", json={"email": "STHKM3"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert sent == {}


def test_admin_login(client, fake_clock):
    r = admin_login(client, admin_code(fake_clock))
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["uid"] == 0 and user["uid_str"] == "00000000"
    assert user["is_admin"] is True and user["uid_tier"] == "admin" and user["title"] == ""
    assert "Max-Age=604800" in r.headers["set-cookie"]  # 管理員 session 7 天
    me = client.get("/api/me").json()
    assert me["is_admin"] is True
    # 管理員也能簽到，小卡會畫頭銜
    c = client.post("/api/checkin").json()
    assert c["card_url"].startswith("/card/00000000/")
    assert client.get(c["card_url"]).status_code == 200


def test_admin_code_single_use(client, fake_clock):
    code = admin_code(fake_clock)
    assert admin_login(client, code).status_code == 200
    # 登出後馬上用同一組碼再登入：拒絕，但不算失敗，重試幾次都不會鎖
    for _ in range(5):
        r = admin_login(client, code)
        assert r.status_code == 400 and r.json()["detail"] == "code_used"
    # 前一個時間窗的碼也不能用（時間步不能倒退）
    assert admin_login(client, admin_code(fake_clock, -1)).json()["detail"] == "code_used"
    conn = db.connect()
    assert db.get_meta(conn, "admin_fails") == 0 and db.get_meta(conn, "admin_lock_until") == 0
    # Aegis 換下一組碼就能登入
    fake_clock.value += timedelta(seconds=30)
    assert admin_login(client, admin_code(fake_clock)).status_code == 200


def test_admin_lockout(client, fake_clock):
    good = admin_code(fake_clock)
    wrong = "000000" if good != "000000" else "111111"
    assert admin_login(client, wrong).json()["detail"] == "wrong_code"
    assert admin_login(client, wrong).json()["detail"] == "wrong_code"
    r = admin_login(client, wrong)  # 第 3 次就鎖
    assert r.status_code == 429
    # 鎖定中，正確的碼也不行
    assert admin_login(client, good).status_code == 429

    # 15 分鐘後解鎖
    fake_clock.value += timedelta(minutes=15)
    assert admin_login(client, admin_code(fake_clock)).status_code == 200


def test_admin_lock_doubles(client, fake_clock):
    for minutes in (15, 30, 60):
        for _ in range(3):
            admin_login(client, "000000" if admin_code(fake_clock) != "000000" else "111111")
        fake_clock.value += timedelta(minutes=minutes) - timedelta(seconds=1)
        assert admin_login(client, admin_code(fake_clock)).status_code == 429
        fake_clock.value += timedelta(seconds=1)
    conn = db.connect()
    assert db.get_meta(conn, "admin_lock_level") == 3


def test_admin_unlock_command(client, fake_clock):
    for _ in range(3):
        admin_login(client, "000000" if admin_code(fake_clock) != "000000" else "111111")
    assert admin_login(client, admin_code(fake_clock)).status_code == 429
    admin.main(["unlock"])
    assert admin_login(client, admin_code(fake_clock)).status_code == 200


def test_admin_disabled_without_secret(client, sent, fake_clock, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOTP", "")
    db.init()
    r = client.post("/api/auth/request-code", json={"email": ADMIN_LOGIN})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_email"
    assert admin_login(client, admin_code(fake_clock)).json()["detail"] == "invalid_email"


def test_admin_login_renamed(client, fake_clock, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_LOGIN", "boss")
    db.init()
    assert admin_login(client, admin_code(fake_clock), "sthkm3").json()["detail"] == "invalid_email"
    r = admin_login(client, admin_code(fake_clock), "boss")
    assert r.status_code == 200 and r.json()["user"]["uid"] == 0


def test_admin_login_clash_disables(client, sent, fake_clock, monkeypatch):
    login(client, sent, "boss@example.com")
    monkeypatch.setattr(config, "ADMIN_LOGIN", "boss@example.com")
    db.init()
    assert db.admin_ok is False


def test_totp_uri(capsys, client):
    admin.main(["totp-uri"])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("otpauth://totp/%E5%90%B1%E9%BC%AF%E8%A8%88%E7%95%AB%3Asthkm3?secret=")


@pytest.mark.parametrize("uid", ["0", "-1"])
def test_cannot_grant_admin_uid(client, sent, uid):
    login(client, sent)
    with pytest.raises(SystemExit):
        admin.main(["grant-uid", "a@example.com", uid])
