"""1.0.2 發版前的漏洞修補：寄信限制、管理員不可探測、圖片、請求大小、儲存權限。"""
import io
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from PIL import Image

from app import anticheat, auth, config, db, images, limits, main, users

from .conftest import ADMIN_LOGIN, admin_code, admin_verify, login


def ask(client, email, ip="198.51.100.1"):
    return client.post("/api/auth/request-code", json={"email": email}, headers={"CF-Connecting-IP": ip})


def verify(client, email, code):
    return client.post("/api/auth/verify", json={"email": email, "code": code})


def bomb(w, h):
    """檔頭寫很大、內容很小的 PNG。"""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * 1000)) + chunk(b"IEND", b""))


def img(fmt, size=(40, 30), **kw):
    buf = io.BytesIO()
    Image.new("RGB", size, (9, 99, 200)).save(buf, fmt, **kw)
    return buf.getvalue()


def put_avatar(client, data):
    return client.put("/api/me/avatar", files={"file": ("a", data, "image/png")})


# ---------- 寄信 ----------

@pytest.mark.parametrize("email", [
    "<v@example.com>", "a<v@example.com>", "z,v@example.com", "(c)v@example.com", '"x"<v@example.com>', "v@example",
    "v@-a.com", "v@a..com", "v v@example.com", "v@example.com\nBcc: x@y.zz", "ｖ@example.com", "\ud800@a.co", "v@a.1", "",
    "a" * 65 + "@example.com", "v@" + "a." * 130 + "com",
])
def test_odd_emails_rejected(client, sent, email):
    import json
    r = client.post("/api/auth/request-code", content=json.dumps({"email": email}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_email" and sent == {}


@pytest.mark.parametrize("email", ["a.b+c_d-e@sub.example.co.jp", "x@xn--fiqs8s.xn--fiqz9s", "1@a1.tw", "o'neil@example.com"])
def test_normal_emails_accepted(email):
    assert limits.valid_email(email)


def test_ip_key():
    assert limits.ip_key("2001:db8:1:1::5") == limits.ip_key("2001:db8:1:1:ffff::9")
    assert limits.ip_key("2001:db8:1:1::5") != limits.ip_key("2001:db8:1:2::5")
    assert limits.ip_key("::ffff:203.0.113.9") == limits.ip_key("203.0.113.9") == "203.0.113.9"
    assert limits.ip_key("203.0.113.9") != limits.ip_key("203.0.113.10")
    assert limits.ip_key("testclient") == "testclient"


def test_ipv6_64_shares_limit(client, sent):
    codes = [ask(client, f"u{i}@example.com", f"2001:db8:1:1::{i:x}").status_code for i in range(12)]
    assert codes == [200] * config.IP_LIMIT + [429] * 2


@pytest.mark.parametrize("name", ["MAIL_LIMIT", "MAIL_DAY_LIMIT"])
def test_site_wide_mail_limit(client, sent, monkeypatch, name):
    monkeypatch.setattr(config, name, 3)
    codes = [ask(client, f"u{i}@example.com", f"203.0.113.{i}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429] and len(sent) == 3
    conn = db.connect()
    assert [(r["kind"], r["count"]) for r in conn.execute("SELECT * FROM notices")] == [("mail_limit", 2)]
    conn.close()


def test_sweep_drops_expired_only(monkeypatch):
    auth._ip_mail.clear()
    now = [1000.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: now[0])
    auth._ip_mail.allow("old")
    now[0] += config.IP_WINDOW - 1
    auth._ip_mail.allow("new")
    now[0] += 2
    limits.sweep_all()
    assert list(auth._ip_mail._hits) == ["new"]


def test_hourly_loop_sweeps(monkeypatch):
    import asyncio
    called = []

    async def fake_sleep(_):
        if called:
            raise asyncio.CancelledError
    monkeypatch.setattr(anticheat.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(anticheat, "check_safe", lambda: None)
    monkeypatch.setattr(limits, "sweep_all", lambda: called.append(1))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(anticheat.loop())
    assert called == [1]


# ---------- 驗證碼：同時送也不能多猜、不能重複用 ----------

def test_parallel_guesses_are_capped(client, sent, monkeypatch):
    ask(client, "a@example.com")
    compared = []
    real = auth.secrets.compare_digest
    monkeypatch.setattr(auth.secrets, "compare_digest", lambda a, b: compared.append(1) or real(a, b))
    with ThreadPoolExecutor(30) as ex:
        codes = list(ex.map(lambda i: verify(client, "a@example.com", f"x{i}").json()["detail"], range(150)))
    assert len(compared) == config.CODE_MAX_ATTEMPTS
    assert codes.count("wrong_code") == config.CODE_MAX_ATTEMPTS - 1


def test_parallel_right_code_works_once(client, sent):
    ask(client, "a@example.com")
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: verify(client, "a@example.com", sent["a@example.com"]).status_code, range(30)))
    assert codes.count(200) == 1


def test_fifth_try_can_still_succeed(client, sent):
    ask(client, "a@example.com")
    for _ in range(config.CODE_MAX_ATTEMPTS - 1):
        assert verify(client, "a@example.com", "x").json()["detail"] == "wrong_code"
    assert verify(client, "a@example.com", sent["a@example.com"]).status_code == 200


def test_used_admin_code_does_not_burn_attempts(client, fake_clock):
    assert admin_verify(client, admin_code(fake_clock)).status_code == 200
    ask(client, ADMIN_LOGIN)
    for _ in range(config.CODE_MAX_ATTEMPTS + 2):
        assert verify(client, ADMIN_LOGIN, admin_code(fake_clock)).json()["detail"] == "code_used"


# ---------- 管理員和一般信箱的回應要完全一樣 ----------

def walk(client, email, fake_clock):
    """把外人能做的事走一遍，記下每一步的回應。"""
    out = []

    def rec(r):
        out.append((r.status_code, r.text, sorted(k for k in r.headers if k not in ("date", "content-length"))))

    rec(verify(client, email, "999999"))                    # 還沒要驗證碼
    rec(ask(client, email))
    rec(ask(client, email))                                 # 60 秒內重寄
    for code in ("999999", "１２３４５６", "", "abc", "é"):
        rec(verify(client, email, code))                    # 第 5 次作廢（管理員第 3 次已鎖定）
    rec(verify(client, email, "999999"))
    fake_clock.value += timedelta(seconds=61)
    rec(ask(client, email))
    rec(verify(client, email, "999999"))                    # 管理員鎖定中
    fake_clock.value += timedelta(minutes=6)
    rec(verify(client, email, "999999"))                    # 過期
    return out


def test_admin_is_indistinguishable(client, sent, fake_clock, monkeypatch):
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: 123457)
    start = fake_clock.value
    normal = walk(client, "someone@example.com", fake_clock)
    fake_clock.value = start
    auth._ip_mail.clear()
    assert walk(client, ADMIN_LOGIN, fake_clock) == normal
    assert ADMIN_LOGIN not in sent
    conn = db.connect()
    assert db.get_meta(conn, "admin_lock_level") == 1        # 裡面還是有鎖
    conn.close()


def test_surrogate_code_is_same_for_everyone(client, sent):
    for email in ("someone@example.com", ADMIN_LOGIN):
        ask(client, email)
        r = client.post("/api/auth/verify", content='{"email":"%s","code":"\\ud800"}' % email,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["detail"] == "wrong_code"


def test_admin_waits_like_real_mail(client, sent, monkeypatch):
    slept = []
    monkeypatch.setattr(auth.time, "sleep", slept.append)
    ask(client, ADMIN_LOGIN)
    assert slept == []                                       # 沒設 SMTP：一般信箱也是立刻回
    db.connect().execute("DELETE FROM login_codes")
    monkeypatch.setattr(config, "SMTP_USER", "u")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "p")
    auth._send_times.clear()
    auth._send_times.append(2.0)
    ask(client, ADMIN_LOGIN)
    assert len(slept) == 1 and 1.8 <= slept[0] <= 2.2


@pytest.mark.parametrize("how", ["no_totp", "bad_totp", "not_email", "no_login"])
def test_disabled_admin_cannot_be_taken_over_by_mail(client, sent, fake_clock, monkeypatch, how):
    """管理員停用後，就算那個信箱真的存在，也不能用寄信的驗證碼登入成管理員。"""
    key, value = {"no_totp": ("ADMIN_TOTP", ""), "bad_totp": ("ADMIN_TOTP", "!!"),
                  "not_email": ("ADMIN_LOGIN", "boss"), "no_login": ("ADMIN_LOGIN", "")}[how]
    monkeypatch.setattr(config, key, value)
    db.init()
    assert db.admin_ok is False
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: 123456)
    assert ask(client, ADMIN_LOGIN).status_code == 200 and sent == {}
    assert verify(client, ADMIN_LOGIN, "123456").json()["detail"] == "wrong_code"
    assert verify(client, ADMIN_LOGIN, admin_code(fake_clock)).json()["detail"] == "wrong_code"


def test_admin_login_is_recorded(client, fake_clock):
    for _ in range(2):
        r = admin_verify(client, admin_code(fake_clock), headers={
            "CF-Connecting-IP": "203.0.113.7", "CF-IPCountry": "TW", "User-Agent": "Test/1.0 " + "x" * 300})
        assert r.status_code == 200
        fake_clock.value += timedelta(seconds=30)
    r = client.get("/api/admin/notices").json()
    assert [(n["kind"], n["count"], n["unread"]) for n in r["items"]] == [("admin_login", 1, True)] * 2
    assert r["items"][0]["detail"].startswith("203.0.113.7 · TW · Test/1.0 xxx") and len(r["items"][0]["detail"]) < 200


def test_normal_login_is_not_recorded(client, sent):
    login(client, sent)
    assert db.connect().execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0


# ---------- 圖片 ----------

def test_bomb_rejected_before_decode(client, sent, monkeypatch):
    login(client, sent)
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("不該解碼"))
    r = put_avatar(client, bomb(8900, 8900))
    assert r.status_code == 400 and r.json()["detail"] == "invalid_image"


