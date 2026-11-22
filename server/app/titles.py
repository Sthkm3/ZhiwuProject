"""UID 的底色與稱號。

- 一般使用者：淺咖啡底
- 管理員（UID 0）預設黃底
- 管理員可以幫任何 UID 指定一個顏色和一個稱號（users.title / users.title_color），會蓋過預設
都只是顯示用，不帶任何權限。顯示的地方：側欄、個人頁、審核頁、小卡。
"""
import re
import sqlite3

from . import config

# 預設的特殊序號：底色、文字色（網頁的 web/style.css .uid.tier-* 用同樣的顏色）
TIER_COLORS = {
    "default": ((201, 162, 122), (42, 20, 0)),    # 淺咖啡 #C9A27A：所有 UID 的預設底色
    "admin": ((255, 209, 102), (42, 20, 0)),      # 黃 #FFD166
}
DEFAULT_TITLE_COLOR = "#ff8a1f"   # 只給稱號沒給顏色時用主色
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_INK, _WHITE = (42, 20, 0), (255, 255, 255)


def fmt_uid(uid: int) -> str:
    return f"{uid:08d}"


def uid_tier(uid: int) -> str | None:
    return "admin" if uid == config.ADMIN_UID else None


def valid_color(color: str) -> bool:
    return bool(_COLOR_RE.match(color))


def hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def text_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """底色上要用深色字還是白字（依亮度）。前端 web/app.js 的 textOn() 用同一條公式。"""
    r, g, b = rgb
    return _INK if (r * 299 + g * 587 + b * 114) / 1000 >= 150 else _WHITE


def badge(user: sqlite3.Row) -> dict:
    """API 回傳用：uid_tier（預設樣式）、title（稱號）、title_color（指定的顏色，#rrggbb 或 None）。"""
    return {
        "uid_tier": uid_tier(user["uid"]),
        "title": user["title"],
        "title_color": user["title_color"] or None,
    }


def badge_colors(user: sqlite3.Row) -> tuple[tuple, tuple]:
    """小卡用：(底色, 文字色)。管理員指定的顏色 > 管理員預設黃 > 一般使用者淺咖啡。"""
    if user["title_color"]:
        bg = hex_rgb(user["title_color"])
        return bg, text_on(bg)
    return TIER_COLORS[uid_tier(user["uid"]) or "default"]
