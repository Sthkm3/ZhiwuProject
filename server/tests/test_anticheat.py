"""1.0.2：1.5 秒限制、冷靜時間、理論上限標記、管理員通知匣、版本顯示。"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import __version__, anticheat, card, checkin, config, db, mailer, notices, users

from .conftest import admin_code, admin_verify, login
from .test_zhi import frozen  # noqa: F401  （fixture）


def as_admin(client, fake_clock):
    client.cookies.clear()
    r = admin_verify(client, admin_code(fake_clock))
    assert r.status_code == 200, r.text
    forget_login()


def zhi(client):
    return client.post("/api/zhi", json={"n": 1})


def forget_login():
    """管理員登入本身也會發一則通知，測其他通知時先拿掉。"""
    conn = db.connect()
    conn.execute("DELETE FROM notices WHERE kind = 'admin_login'")
    conn.close()


# ---------- 冷靜時間 ----------

def test_cooldown_after_too_many_rejects(client, sent, frozen, fake_clock):  # noqa: F811
    login(client, sent)
    assert zhi(client).json()["added"] == 1
    for _ in range(config.COOL_REJECTS - 1):
        assert zhi(client).json()["added"] == 0
    r = zhi(client)
    assert r.status_code == 429 and r.json()["detail"] == "cooldown"
    frozen[0] += config.COOL_SECONDS - 0.1       # 冷靜期間怎麼送都不算，也不會延長
    assert zhi(client).status_code == 429
    frozen[0] += 0.2
    assert zhi(client).json() == {"clicks": 2, "added": 1}
    as_admin(client, fake_clock)
    items = client.get("/api/admin/notices").json()["items"]
    assert [(n["kind"], n["uid_str"], n["count"]) for n in items] == [("cooldown", "00000001", 1)]


def test_normal_pace_never_cools_down(client, sent, frozen):  # noqa: F811
    login(client, sent)
    for i in range(60):
        frozen[0] += 1.6 + (0.25 if i % 2 else -0.25)   # 網路抖動：一下慢一下快
        assert zhi(client).json()["added"] == 1


# ---------- 理論上限 ----------

def set_clicks(uid, n):
    conn = db.connect()
    conn.execute("UPDATE users SET clicks = ? WHERE uid = ?", (n, uid))
    conn.close()


def test_flag_over_limit(client, sent, fake_clock):
    login(client, sent)
    fake_clock.set("2026-11-21T12:00:00+08:00")        # 架設後 1 小時：上限 2400
    set_clicks(1, 2400)
    set_clicks(0, 999999)                              # 管理員不檢查
    assert anticheat.check() == []
    set_clicks(1, 2401)
    assert anticheat.check() == [1]
    assert anticheat.check() == []                     # 已標記的不重複通知
    assert "flagged" not in client.get("/api/users/1").json()          # 本人和其他人看不到
    assert "flagged_at" not in client.get("/api/me").json()
    assert client.delete("/api/admin/users/1/flag").status_code == 403

    as_admin(client, fake_clock)
    assert client.get("/api/users/1").json()["flagged"] is True
    assert client.get("/api/me").json()["unread_notices"] == 1
    r = client.get("/api/admin/notices").json()
    assert r["items"][0]["kind"] == "flagged" and r["items"][0]["detail"] == "2,401 > 2,400"
    assert client.delete("/api/admin/users/1/flag").status_code == 200
    assert client.get("/api/users/1").json()["flagged"] is False
    assert client.delete("/api/admin/users/99/flag").status_code == 404
    assert anticheat.check() == [1]                    # 仍超過就再標記


def test_upgrade_from_101(tmp_path, monkeypatch):
    """1.0.1 的資料庫：補欄位與表，最初架設時間用最早的帳號。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ADMIN_LOGIN", "")
    conn = sqlite3.connect(tmp_path / "zhiwu.db")
    conn.execute("CREATE TABLE users (uid INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, "
                 "display_name TEXT NOT NULL, bio_md TEXT NOT NULL DEFAULT '', lang TEXT NOT NULL, "
                 "avatar_ver INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0, "
                 "title TEXT NOT NULL DEFAULT '', title_color TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)")
    for uid, at in ((1, 2000), (2, 1000)):
        conn.execute("INSERT INTO users(uid, email, display_name, lang, clicks, created_at) VALUES (?, ?, 'x', 'en', 7, ?)",
                     (uid, f"{uid}@example.com", at))
    conn.commit()
    conn.close()
    db.init()
    db.init()   # 再啟動一次也不會變
    conn = db.connect()
    assert db.get_meta(conn, "server_start") == 1000
    row = conn.execute("SELECT clicks, flagged_at FROM users WHERE uid = 1").fetchone()
    assert row["clicks"] == 7 and row["flagged_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0
    conn.close()


# ---------- 通知匣 ----------

def test_notices_admin_only(client, sent):
    assert client.get("/api/admin/notices").status_code == 401
    login(client, sent)
    assert client.get("/api/admin/notices").status_code == 403
    assert client.post("/api/admin/notices/read", json={"upto": 1}).status_code == 403
    assert "unread_notices" not in client.get("/api/me").json()


def test_notices_merge_read_and_keep(client, sent, fake_clock, monkeypatch):
    as_admin(client, fake_clock)
    notices.add("error", None, "a")
    notices.add("error", None, "a")
    notices.add("error", None, "b")
    r = client.get("/api/admin/notices").json()
    assert [(n["detail"], n["count"]) for n in r["items"]] == [("b", 1), ("a", 2)]
    assert client.post("/api/admin/notices/read", json={"upto": r["upto"]}).json() == {"unread": 0}
    fake_clock.set("2026-11-21T11:30:00+08:00")
    notices.add("error", None, "a")               # 一小時內：併進去，變回未讀
    assert client.get("/api/me").json()["unread_notices"] == 1
    assert client.post("/api/admin/notices/read", json={"upto": r["upto"]}).json() == {"unread": 1}   # 之後才進來的不算看過
    fake_clock.set("2026-11-21T13:00:00+08:00")
    notices.add("error", None, "a")               # 超過一小時：新的一則
    assert client.get("/api/admin/notices").json()["total"] == 3
    monkeypatch.setattr(config, "NOTICE_KEEP", 2)
    notices.add("error", None, "c")
    r = client.get("/api/admin/notices").json()
    assert r["total"] == 2 and r["items"][0]["detail"] == "c"
    assert client.get("/api/admin/notices?offset=1").json()["items"][0]["detail"] == "a"


def test_notice_failure_is_swallowed(monkeypatch):
    def boom():
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(db, "connect", boom)
    notices.add("error")


def test_error_mail_and_lock_notices(client, sent, fake_clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret a@example.com")
    monkeypatch.setattr(mailer, "send_code", boom)
    assert client.post("/api/auth/request-code", json={"email": "a@example.com"}).status_code == 502
    for _ in range(config.ADMIN_MAX_FAILS):
        admin_verify(client, "abc")
    monkeypatch.setattr(users, "render_md", boom)
    with pytest.raises(RuntimeError):
        client.get("/api/users/0")
    conn = db.connect()
    rows = [(r["kind"], r["detail"]) for r in conn.execute("SELECT * FROM notices ORDER BY id")]
    conn.close()
    assert rows == [("mail_failed", "RuntimeError"), ("admin_locked", "15 min"),
                    ("error", "GET /api/users/{uid}: RuntimeError")]


# ---------- 其他 ----------

def test_version_shown(client, sent):
    for path in ("/", "/notices"):
        html = client.get(path).text
        assert f"v{__version__}</span>" in html and "<!--" + "APP_VERSION" not in html
    login(client, sent)
    r = client.post("/api/checkin").json()
    assert f"v{__version__}</footer>" in client.get(f"/c/1/{r['day']}").text


@pytest.mark.parametrize("path", [
    f"/api/users/{10**30}", f"/c/{10**30}/2026-11-21", f"/card/{10**30}/2026-11-21.png",
    f"/avatars/{10**30}.webp", f"/avatars/{'9' * 400}.webp", f"/card/guest.png?code=AB12&n={'9' * 5000}",
])
def test_huge_numbers_are_404(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="Windows 沒有 POSIX 權限")
def test_approved_meme_is_world_readable(client, sent, fake_clock):
    from .test_submissions import _img
    login(client, sent)
    sid = client.post("/api/submissions", files={"file": ("a.png", _img(), "image/png")}).json()["item"]["id"]
    as_admin(client, fake_clock)
    name = client.post(f"/api/admin/submissions/{sid}/approve").json()["meme"]
    assert (config.MEME_DIR / name).stat().st_mode & 0o777 == 0o644


def test_parallel_requests_count_once(frozen):  # noqa: F811
    with ThreadPoolExecutor(16) as ex:
        results = [r for r, _ in ex.map(lambda _: checkin._take(1), range(64))]
    assert results.count("ok") == 1


def test_no_beta_left(client):
    assert not hasattr(config, "BETA") and "beta" not in client.get("/").text.lower()
    assert card.CARD_VERSION == 3