@pytest.mark.parametrize("fmt,ok", [("PNG", 1), ("JPEG", 1), ("WEBP", 1), ("GIF", 1),
                                    ("TIFF", 0), ("BMP", 0), ("ICO", 0), ("TGA", 0), ("PPM", 0), ("PCX", 0)])
def test_only_four_formats(client, sent, fmt, ok):
    login(client, sent)
    assert put_avatar(client, img(fmt)).status_code == (200 if ok else 400)
    r = client.post("/api/submissions", files={"file": ("a.png", img(fmt), "image/png")})
    assert r.status_code == (200 if ok else 400)


def test_psd_rejected(client, sent):
    login(client, sent)
    psd = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, 4, 4, 8, 3) + b"\0" * 14 + b"\0" * 48
    with Image.open(io.BytesIO(psd)) as im:
        assert im.format == "PSD"                            # 這個檔本身是讀得到的
    assert put_avatar(client, psd).status_code == 400


def test_exif_rotation_kept(client, sent):
    exif = Image.Exif()
    exif[0x0112] = 6
    im, alpha = images.decode(img("JPEG", (40, 30), exif=exif))
    assert im.size == (30, 40) and alpha is False


def test_avatar_three_per_minute(client, sent):
    login(client, sent)
    codes = [put_avatar(client, d).status_code for d in (img("PNG"), b"bad", img("PNG"), img("PNG"))]
    assert codes == [200, 400, 200, 429]                     # 失敗也算
    assert put_avatar(client, img("PNG")).json()["detail"] == "too_frequent"
    client.cookies.clear()
    login(client, sent, "b@example.com")
    assert put_avatar(client, img("PNG")).status_code == 200


