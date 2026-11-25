"""分享頁 /c/<uid>/<day>：有 og:image，貼到 Discord、X、LINE 會出現小卡預覽。"""
import sqlite3
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from . import __version__, config
from .card import DAY_RE
from .checkin import stats
from .db import get_db
from .deps import fail
from .i18n import norm_lang, t
from .titles import fmt_uid

router = APIRouter()

PAGE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {site}</title>
<meta property="og:type" content="website">
<meta property="og:site_name" content="{site}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{image}">
<meta property="og:image:width" content="1080">
<meta property="og:image:height" content="1350">
<meta property="og:url" content="{url}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/assets/icon-192.png" type="image/png" sizes="192x192">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
         justify-content: center; gap: 24px; background: #1a0f06; color: #fff6ea;
         font-family: system-ui, "Noto Sans CJK TC", sans-serif; padding: 24px; box-sizing: border-box; }}
  img {{ width: min(540px, 100%); border-radius: 16px; box-shadow: 0 12px 40px #0008; }}
  a.open {{ background: #ff8a1f; color: #2a1400; padding: 12px 28px; border-radius: 999px;
            text-decoration: none; font-weight: 700; }}
  footer {{ font-size: 13px; color: #d9b993; text-align: center; line-height: 1.7; }}
  footer a {{ color: #ff8a1f; }}
</style>
</head>
<body>
<img src="{image_path}" alt="{title}">
<a class="open" href="/">{open}</a>
<footer>{about}<br><a href="https://official-website.meridianproject.tw/talents/yuzumi" target="_blank" rel="noopener noreferrer">official-website.meridianproject.tw/talents/yuzumi</a><br>v{version}</footer>
</body>
</html>"""


@router.get("/c/{uid}/{day}", response_class=HTMLResponse)
def share_page(uid: int, day: str, conn: sqlite3.Connection = Depends(get_db)):
    if not DAY_RE.match(day):
        fail(404, "not_found")
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    if user is None or conn.execute(
            "SELECT 1 FROM checkins WHERE uid = ? AND day = ?", (uid, day)).fetchone() is None:
        fail(404, "not_found")
    lang = norm_lang(user["lang"])
    total, _ = stats(conn, uid, day)
    # ?c= 讓吱吱次數變了之後，預覽圖的網址也跟著變（小卡有 10 分鐘快取）
    image_path = f"/card/{fmt_uid(uid)}/{day}.png?c={user['clicks']}"
    return PAGE.format(
        lang=lang,
        site="吱鼯計畫",
        title=escape(t(lang, "share.title", name=user["display_name"])),
        desc=escape(t(lang, "share.text", n=total)),
        image=escape(config.PUBLIC_URL + image_path),
        image_path=image_path,
        url=escape(f"{config.PUBLIC_URL}/c/{fmt_uid(uid)}/{day}"),
        open=escape(t(lang, "share.open")),
        about=escape(t(lang, "about.yuzumi")),
        version=__version__,
    )
