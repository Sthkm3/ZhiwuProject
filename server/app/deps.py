"""共用的 dependency：錯誤格式、session 驗證、取得使用者 IP。"""
import hashlib
import sqlite3

from fastapi import Depends, HTTPException, Request

from . import clock, config
from .db import get_db


def fail(status: int, code: str):
    """錯誤一律回 {"detail": "<code>"}，前端用 err.<code> 翻譯。"""
    raise HTTPException(status_code=status, detail=code)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def client_ip(request: Request) -> str:
    # port 只綁 127.0.0.1，外部流量都經過 cloudflared，所以可以信任 CF-Connecting-IP
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def session_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(config.COOKIE_NAME)


def optional_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row | None:
    token = session_token(request)
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.uid = s.uid "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (sha256(token), clock.ts()),
    ).fetchone()


def current_user(user: sqlite3.Row | None = Depends(optional_user)) -> sqlite3.Row:
    if user is None:
        fail(401, "unauthorized")
    return user


def admin_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if user["uid"] != config.ADMIN_UID:
        fail(403, "forbidden")
    return user
