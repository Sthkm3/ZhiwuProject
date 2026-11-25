"""限流（放記憶體，只有一個 worker）、IP 分組、信箱格式、請求大小上限。"""
import ipaddress
import re
import threading
import time
from collections import deque

from fastapi import HTTPException
from starlette.responses import JSONResponse

from . import config

# 只收一個單純的 ASCII 地址。<a@b.c>、x,a@b.c 這類寫法會寄到同一個人，卻被當成不同信箱
EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])")


def valid_email(email: str) -> bool:
    return len(email) <= 254 and EMAIL_RE.fullmatch(email) is not None


def ip_key(ip: str) -> str:
    """限流用的 IP。IPv6 的使用者手上通常有一整段 /64 可以換，所以算同一個。"""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip[:64]
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network((int(addr), 64), strict=False))
    return str(addr)


class Window:
    """同一個 key 在 window 秒內最多 limit 次。傳 config 的名稱，測試改值才會生效。"""
    _all: list["Window"] = []

    def __init__(self, limit: str, window: str):
        self._limit, self._window = limit, window
        self._hits: dict = {}
        self._lock = threading.Lock()
        Window._all.append(self)

    def allow(self, key) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - getattr(config, self._window):
                q.popleft()
            if len(q) >= getattr(config, self._limit):
                return False
            q.append(now)
            return True

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def sweep(self) -> None:
        """丟掉已經過期的 key，不然來過的 IP 會一直佔著記憶體。"""
        old = time.monotonic() - getattr(config, self._window)
        with self._lock:
            for key in [k for k, q in self._hits.items() if not q or q[-1] < old]:
                del self._hits[key]


def sweep_all() -> None:
    for w in Window._all:
        w.sweep()


class BodyLimit:
    """擋太大的請求。沒有這個的話，還沒檢查登入就會先把整個請求收完。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = config.UPLOAD_BODY_MAX if scope["path"] in config.UPLOAD_PATHS else config.BODY_MAX
        too_big = JSONResponse({"detail": "file_too_large"}, status_code=413)
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length[:20]) > limit:
            return await too_big(scope, receive, send)
        got, replaced = 0, False

        async def limited():  # chunked 沒有 Content-Length，邊收邊算
            nonlocal got
            message = await receive()
            if message["type"] == "http.request":
                got += len(message.get("body", b""))
                if got > limit:
                    raise HTTPException(413, "file_too_large")
            return message

        async def answer(message):  # 裡面會把上面的例外變成 400，統一換成 413
            nonlocal replaced
            if got <= limit:
                await send(message)
            elif not replaced:
                replaced = True
                await too_big(scope, receive, send)

        await self.app(scope, limited, answer)