def test_one_image_at_a_time(client, sent, monkeypatch):
    login(client, sent)
    monkeypatch.setattr(config, "IMAGE_WAIT", 0.05)
    with images.slot():
        r = put_avatar(client, img("PNG"))
        assert r.status_code == 429 and r.json()["detail"] == "too_frequent"
    assert put_avatar(client, img("PNG")).status_code == 200  # 失敗後鎖有放開
    assert put_avatar(client, b"bad").status_code == 400
    assert images._slot.acquire(timeout=0) and images._slot.release() is None


# ---------- 請求大小、路徑 ----------

def test_body_limit_by_length(client):
    r = client.post("/api/auth/request-code", json={"email": "a" * config.BODY_MAX})
    assert r.status_code == 413 and r.json()["detail"] == "file_too_large"
    r = client.post("/api/submissions", files={"file": ("a", b"\0" * config.UPLOAD_BODY_MAX)})
    assert r.status_code == 413


@pytest.mark.parametrize("path,ctype,limit", [
    ("/api/auth/request-code", "application/json", config.BODY_MAX),
    ("/api/submissions", "multipart/form-data; boundary=b", config.UPLOAD_BODY_MAX),
])
def test_body_limit_chunked(client, path, ctype, limit):
    got = [0]

    def body():
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\n' if "multi" in ctype else b'{"email":"'
        for _ in range(limit // 32768 + 40):
            got[0] += 1
            yield b"a" * 32768
    r = client.post(path, content=body(), headers={"Content-Type": ctype})
    assert r.status_code == 413 and r.json()["detail"] == "file_too_large"


def test_five_mb_file_still_allowed(client, sent):
    login(client, sent)
    data = img("PNG")
    assert put_avatar(client, data + b"\0" * (config.AVATAR_MAX_BYTES - len(data))).status_code == 200


def test_csrf_not_bypassed_by_host(client, sent):
    login(client, sent)
    r = client.post("/api/checkin", headers={"X-ZHIWU": "", "Host": "x/y?"})
    assert r.status_code in (400, 403)


# ---------- 儲存權限 ----------

@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="要在非 root 的 Linux 上測")
def test_storage_check(tmp_path, monkeypatch, capsys):
    data, memes = tmp_path / "d", tmp_path / "m"
    data.mkdir(), memes.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "MEME_DIR", memes)
    main.check_storage()
    memes.chmod(0o500)
    main.check_storage()
    assert "[WARN]" in capsys.readouterr().out
    (data / "zhiwu.db").touch(0o400)
    with pytest.raises(RuntimeError, match="zhiwu.db"):
        main.check_storage()
    memes.chmod(0o700)


def test_users_module_has_no_direct_open():
    assert not hasattr(users, "Image") or "Image.open" not in open(users.__file__, encoding="utf-8").read()
