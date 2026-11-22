import io
import os
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from markdown_it import MarkdownIt
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import card, clock, config
from .checkin import stats
from .db import get_db, transaction
from .deps import admin_user, current_user, fail
from .i18n import norm_lang
from .titles import DEFAULT_TITLE_COLOR, badge, fmt_uid, valid_color

router = APIRouter()

# 關閉原始 HTML（輸入的 <script> 會被跳脫）與圖片（避免追蹤用的外部圖片）
_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"]).disable("image")


def _link_open(self, tokens, idx, options, env):
    # add_render_rule 會把函式綁到 renderer 上，所以第一個參數是 self
    tokens[idx].attrSet("rel", "nofollow noopener noreferrer")
    tokens[idx].attrSet("target", "_blank")
    return self.renderToken(tokens, idx, options, env)


_md.add_render_rule("link_open", _link_open)


def render_md(text: str) -> str:
    return _md.render(text or "")


# ---------- 帳號建立與 UID 給號 ----------

def create_user(conn: sqlite3.Connection, email: str, lang: str) -> sqlite3.Row:
    """依序給號（從 1 開始；0 是管理員）。不用 AUTOINCREMENT：SQLite 會從現有最大 uid 往後給，
    一旦有人被 grant-uid 指定成很大的號碼，之後的號碼就會全部跳過去。"""
    with transaction(conn):
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            return row
        uid = conn.execute("SELECT value FROM meta WHERE key = 'next_uid'").fetchone()[0]
        # 跳過已經被 grant-uid 指定出去的號碼
        while conn.execute("SELECT 1 FROM users WHERE uid = ?", (uid,)).fetchone():
            uid += 1
        if uid > config.UID_MAX:
            fail(503, "uid_exhausted")
        conn.execute(
            "INSERT INTO users(uid, email, display_name, lang, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, email, f"吱鼯_{uid}", norm_lang(lang), clock.ts()),
        )
        conn.execute("UPDATE meta SET value = ? WHERE key = 'next_uid'", (uid + 1,))
    return conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()


# ---------- 個人資料 ----------

def avatar_url(user: sqlite3.Row) -> str | None:
    return f"/avatars/{fmt_uid(user['uid'])}.webp?v={user['avatar_ver']}" if user["avatar_ver"] else None


def profile(conn: sqlite3.Connection, user: sqlite3.Row, lang: str, private: bool = False) -> dict:
    total, streak = stats(conn, user["uid"], live=True)
    out = {
        "uid": user["uid"],
        "uid_str": fmt_uid(user["uid"]),
        "display_name": user["display_name"],
        **badge(user),
        "bio_md": user["bio_md"],
        "bio_html": render_md(user["bio_md"]),
        "avatar_url": avatar_url(user),
        "total_days": total,
        "streak": streak,
        "clicks": user["clicks"],
        "created_at": datetime.fromtimestamp(user["created_at"], timezone.utc).isoformat(),
    }
    if private:
        out["email"] = user["email"]
        out["lang"] = user["lang"]
        out["is_admin"] = user["uid"] == config.ADMIN_UID
        if out["is_admin"]:
            out["pending_reviews"] = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE status = 'pending'").fetchone()[0]
    return out


def clean_name(name: str) -> str:
    # 去掉換行等控制字元
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cc").strip()
    if not 1 <= len(name) <= config.NAME_MAX:
        fail(400, "name_invalid")
    return name


class ProfilePatch(BaseModel):
    display_name: str | None = None
    bio_md: str | None = None
    lang: str | None = None


@router.get("/api/me")
def get_me(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return profile(conn, user, user["lang"], private=True)


@router.patch("/api/me")
def patch_me(body: ProfilePatch, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    fields = {}
    if body.display_name is not None:
        fields["display_name"] = clean_name(body.display_name)
    if body.bio_md is not None:
        bio = body.bio_md.replace("\r\n", "\n")
        if len(bio) > config.BIO_MAX:
            fail(400, "bio_too_long")
        fields["bio_md"] = bio
    if body.lang is not None:
        if body.lang not in config.LANGS:
            fail(400, "invalid_lang")
        fields["lang"] = body.lang
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE uid = ?", (*fields.values(), user["uid"]))
        if {"display_name", "lang"} & fields.keys():
            card.invalidate(user["uid"])
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (user["uid"],)).fetchone()
    return profile(conn, user, user["lang"], private=True)


# ---------- 大頭貼 ----------

def process_avatar(data: bytes) -> bytes:
    """置中裁成正方形、縮成 1024×1024、重新編成 WebP（順便去掉 EXIF 與偽裝成圖片的內容）。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            im.load()
            im = ImageOps.exif_transpose(im)
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
    except Exception:
        fail(400, "invalid_image")
    size = config.AVATAR_SIZE
    im = ImageOps.fit(im, (size, size), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=90, method=4)
    return out.getvalue()


@router.put("/api/me/avatar")
def put_avatar(file: UploadFile = File(...), user=Depends(current_user),
               conn: sqlite3.Connection = Depends(get_db)):
    data = file.file.read(config.AVATAR_MAX_BYTES + 1)
    if len(data) > config.AVATAR_MAX_BYTES:
        fail(413, "file_too_large")
    webp = process_avatar(data)
    folder = config.DATA_DIR / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(webp)
    os.replace(tmp, folder / f"{user['uid']}.webp")
    conn.execute("UPDATE users SET avatar_ver = ? WHERE uid = ?", (clock.ts(), user["uid"]))
    card.invalidate(user["uid"])
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (user["uid"],)).fetchone()
    return profile(conn, user, user["lang"], private=True)


@router.get("/avatars/{uid}.webp")
def get_avatar(uid: int):
    path = config.DATA_DIR / "avatars" / f"{uid}.webp"
    if not path.exists():
        fail(404, "not_found")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


# ---------- 公開資料 ----------

@router.get("/api/users/{uid}")
def get_user(uid: int, lang: str = Query(config.DEFAULT_LANG), conn: sqlite3.Connection = Depends(get_db)):
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    if user is None:
        fail(404, "user_not_found")
    return profile(conn, user, lang)


# ---------- 管理員：指定 UID 的顏色與稱號 ----------

class TitleIn(BaseModel):
    title: str = ""
    color: str = ""


@router.put("/api/admin/users/{uid}/title")
def set_title(uid: int, body: TitleIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """稱號和顏色都留空 = 清除。只給稱號沒給顏色時用主色；也可以只給顏色不給稱號。"""
    title = "".join(ch for ch in body.title if unicodedata.category(ch) != "Cc").strip()
    color = body.color.strip().lower()
    if len(title) > config.TITLE_MAX:
        fail(400, "title_too_long")
    if color and not valid_color(color):
        fail(400, "invalid_color")
    if title and not color:
        color = DEFAULT_TITLE_COLOR
    cur = conn.execute("UPDATE users SET title = ?, title_color = ? WHERE uid = ?", (title, color, uid))
    if cur.rowcount == 0:
        fail(404, "user_not_found")
    card.invalidate(uid)
    print(f"[ADMIN] title uid={fmt_uid(uid)} title={title!r} color={color or '-'}", flush=True)
    user = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    return profile(conn, user, user["lang"])


class MarkdownIn(BaseModel):
    md: str


@router.post("/api/markdown")
def preview_markdown(body: MarkdownIn, user=Depends(current_user)):
    if len(body.md) > config.BIO_MAX:
        fail(400, "bio_too_long")
    return {"html": render_md(body.md)}
