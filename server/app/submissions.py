"""使用者投稿迷因圖，管理員審核。

流程：使用者上傳 → 重新編碼後存在 DATA_DIR/submissions/<id>.<ext>（pending）
      → 管理員同意：依 image/ 的命名規則存成 MEME_DIR/zhiwu_<下一號>.<ext>，之後的小卡就可能抽到
      → 管理員拒絕：刪掉檔案，紀錄保留（每日額度要算）
"""
import io
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from PIL import Image

from . import clock, config, images, limits
from .db import get_db, transaction
from .deps import admin_user, current_user, fail
from .titles import badge, fmt_uid

router = APIRouter()

MEME_NAME = re.compile(r"^zhiwu_(\d+)\.[A-Za-z0-9]+$")
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "jpeg": "image/jpeg",
               "webp": "image/webp", "gif": "image/gif"}


def submission_dir() -> Path:
    return config.DATA_DIR / "submissions"


def pending_path(row: sqlite3.Row) -> Path:
    return submission_dir() / f"{row['id']}.{row['ext']}"


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
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def next_meme_number() -> int:
    """迷因資料夾裡 zhiwu_<n>.* 的最大編號 + 1。"""
    nums = [int(m.group(1)) for p in config.MEME_DIR.iterdir() if (m := MEME_NAME.match(p.name))] \
        if config.MEME_DIR.is_dir() else []
    return max(nums, default=-1) + 1


def iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def image_url(row: sqlite3.Row) -> str | None:
    return None if row["status"] == "rejected" else f"/api/submissions/{row['id']}/image"


def used_today(conn: sqlite3.Connection, uid: int, day: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM submissions WHERE uid = ? AND day = ?", (uid, day)).fetchone()[0]


def quota(conn: sqlite3.Connection, uid: int) -> dict:
    used = used_today(conn, uid, clock.today())
    return {"used": used, "limit": config.SUBMIT_PER_DAY, "remaining": max(0, config.SUBMIT_PER_DAY - used)}


def item(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "status": row["status"], "created_at": iso(row["created_at"]),
            "image_url": image_url(row)}


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM submissions WHERE status = 'pending'").fetchone()[0]


# ---------- 使用者 ----------

_upload_hits = limits.Window("SUBMIT_LIMIT", "UPLOAD_WINDOW")


@router.post("/api/submissions")
def upload(file: UploadFile = File(...), user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    if not _upload_hits.allow(user["uid"]):
        fail(429, "too_frequent")
    data = file.file.read(config.SUBMIT_MAX_BYTES + 1)
    if len(data) > config.SUBMIT_MAX_BYTES:
        fail(413, "file_too_large")
    with images.slot():
        body, ext = process_meme(data)  # 先處理圖片，不佔用寫入鎖
    day = clock.today()
    submission_dir().mkdir(parents=True, exist_ok=True)
    # BEGIN IMMEDIATE：同時上傳好幾張也不會超過額度；寫檔失敗會連同 INSERT 一起還原
    with transaction(conn):
        if used_today(conn, user["uid"], day) >= config.SUBMIT_PER_DAY:
            fail(429, "quota_exceeded")
        cur = conn.execute("INSERT INTO submissions(uid, day, ext, created_at) VALUES (?, ?, ?, ?)",
                           (user["uid"], day, ext, clock.ts()))
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (cur.lastrowid,)).fetchone()
        write_atomic(pending_path(row), body)
    return {"item": item(row), **quota(conn, user["uid"])}


@router.get("/api/submissions")
def my_submissions(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM submissions WHERE uid = ? ORDER BY id DESC LIMIT 30", (user["uid"],))
    return {**quota(conn, user["uid"]), "items": [item(r) for r in rows]}


@router.get("/api/submissions/{sid}/image")
def submission_image(sid: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    # 只有上傳者本人和管理員看得到；別人一律當作不存在
    if row is None or (row["uid"] != user["uid"] and user["uid"] != config.ADMIN_UID):
        fail(404, "not_found")
    if row["status"] == "pending":
        path = pending_path(row)
    elif row["status"] == "approved" and row["meme"]:
        path = config.MEME_DIR / row["meme"]
    else:
        fail(404, "not_found")
    if not path.is_file():
        fail(404, "not_found")
    ext = path.suffix.lower().lstrip(".")
    return FileResponse(path, media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=3600"})


# ---------- 管理員審核 ----------

@router.get("/api/admin/submissions")
def next_pending(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """最舊的一張待審投稿（一張一張審）。"""
    row = conn.execute(
        "SELECT s.*, u.display_name, u.title, u.title_color FROM submissions s JOIN users u ON u.uid = s.uid "
        "WHERE s.status = 'pending' ORDER BY s.id LIMIT 1").fetchone()
    left = pending_count(conn)
    if row is None:
        return {"item": None, "pending": left}
    return {
        "item": {
            **item(row),
            "uploader": {"uid": row["uid"], "uid_str": fmt_uid(row["uid"]), **badge(row),
                         "display_name": row["display_name"]},
        },
        "pending": left,
    }


def _load_pending(conn: sqlite3.Connection, sid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        fail(404, "not_found")
    if row["status"] != "pending":
        fail(409, "already_reviewed")
    return row


@router.post("/api/admin/submissions/{sid}/approve")
def approve(sid: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    with transaction(conn):  # 寫入鎖確保兩次同意不會拿到同一個編號
        row = _load_pending(conn, sid)
        src = pending_path(row)
        if not src.is_file():
            fail(404, "not_found")
        config.MEME_DIR.mkdir(parents=True, exist_ok=True)
        n = next_meme_number()
        while any(config.MEME_DIR.glob(f"zhiwu_{n}.*")):
            n += 1
        name = f"zhiwu_{n}.{row['ext']}"
        # 先複製成 . 開頭的暫存檔（抽卡會略過），完整寫好再改名
        fd, tmp = tempfile.mkstemp(dir=config.MEME_DIR, prefix=".", suffix=".tmp")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o644)  # mkstemp 是 0600，圖庫要讓 NAS 上的其他帳號也讀得到
            os.replace(tmp, config.MEME_DIR / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        conn.execute("UPDATE submissions SET status = 'approved', meme = ?, reviewed_at = ? WHERE id = ?",
                     (name, clock.ts(), sid))
    src.unlink(missing_ok=True)
    print(f"[review] approved #{sid} from UID {fmt_uid(row['uid'])} -> {name}", flush=True)
    return {"id": sid, "status": "approved", "meme": name, "pending": pending_count(conn)}


@router.post("/api/admin/submissions/{sid}/reject")
def reject(sid: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    with transaction(conn):
        row = _load_pending(conn, sid)
        conn.execute("UPDATE submissions SET status = 'rejected', reviewed_at = ? WHERE id = ?", (clock.ts(), sid))
    pending_path(row).unlink(missing_ok=True)
    print(f"[review] rejected #{sid} from UID {fmt_uid(row['uid'])}", flush=True)
    return {"id": sid, "status": "rejected", "pending": pending_count(conn)}
