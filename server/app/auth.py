"""信箱驗證碼登入（沒有密碼）：第一次驗證成功就自動建立帳號。

管理員：信箱欄輸入 ADMIN_LOGIN，驗證碼是 Aegis 算的 TOTP（不寄信）。錯 3 次就全域鎖定，時間逐次加倍。
對外的回應和一般信箱完全相同（包括鎖定時），別人沒辦法從回應試出哪個是管理員。
"""
import random
import secrets
import sqlite3
import time
from collections import deque

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from . import clock, config, db, limits, mailer, notices, totp
from .db import get_db, get_meta, set_meta, transaction
from .deps import client_ip, current_user, fail, session_token, sha256
from .i18n import norm_lang
from .users import create_user, profile

router = APIRouter()

_ip_mail = limits.Window("IP_LIMIT", "IP_WINDOW")              # 同一 IP 的寄信次數
_all_mail = limits.Window("MAIL_LIMIT", "MAIL_WINDOW")         # 全站的寄信次數
_day_mail = limits.Window("MAIL_DAY_LIMIT", "MAIL_DAY_WINDOW")
_send_times: deque = deque(maxlen=20)   # 最近實際寄信花的秒數；管理員不寄信，用它等差不多久


def is_admin(conn: sqlite3.Connection, email: str) -> bool:
    """管理員的信箱。就算管理員登入停用了也算：不能寄信給它，也不能用一般流程登入。"""
    row = conn.execute("SELECT uid FROM users WHERE email = ?", (email,)).fetchone()
    return row is not None and row["uid"] == config.ADMIN_UID


def norm_email(email: str) -> str:
    email = email.strip().lower()
    if not limits.valid_email(email):
        fail(400, "invalid_email")
    return email


def mail_allowed() -> bool:
    for window, name in ((_day_mail, "24 h"), (_all_mail, "10 min")):
        if not window.allow("all"):
            notices.add("mail_limit", None, name)
            return False
    return True


class CodeRequest(BaseModel):
    email: str
    lang: str = config.DEFAULT_LANG


class VerifyRequest(BaseModel):
    email: str
    code: str
    lang: str = config.DEFAULT_LANG


@router.post("/api/auth/request-code")
def request_code(body: CodeRequest, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    sent = {"status": "sent", "expires_in": config.CODE_TTL, "resend_in": config.CODE_RESEND}
    email = norm_email(body.email)
    now = clock.ts()
    conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))

    old = conn.execute("SELECT last_sent_at FROM login_codes WHERE email = ?", (email,)).fetchone()
    if old and now - old["last_sent_at"] < config.CODE_RESEND:
        fail(429, "too_many_requests")
    if not _ip_mail.allow(limits.ip_key(client_ip(request))) or not mail_allowed():
        fail(429, "too_many_requests")

    # secrets 才是密碼學安全的亂數
    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute(
        "INSERT INTO login_codes(email, code_hash, attempts, expires_at, last_sent_at) VALUES (?, ?, 0, ?, ?) "
        "ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, attempts = 0, "
        "expires_at = excluded.expires_at, last_sent_at = excluded.last_sent_at",
        (email, sha256(code), now + config.CODE_TTL, now),
    )
    if is_admin(conn, email):
        # 管理員不寄信，驗證碼看 Aegis。等一段和實際寄信差不多的時間，從快慢也看不出來
        if config.SMTP_USER and config.SMTP_PASSWORD:
            time.sleep((random.choice(_send_times) if _send_times else 1.5) * random.uniform(0.9, 1.1))
        return sent
    started = time.monotonic()
    try:
        mailer.send_code(email, code, norm_lang(body.lang))
        _send_times.append(time.monotonic() - started)
    except Exception as e:
        print("Error sending email:", e, flush=True)
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
        notices.add("mail_failed", None, type(e).__name__)
        fail(502, "send_failed")
    return sent


