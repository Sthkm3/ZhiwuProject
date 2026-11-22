"""用 Pillow 畫吱吱小卡（1080×1350 PNG），第一次被請求時才畫，之後快取在 /data/cards/<uid>/。

小卡上有吱吱（點擊）次數，所以快取檔名帶次數：<day>.v<版本>.c<次數>.png，次數變了就重畫。
"""
import os
import re
import shutil
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import config
from .checkin import stats
from .db import get_db
from .deps import fail
from .i18n import norm_lang, t
from .titles import badge_colors, fmt_uid

Image.MAX_IMAGE_PIXELS = 40_000_000  # 擋解壓縮炸彈

router = APIRouter()

W, H = 1080, 1350
PAD = 48
ACCENT = (255, 138, 31)
TEXT = (255, 246, 234)
MUTED = (217, 185, 147)
INK = (42, 20, 0)
DEFAULT_MEME = Path(__file__).resolve().parent / "assets" / "default-meme.jpg"
CARD_VERSION = 1  # 版面改了就加 1，舊的快取自動作廢
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
        with Image.open(path) as im:
            im.seek(0)  # gif 只取第一幀
            im = ImageOps.exif_transpose(im)
            return im.convert("RGB")
    except Exception:
        return None


def render(user: sqlite3.Row, day: str, meme: str | None, total: int, streak: int, clicks: int) -> Image.Image:
    lang = norm_lang(user["lang"])
    uid = user["uid"]

    # 背景：上下漸層
    img = Image.new("RGB", (W, H))
    top, bottom = (70, 38, 12), (24, 13, 5)
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(top, bottom)))

    # 梗圖
    box_w, box_h = W - 2 * PAD, 760
    panel = Image.new("RGB", (box_w, box_h), (18, 10, 4))
    pic = load_image(config.MEME_DIR / meme) if meme else None
    pic = pic or load_image(DEFAULT_MEME)   # 圖庫是空的（或圖被刪了）就用預設圖
    if pic:
        pic = ImageOps.contain(pic, (box_w, box_h), Image.LANCZOS)
        panel.paste(pic, ((box_w - pic.width) // 2, (box_h - pic.height) // 2))
    else:
        pd = ImageDraw.Draw(panel)
        f = font(lang, 48)
        msg = t(lang, "card.no_meme")
        pd.text((box_w / 2, box_h / 2), msg, font=f, fill=MUTED, anchor="mm")
    img.paste(panel, (PAD, PAD), rounded_mask(panel.size, 28))

    # 大頭貼
    av_size, av_y = 168, PAD + box_h + 40
    avatar = None
    if user["avatar_ver"]:
        avatar = load_image(config.DATA_DIR / "avatars" / f"{uid}.webp")
    if avatar:
        avatar = avatar.resize((av_size, av_size), Image.LANCZOS)
    else:
        avatar = Image.new("RGB", (av_size, av_size), ACCENT)
        ad = ImageDraw.Draw(avatar)
        initial = (user["display_name"] or "?")[0]
        ad.text((av_size / 2, av_size / 2), initial, font=font(lang, 84, True), fill=INK, anchor="mm")
    circle = Image.new("L", (av_size, av_size), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, av_size - 1, av_size - 1), fill=255)
    img.paste(avatar, (PAD, av_y), circle)

    # 名稱、UID、稱號（管理員指定的顏色或預設的特殊序號用背景色標示）
    tx = PAD + av_size + 32
    max_w = W - PAD - tx
    f_name = font(lang, 54, True)
    d.text((tx, av_y + 14), fit_text(d, user["display_name"], f_name, max_w), font=f_name, fill=TEXT)
    uid_text = f"UID {fmt_uid(uid)}"
    colors = badge_colors(user)
    uid_y = av_y + 120  # UID 這一行的垂直中心
    if colors:
        bg, fg = colors
        f_uid = font("en", 36, True)
        tw = d.textlength(uid_text, font=f_uid)
        d.rounded_rectangle((tx, uid_y - 28, tx + tw + 36, uid_y + 28), 14, fill=bg)
        d.text((tx + 18, uid_y), uid_text, font=f_uid, fill=fg, anchor="lm")
        if user["title"]:   # 稱號：接在 UID 後面，同樣的顏色
            x = tx + tw + 36 + 16
            f_title = font(lang, 36, True)
            title = fit_text(d, user["title"], f_title, int(W - PAD - x - 36))
            ttw = d.textlength(title, font=f_title)
            d.rounded_rectangle((x, uid_y - 28, x + ttw + 36, uid_y + 28), 14, fill=bg)
            d.text((x + 18, uid_y), title, font=f_title, fill=fg, anchor="lm")
    else:
        d.text((tx, uid_y), uid_text, font=font("en", 36), fill=MUTED, anchor="lm")

    # 左：簽到天數、連續天數；右：吱吱次數（兩個數字各自獨立）
    line_y = av_y + av_size + 36
    d.line([(PAD, line_y), (W - PAD, line_y)], fill=(110, 72, 36), width=2)
    f_num = font("en", 92, True)
    num = f"{clicks:,}"
    num_w = d.textlength(num, font=f_num)
    d.text((W - PAD, line_y + 24), num, font=f_num, fill=ACCENT, anchor="ra")
    d.text((W - PAD, line_y + 148), t(lang, "card.clicks"), font=font(lang, 36), fill=MUTED, anchor="ra")
    f_day = font(lang, 92, True)
    d.text((PAD, line_y + 24), fit_text(d, t(lang, "card.day", n=total), f_day, int(W - 2 * PAD - num_w - 40)),
           font=f_day, fill=TEXT)
    sub = f"{t(lang, 'card.streak', n=streak)}  ·  {day}"
    d.text((PAD, line_y + 148), sub, font=font(lang, 36), fill=MUTED)

    host = urlparse(config.PUBLIC_URL).netloc or config.PUBLIC_URL
    footer = f"{host} · Beta" if config.BETA else host
    d.text((W - PAD, H - PAD), footer, font=font("en", 30, True), fill=ACCENT, anchor="rs")
    return img


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
