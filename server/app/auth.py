"""信箱驗證碼登入（沒有密碼）：第一次驗證成功就自動建立帳號。

管理員：信箱欄輸入 ADMIN_LOGIN，驗證碼是 Aegis 算的 TOTP（不寄信）。錯 3 次就全域鎖定，時間逐次加倍。
"""
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from . import clock, config, db, mailer, totp
from .db import get_db, get_meta, set_meta, transaction
from .deps import client_ip, current_user, fail, session_token, sha256
from .i18n import norm_lang
from .users import create_user, profile

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 同一 IP 的寄信次數（單一 worker，放記憶體就夠）
_ip_hits: dict[str, deque] = defaultdict(deque)
_ip_lock = threading.Lock()


def ip_allowed(ip: str) -> bool:
    now = time.monotonic()
    with _ip_lock:
        q = _ip_hits[ip]
        while q and q[0] < now - config.IP_WINDOW:
            q.popleft()
        if len(q) >= config.IP_LIMIT:
            return False
        q.append(now)
        return True


def is_admin_login(raw: str) -> bool:
    return db.admin_ok and raw.strip().lower() == config.ADMIN_LOGIN.lower()


def norm_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        fail(400, "invalid_email")
    return email


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
    if is_admin_login(body.email):
        # 管理員不寄信，驗證碼看 Aegis。回應和一般信箱完全相同
        if not ip_allowed(client_ip(request)):
            fail(429, "too_many_requests")
        return sent

    email = norm_email(body.email)
    now = clock.ts()
    conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))

    old = conn.execute("SELECT last_sent_at FROM login_codes WHERE email = ?", (email,)).fetchone()
    if old and now - old["last_sent_at"] < config.CODE_RESEND:
        fail(429, "too_many_requests")
    if not ip_allowed(client_ip(request)):
        fail(429, "too_many_requests")

    # secrets 才是密碼學安全的亂數
    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute(
        "INSERT INTO login_codes(email, code_hash, attempts, expires_at, last_sent_at) VALUES (?, ?, 0, ?, ?) "
        "ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, attempts = 0, "
        "expires_at = excluded.expires_at, last_sent_at = excluded.last_sent_at",
        (email, sha256(code), now + config.CODE_TTL, now),
    )
    try:
        mailer.send_code(email, code, norm_lang(body.lang))
    except Exception as e:
        print("Error sending email:", e, flush=True)
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
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
    if is_admin_login(body.email):
        err = admin_check(conn, body.code.strip(), client_ip(request))
        if err:
            fail(429 if err == "too_many_requests" else 400, err)
        admin = conn.execute("SELECT * FROM users WHERE uid = ?", (config.ADMIN_UID,)).fetchone()
        return issue_session(conn, response, admin)

    email = norm_email(body.email)
    code = body.code.strip()
    now = clock.ts()

    row = conn.execute("SELECT * FROM login_codes WHERE email = ?", (email,)).fetchone()
    if row is None:
        fail(400, "not_requested")
    if row["expires_at"] < now:
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
        fail(400, "expired")

    # 錯太多次直接作廢，防止暴力猜碼
    if not secrets.compare_digest(sha256(code), row["code_hash"]):
        attempts = row["attempts"] + 1
        if attempts >= config.CODE_MAX_ATTEMPTS:
            conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
            fail(400, "expired")
        conn.execute("UPDATE login_codes SET attempts = ? WHERE email = ?", (attempts, email))
        fail(400, "wrong_code")

    # 驗證碼只能用一次
    conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
    user = create_user(conn, email, body.lang)
    return issue_session(conn, response, user)


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(get_db)):
    token = session_token(request)
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256(token),))
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"status": "ok"}
