import asyncio
import hashlib
import mimetypes
import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, anticheat, auth, card, checkin, config, db, limits, notices, share, submissions, users

# python:3.12-slim 沒有 /etc/mime.types，這些副檔名會被當成 text/plain；加上 nosniff 後瀏覽器可能拒絕播放
for _type, _ext in (("image/webp", ".webp"), ("video/mp4", ".mp4"), ("audio/mp4", ".m4a"), ("application/manifest+json", ".webmanifest"),
                    ("image/svg+xml", ".svg"), ("image/x-icon", ".ico")):
    mimetypes.add_type(_type, _ext)

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def check_storage() -> None:
    """容器不是用 root 跑：資料夾的擁有者不對時，啟動就講清楚。"""
    who = f"{os.geteuid()}:{os.getegid()}" if hasattr(os, "geteuid") else "?"

    def bad(path):
        return path.exists() and not os.access(path, os.R_OK | os.W_OK | (os.X_OK if path.is_dir() else 0))

    data = config.DATA_DIR
    paths = (data, *(data / n for n in ("zhiwu.db", "zhiwu.db-wal", "zhiwu.db-shm", "avatars", "cards", "submissions")))
    for p in paths:
        if bad(p):
            raise RuntimeError(f"{p} 不能讀寫。目前用 UID:GID {who} 執行，請先 chown -R {who} 掛到 /data 的資料夾")
    if bad(config.MEME_DIR):
        print(f"[WARN] {config.MEME_DIR} 不能讀寫，圖庫或投稿會失敗。請 chown -R {who} 掛到 /memes 的資料夾", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_storage()
    db.init()
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        print("[WARN] SMTP_USER / SMTP_PASSWORD 沒有設定：不會寄信，驗證碼只印在 log（[DEV] verify code ...）",
              flush=True)
    else:
        print(f"[mail] {config.SMTP_USER}@{config.SMTP_HOST}:{config.SMTP_PORT}, from {config.MAIL_FROM}", flush=True)
    await asyncio.to_thread(anticheat.check_safe)
    task = asyncio.create_task(anticheat.loop())
    yield
    task.cancel()


app = FastAPI(title="吱鼯計畫", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # 防 CSRF：會改資料的 /api 請求要帶 X-ZHIWU: 1（跨站表單帶不了自訂 header）。
    # 用 Bearer token 的客戶端（Android）沒有 cookie，不受 CSRF 影響，所以不檢查。
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.scope["path"].startswith("/api/")
            and request.headers.get("x-zhiwu") != "1"
            and not request.headers.get("authorization")):
        return JSONResponse({"detail": "csrf"}, status_code=403)
    try:
        response = await call_next(request)
    except Exception as e:
        # 未處理的錯誤（500）：記到管理員通知匣。只留路由和例外類型，不留參數內容
        route = getattr(request.scope.get("route"), "path", "?")
        await asyncio.to_thread(notices.add, "error", None, f"{request.method} {route}: {type(e).__name__}")
        raise
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.scope["path"].startswith("/assets/"):
        # 沒有這行時 Cloudflare 會自己快取 .js / .css，改版後使用者拿到舊檔。
        # no-cache = 每次向伺服器確認，沒變就回 304
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.add_middleware(limits.BodyLimit)   # 最後加的在最外層

for module in (auth, users, checkin, card, share, submissions, notices):
    app.include_router(module.router)


@app.exception_handler(OverflowError)
def too_big(request: Request, exc: OverflowError):
    # UID 或編號超過 SQLite 整數範圍：一定不存在
    return JSONResponse({"detail": "not_found"}, status_code=404)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@lru_cache(maxsize=1)
def asset_version() -> str:
    """web/ 底下所有檔案內容的雜湊。檔案一改，網址的 ?v= 就變，Cloudflare 和瀏覽器都會抓新的。"""
    h = hashlib.sha256()
    for p in sorted(config.WEB_DIR.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(config.WEB_DIR).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


def index():
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__ASSET_VERSION__", asset_version())
    html = html.replace("<!--APP_VERSION-->", __version__)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.WEB_DIR / "favicon.ico", headers={"Cache-Control": "public, max-age=86400"})


app.add_api_route("/", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/u/{uid}", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/upload", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/review", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/notices", index, methods=["GET", "HEAD"], include_in_schema=False)
app.mount("/assets", StaticFiles(directory=config.WEB_DIR, check_dir=False), name="assets")
