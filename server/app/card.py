"""用 Pillow 畫吱吱小卡（1080×1350 PNG），第一次被請求時才畫，之後快取在 /data/cards/<uid>/。

小卡上有吱吱（點擊）次數，所以快取檔名帶次數：<day>.v<版本>.c<次數>.png，次數變了就重畫。
"""
import io
import os
import random
import re
import shutil
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import clock, config, limits
from .checkin import list_memes, stats
from .db import get_db
from .deps import client_ip, fail
from .i18n import norm_lang, t
from .images import FORMATS
from .titles import badge_colors, fmt_uid

router = APIRouter()

W, H = 1080, 1350
PAD = 48
ACCENT = (255, 138, 31)
TEXT = (255, 246, 234)
MUTED = (217, 185, 147)
INK = (42, 20, 0)
DEFAULT_MEME = Path(__file__).resolve().parent / "assets" / "default-meme.jpg"
CARD_VERSION = 2  # 版面改了就加 1，舊的快取自動作廢
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Debian 套件 fonts-noto-cjk；同一個 .ttc 裡依序是 JP、KR、SC、TC、HK
NOTO_DIR = Path("/usr/share/fonts/opentype/noto")
NOTO_INDEX = {"ja": 0, "ko": 1, "zh-CN": 2, "zh-TW": 3, "en": 0}
# 本機（Windows）開發用的備援字型
WIN_FONTS = {
    "zh-TW": ["msjh.ttc", "NotoSansTC-VF.ttf"],
    "zh-CN": ["msyh.ttc", "NotoSansSC-VF.ttf", "msjh.ttc"],
    "ja": ["YuGothB.ttc", "YuGothR.ttc", "meiryo.ttc"],
    "ko": ["malgunbd.ttf", "malgun.ttf"],
    "en": ["YuGothB.ttc", "msjh.ttc", "NotoSansTC-VF.ttf"],
}


def _font_candidates(lang: str, bold: bool):
    env = config.FONT_BOLD if bold else config.FONT_REGULAR
    if env:
        yield env, 0
    yield str(NOTO_DIR / ("NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc")), NOTO_INDEX[lang]
    for name in WIN_FONTS[lang]:
        yield os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), 0