def admin_check(conn: sqlite3.Connection, code: str, ip: str) -> str | None:
    """比對管理員 TOTP。成功回傳 None，失敗回傳錯誤代碼。
    計數寫在交易裡，同時送出多個請求也不會多猜幾次。"""
    now = clock.ts()
    with transaction(conn):
        if now < get_meta(conn, "admin_lock_until"):
            return "too_many_requests"
        step = totp.verify(config.ADMIN_TOTP, code, now)
        if step is not None and step <= get_meta(conn, "admin_last_step"):
            # 碼是對的但用過了（例如登出後馬上用同一組再登入）：拒絕，但不算猜錯、不計入鎖定。
            # 拿得到正確的碼才會發生，不是暴力猜碼
            return "code_used"
        if step is not None:
            set_meta(conn, "admin_fails", 0)
            set_meta(conn, "admin_lock_level", 0)
            set_meta(conn, "admin_last_step", step)
            print(f"[ADMIN] login from {ip}", flush=True)
            return None
        fails = get_meta(conn, "admin_fails") + 1
        print(f"[ADMIN] login failed from {ip} ({fails}/{config.ADMIN_MAX_FAILS})", flush=True)
        if fails < config.ADMIN_MAX_FAILS:
            set_meta(conn, "admin_fails", fails)
            return "wrong_code"
        level = get_meta(conn, "admin_lock_level")
        lock = min(config.ADMIN_LOCK_BASE * 2 ** level, config.ADMIN_LOCK_MAX)
        set_meta(conn, "admin_fails", 0)
        set_meta(conn, "admin_lock_level", level + 1)
        set_meta(conn, "admin_lock_until", now + lock)
        print(f"[ADMIN] locked for {lock // 60} min", flush=True)
    notices.add("admin_locked", None, f"{lock // 60} min")   # 交易結束後才寫
    return "too_many_requests"


def issue_session(conn: sqlite3.Connection, response: Response, user: sqlite3.Row) -> dict:
    now = clock.ts()
    ttl = config.ADMIN_SESSION_TTL if user["uid"] == config.ADMIN_UID else config.SESSION_TTL
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions(token_hash, uid, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (sha256(token), user["uid"], now, now + ttl),
    )
    response.set_cookie(
        config.COOKIE_NAME, token, max_age=ttl,
        httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/",
    )
    # token 也放在 JSON，之後 Android 用 Authorization: Bearer <token>
    return {"token": token, "user": profile(conn, user, user["lang"], private=True)}


@router.post("/api/auth/verify")
def verify(body: VerifyRequest, request: Request, response: Response,
           conn: sqlite3.Connection = Depends(get_db)):
    email = norm_email(body.email)
    code = body.code.strip()
    now = clock.ts()

    row = conn.execute("SELECT * FROM login_codes WHERE email = ?", (email,)).fetchone()
    if row is None:
        fail(400, "not_requested")
    if row["expires_at"] < now:
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
        fail(400, "expired")

    # 先用掉一次機會再比對（一句 SQL，同時送很多個也不會多猜）。認 code_hash：不會扣到剛重寄的新碼
    this_code = (email, row["code_hash"])
    used = conn.execute("UPDATE login_codes SET attempts = attempts + 1 "
                        "WHERE email = ? AND code_hash = ? AND attempts < ? RETURNING attempts",
                        (*this_code, config.CODE_MAX_ATTEMPTS)).fetchall()
    if not used:
        fail(400, "expired")

    admin = is_admin(conn, email)
    if admin:
        # 停用時一律不對；鎖定中也只當作碼錯了，不透露這是管理員
        err = admin_check(conn, code, client_ip(request)) if db.admin_ok else "wrong_code"
        if err == "code_used":   # 碼是對的，只是用過了：不算猜錯
            conn.execute("UPDATE login_codes SET attempts = attempts - 1 WHERE email = ? AND code_hash = ?", this_code)
            fail(400, err)
        ok = err is None
    else:
        ok = code.isascii() and secrets.compare_digest(sha256(code), row["code_hash"])

    # 錯太多次直接作廢，防止暴力猜碼
    if not ok:
        if used[0]["attempts"] >= config.CODE_MAX_ATTEMPTS:
            conn.execute("DELETE FROM login_codes WHERE email = ? AND code_hash = ?", this_code)
            fail(400, "expired")
        fail(400, "wrong_code")

    # 驗證碼只能用一次（同時送兩個對的，只有一個成功）
    if conn.execute("DELETE FROM login_codes WHERE email = ? AND code_hash = ?", this_code).rowcount == 0:
        fail(400, "not_requested")
    user = create_user(conn, email, body.lang)
    if admin:
        agent = "".join(ch for ch in request.headers.get("user-agent", "") if ch.isprintable())[:160]
        country = request.headers.get("cf-ipcountry", "")[:8]
        notices.add("admin_login", None, " · ".join(x for x in (client_ip(request)[:64], country, agent) if x),
                    merge=False)
    return issue_session(conn, response, user)


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(get_db)):
    token = session_token(request)
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256(token),))
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"status": "ok"}
