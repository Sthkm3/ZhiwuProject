import hashlib
import mimetypes
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, card, checkin, config, db, share, submissions, users

# python:3.12-slim 沒有 /etc/mime.types，這些副檔名會被當成 text/plain；加上 nosniff 後瀏覽器可能拒絕播放
for _type, _ext in (("image/webp", ".webp"), ("video/mp4", ".mp4"), ("audio/mp4", ".m4a"), ("application/manifest+json", ".webmanifest"),
                    ("image/svg+xml", ".svg"), ("image/x-icon", ".ico")):
    mimetypes.add_type(_type, _ext)

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        print("[WARN] SMTP_USER / SMTP_PASSWORD 沒有設定：不會寄信，驗證碼只印在 log（[DEV] verify code ...）",
              flush=True)
    else:
        print(f"[mail] {config.SMTP_USER}@{config.SMTP_HOST}:{config.SMTP_PORT}, from {config.MAIL_FROM}", flush=True)
    yield


app = FastAPI(title="吱鼯計畫", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # 防 CSRF：會改資料的 /api 請求要帶 X-ZHIWU: 1（跨站表單帶不了自訂 header）。
    # 用 Bearer token 的客戶端（Android）沒有 cookie，不受 CSRF 影響，所以不檢查。
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path.startswith("/api/")
            and request.headers.get("x-zhiwu") != "1"
            and not request.headers.get("authorization")):
        return JSONResponse({"detail": "csrf"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/assets/"):
        # 沒有這行時 Cloudflare 會自己快取 .js / .css，改版後使用者拿到舊檔。
        # no-cache = 每次向伺服器確認，沒變就回 304
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


for module in (auth, users, checkin, card, share, submissions):
    app.include_router(module.router)


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
    html = html.replace("<!--BETA_TITLE-->", " (Beta)" if config.BETA else "")
    html = html.replace("<!--BETA_BADGE-->", ' <small class="beta">(Beta)</small>' if config.BETA else "")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.WEB_DIR / "favicon.ico", headers={"Cache-Control": "public, max-age=86400"})


app.add_api_route("/", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/u/{uid}", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/upload", index, methods=["GET", "HEAD"], include_in_schema=False)
app.add_api_route("/review", index, methods=["GET", "HEAD"], include_in_schema=False)
app.mount("/assets", StaticFiles(directory=config.WEB_DIR, check_dir=False), name="assets")