@lru_cache(maxsize=64)
def font(lang: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path, index in _font_candidates(lang, bold):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    return ImageFont.load_default(size)


def fit_text(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> str:
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text + "…"


def rounded_mask(size, radius) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def load_image(path: Path) -> Image.Image | None:
    try:
        with Image.open(path, formats=FORMATS) as im:
            im.seek(0)  # gif 只取第一幀
            im = ImageOps.exif_transpose(im)
            return im.convert("RGB")
    except Exception:
        return None


def draw(lang: str, meme: str | None, name: str, avatar: Image.Image | None, badge: dict | None,
         headline: str, sub: str, clicks: int) -> Image.Image:
    """小卡的版面。badge = {"uid": "UID 00000001", "colors": (底色, 文字色), "title": 稱號}；
    訪客沒有 UID，badge 傳 None，那一行就不畫。"""
    # 背景：上下漸層
    img = Image.new("RGB", (W, H))
    top, bottom = (70, 38, 12), (24, 13, 5)
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(top, bottom)))

    # 圖庫抽到的圖
    box_w, box_h = W - 2 * PAD, 760
    panel = Image.new("RGB", (box_w, box_h), (18, 10, 4))
    pic = load_image(config.MEME_DIR / meme) if meme else None
    pic = pic or load_image(DEFAULT_MEME)   # 圖庫是空的（或圖被刪了）就用預設圖
    if pic:
        pic = ImageOps.contain(pic, (box_w, box_h), Image.LANCZOS)
        panel.paste(pic, ((box_w - pic.width) // 2, (box_h - pic.height) // 2))
    else:
        pd = ImageDraw.Draw(panel)
        pd.text((box_w / 2, box_h / 2), t(lang, "card.no_meme"), font=font(lang, 48), fill=MUTED, anchor="mm")
    img.paste(panel, (PAD, PAD), rounded_mask(panel.size, 28))

    # 大頭貼（沒有就用主色圓形 + 名稱第一個字）
    av_size, av_y = 168, PAD + box_h + 40
    if avatar:
        avatar = avatar.resize((av_size, av_size), Image.LANCZOS)
    else:
        avatar = Image.new("RGB", (av_size, av_size), ACCENT)
        ad = ImageDraw.Draw(avatar)
        ad.text((av_size / 2, av_size / 2), (name or "?")[0], font=font(lang, 84, True), fill=INK, anchor="mm")
    circle = Image.new("L", (av_size, av_size), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, av_size - 1, av_size - 1), fill=255)
    img.paste(avatar, (PAD, av_y), circle)

    # 名稱；有 UID 的話下一行是 UID 與稱號（同一個底色）
    tx = PAD + av_size + 32
    max_w = W - PAD - tx
    f_name = font(lang, 54, True)
    if badge:
        d.text((tx, av_y + 14), fit_text(d, name, f_name, max_w), font=f_name, fill=TEXT)
        bg, fg = badge["colors"]
        uid_y = av_y + 120  # UID 這一行的垂直中心
        f_uid = font("en", 36, True)
        tw = d.textlength(badge["uid"], font=f_uid)
        d.rounded_rectangle((tx, uid_y - 28, tx + tw + 36, uid_y + 28), 14, fill=bg)
        d.text((tx + 18, uid_y), badge["uid"], font=f_uid, fill=fg, anchor="lm")
        if badge["title"]:
            x = tx + tw + 36 + 16
            f_title = font(lang, 36, True)
            title = fit_text(d, badge["title"], f_title, int(W - PAD - x - 36))
            ttw = d.textlength(title, font=f_title)
            d.rounded_rectangle((x, uid_y - 28, x + ttw + 36, uid_y + 28), 14, fill=bg)
            d.text((x + 18, uid_y), title, font=f_title, fill=fg, anchor="lm")
    else:
        # 訪客：只有名稱，垂直置中在大頭貼旁邊
        d.text((tx, av_y + av_size / 2), fit_text(d, name, f_name, max_w), font=f_name, fill=TEXT, anchor="lm")

    # 左：大字（簽到天數）與小字；右：吱吱次數
    line_y = av_y + av_size + 36
    d.line([(PAD, line_y), (W - PAD, line_y)], fill=(110, 72, 36), width=2)
    f_num = font("en", 92, True)
    num = f"{clicks:,}"
    num_w = d.textlength(num, font=f_num)
    d.text((W - PAD, line_y + 24), num, font=f_num, fill=ACCENT, anchor="ra")
    d.text((W - PAD, line_y + 148), t(lang, "card.clicks"), font=font(lang, 36), fill=MUTED, anchor="ra")
    f_day = font(lang, 92, True)
    d.text((PAD, line_y + 24), fit_text(d, headline, f_day, int(W - 2 * PAD - num_w - 40)), font=f_day, fill=TEXT)
    d.text((PAD, line_y + 148), sub, font=font(lang, 36), fill=MUTED)

    host = urlparse(config.PUBLIC_URL).netloc or config.PUBLIC_URL
    d.text((W - PAD, H - PAD), host, font=font("en", 30, True), fill=ACCENT, anchor="rs")
    return img


def render(user: sqlite3.Row, day: str, meme: str | None, total: int, streak: int, clicks: int) -> Image.Image:
    """登入使用者的小卡。"""
    lang = norm_lang(user["lang"])
    avatar = load_image(config.DATA_DIR / "avatars" / f"{user['uid']}.webp") if user["avatar_ver"] else None
    badge = {"uid": f"UID {fmt_uid(user['uid'])}", "colors": badge_colors(user), "title": user["title"]}
    return draw(lang, meme, user["display_name"], avatar, badge,
                t(lang, "card.day", n=total), f"{t(lang, 'card.streak', n=streak)}  ·  {day}", clicks)


def render_guest(lang: str, code: str, day: str, meme: str | None, clicks: int) -> Image.Image:
    """訪客的小卡：名稱是「訪客 #XXXX」，沒有 UID、沒有顏色，也沒有簽到天數。"""
    return draw(lang, meme, t(lang, "card.guest_name", code=code), None, None, t(lang, "card.guest"), day, clicks)


def card_dir(uid: int) -> Path:
    return config.DATA_DIR / "cards" / str(uid)


def invalidate(uid: int) -> None:
    """名稱、大頭貼、語言改了之後，舊的小卡重畫。"""
    shutil.rmtree(card_dir(uid), ignore_errors=True)


def card_path(conn: sqlite3.Connection, uid: int, day: str) -> Path | None:
    row = conn.execute("SELECT meme FROM checkins WHERE uid = ? AND day = ?", (uid, day)).fetchone()
    if row is None:
        return None
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    folder = card_dir(uid)
    path = folder / f"{day}.v{CARD_VERSION}.c{user['clicks']}.png"
    if path.exists():
        return path
    total, streak = stats(conn, uid, day)
    img = render(user, day, row["meme"], total, streak, user["clicks"])
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        img.save(f, "PNG", optimize=True)
    os.replace(tmp, path)
    # 同一天舊次數的小卡不會再用到
    for old in folder.glob(f"{day}.v*.png"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                pass   # Windows 上別的請求正在讀，下次再刪
    return path


@router.get("/card/{uid}/{day}.png")
def get_card(uid: int, day: str, conn: sqlite3.Connection = Depends(get_db)):
    if not DAY_RE.match(day):
        fail(404, "not_found")
    path = card_path(conn, uid, day)
    if path is None:
        fail(404, "not_found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=60"})


# ---------- 訪客小卡：伺服器不記任何訪客資料，代號和次數都是瀏覽器自己帶來的 ----------

GUEST_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
_guest_lock = threading.Lock()
_guest_hits = limits.Window("GUEST_CARD_LIMIT", "GUEST_CARD_WINDOW")
_guest_cache: "OrderedDict[tuple, bytes]" = OrderedDict()  # 最近畫過的幾張（PNG）


def guest_meme(code: str, day: str) -> str | None:
    """同一個訪客同一天固定抽到同一張（伺服器沒有地方記，所以用代號 + 日期當亂數種子）。"""
    memes = list_memes()
    return random.Random(f"{code}:{day}").choice(memes) if memes else None


@router.get("/card/guest.png")
def get_guest_card(request: Request, code: str = "", n: str = "0", lang: str = config.DEFAULT_LANG):
    if not GUEST_CODE_RE.match(code) or len(n) > 7 or not n.isascii() or not n.isdigit() or int(n) > config.GUEST_CLICKS_MAX:
        fail(404, "not_found")
    clicks, lang, day = int(n), norm_lang(lang), clock.today()
    meme = guest_meme(code, day)
    key = (code, clicks, lang, day, meme, CARD_VERSION)
    with _guest_lock:
        png = _guest_cache.get(key)
        if png is None:
            if not _guest_hits.allow(limits.ip_key(client_ip(request))):
                fail(429, "too_many_requests")
            buf = io.BytesIO()
            render_guest(lang, code, day, meme, clicks).save(buf, "PNG")
            png = buf.getvalue()
            _guest_cache[key] = png
            while len(_guest_cache) > config.GUEST_CARD_CACHE:
                _guest_cache.popitem(last=False)
        else:
            _guest_cache.move_to_end(key)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})
