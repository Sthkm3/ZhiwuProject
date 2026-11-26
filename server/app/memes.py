"""圖庫：抽卡用的圖、管理員管理圖片（新增、改出處、下架）。

- 圖檔在 MEME_DIR，檔名存進簽到紀錄，所以圖不刪只下架：不再被抽到，舊小卡照舊畫得出來
- memes 表只記有設定過出處或下架的圖；直接放進資料夾的圖照樣會被抽到
"""
import io
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from . import clock, config, db, images, limits
from .db import get_db, transaction
from .deps import admin_user, fail

router = APIRouter()

MEME_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MEME_NAME = re.compile(r"^zhiwu_(\d+)\.[A-Za-z0-9]+$")
THUMB_SIZE = 320


# ---------- 圖庫清單 ----------

def all_files() -> list[str]:
    """資料夾裡所有的圖（含下架的）。"""
    if not config.MEME_DIR.is_dir():
        return []
    return sorted(
        p.name for p in config.MEME_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in MEME_EXTS and not p.name.startswith(".")
    )


def _hidden(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM memes WHERE hidden_at IS NOT NULL")}


def list_memes() -> list[str]:
    """會被抽到的圖（上架中）。"""
    files = all_files()
    if not files:
        return []
    conn = db.connect()
    try:
        hidden = _hidden(conn)
    finally:
        conn.close()
    return [f for f in files if f not in hidden]


def source_of(name: str | None, conn: sqlite3.Connection | None = None) -> str:
    if not name:
        return ""
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT source FROM memes WHERE name = ?", (name,)).fetchone()
        return row[0] if row else ""
    finally:
        if own:
            conn.close()


def clean_source(text: str | None) -> str:
    """出處只當純文字顯示：去掉控制字元，最多 SOURCE_MAX 字。"""
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) not in ("Cc", "Cf", "Cs")).strip()
    if len(text) > config.SOURCE_MAX:
        fail(400, "source_too_long")
    return text


# ---------- 存圖 ----------

def process_meme(data: bytes) -> tuple[bytes, str]:
    """重新編碼成 JPEG（有透明度就用 PNG）：去掉 EXIF（含 GPS）和偽裝成圖片的內容，GIF 只取第一格。"""
    im, has_alpha = images.decode(data)
    if max(im.size) > config.SUBMIT_MAX_SIDE:
        im.thumbnail((config.SUBMIT_MAX_SIDE, config.SUBMIT_MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    if has_alpha:
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "png"
    im.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue(), "jpg"


def write_atomic(path: Path, data: bytes) -> None:
    """先寫成 . 開頭的暫存檔（抽卡會略過），完整寫好再改名。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)  # mkstemp 是 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def next_meme_number() -> int:
    """迷因資料夾裡 zhiwu_<n>.* 的最大編號 + 1。"""
    nums = [int(m.group(1)) for p in config.MEME_DIR.iterdir() if (m := MEME_NAME.match(p.name))] \
        if config.MEME_DIR.is_dir() else []
    return max(nums, default=-1) + 1


def store_meme(conn: sqlite3.Connection, data: bytes, ext: str, source: str) -> str:
    """存成 zhiwu_<下一號>.<ext> 並記下出處，回傳檔名。要在 transaction(conn) 裡呼叫，兩張同時存才不會拿到同一號。"""
    config.MEME_DIR.mkdir(parents=True, exist_ok=True)
    n = next_meme_number()
    while any(config.MEME_DIR.glob(f"zhiwu_{n}.*")):
        n += 1
    name = f"zhiwu_{n}.{ext}"
    write_atomic(config.MEME_DIR / name, data)
    if source:
        set_meme(conn, name, source=source)
    return name


def set_meme(conn: sqlite3.Connection, name: str, *, source: str | None = None, hidden: bool | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO memes(name) VALUES (?)", (name,))
    if source is not None:
        conn.execute("UPDATE memes SET source = ? WHERE name = ?", (source, name))
    if hidden is not None:
        conn.execute("UPDATE memes SET hidden_at = ? WHERE name = ?", (clock.ts() if hidden else None, name))


def forget_cards(conn: sqlite3.Connection, name: str) -> None:
    """出處改了：用到這張圖的小卡快取都要重畫。"""
    from .card import card_dir   # card 也會用到這個模組，放這裡避免循環匯入
    for uid, day in conn.execute("SELECT uid, day FROM checkins WHERE meme = ?", (name,)):
        for p in card_dir(uid).glob(f"{day}.v*.png"):
            p.unlink(missing_ok=True)


# ---------- 管理員 ----------

def _check_name(name: str) -> str:
    """只接受圖庫裡真的有的檔名，不拼使用者給的路徑。"""
    if name not in all_files():
        fail(404, "not_found")
    return name


def _item(name: str, row: sqlite3.Row | None, used: int) -> dict:
    mtime = int((config.MEME_DIR / name).stat().st_mtime)
    return {
        "name": name,
        "source": row["source"] if row else "",
        "hidden": bool(row and row["hidden_at"]),
        "used": used,
        "thumb_url": f"/api/admin/memes/{name}/thumb?v={mtime}",
    }


def _one(conn: sqlite3.Connection, name: str) -> dict:
    row = conn.execute("SELECT * FROM memes WHERE name = ?", (name,)).fetchone()
    used = conn.execute("SELECT COUNT(*) FROM checkins WHERE meme = ?", (name,)).fetchone()[0]
    return _item(name, row, used)


@router.get("/api/admin/memes")
def list_all(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM memes")}
    used = dict(conn.execute("SELECT meme, COUNT(*) FROM checkins WHERE meme IS NOT NULL GROUP BY meme").fetchall())
    # 新的在前面：zhiwu_<n> 依編號，其他檔名排在後面
    files = sorted(all_files(), key=lambda f: (-(int(m.group(1)) if (m := MEME_NAME.match(f)) else -1), f))
    return {"items": [_item(f, rows.get(f), used.get(f, 0)) for f in files]}


_add_hits = limits.Window("ADMIN_ADD_LIMIT", "UPLOAD_WINDOW")


@router.post("/api/admin/memes")
def add(file: UploadFile = File(...), source: str = Form(""), admin=Depends(admin_user),
        conn: sqlite3.Connection = Depends(get_db)):
    """管理員直接上架：和投稿一樣重新編碼，但不用審核。"""
    source = clean_source(source)
    if not _add_hits.allow(admin["uid"]):
        fail(429, "too_frequent")
    data = file.file.read(config.SUBMIT_MAX_BYTES + 1)
    if len(data) > config.SUBMIT_MAX_BYTES:
        fail(413, "file_too_large")
    with images.slot():
        body, ext = process_meme(data)
    with transaction(conn):
        name = store_meme(conn, body, ext, source)
    print(f"[images] added {name}", flush=True)
    return _one(conn, name)


class MemeIn(BaseModel):
    source: str | None = None
    hidden: bool | None = None


@router.put("/api/admin/memes/{name}")
def update(name: str, body: MemeIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    name = _check_name(name)
    source = None if body.source is None else clean_source(body.source)
    with transaction(conn):
        old = source_of(name, conn)
        set_meme(conn, name, source=source, hidden=body.hidden)
    if source is not None and source != old:
        forget_cards(conn, name)
    if body.hidden is not None:
        print(f"[images] {'hide' if body.hidden else 'show'} {name}", flush=True)
    return _one(conn, name)


@router.get("/api/admin/memes/{name}/thumb")
def thumb(name: str, admin=Depends(admin_user)):
    name = _check_name(name)
    src = config.MEME_DIR / name
    folder = config.DATA_DIR / "thumbs"
    path = folder / f"{name}.webp"
    if not path.exists() or path.stat().st_mtime < src.stat().st_mtime:
        with images.slot():
            im = images.load(src)
            if im is None:
                fail(404, "not_found")
            im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=80)
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(path, out.getvalue())
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})
