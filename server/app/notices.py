"""管理員通知匣：被標記的帳號、太頻繁、伺服器錯誤等重要訊息。

不記信箱、驗證碼、token。寫通知失敗只印 log，不影響原本的請求。
"""
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import clock, config, db
from .db import get_db
from .deps import admin_user
from .titles import fmt_uid

router = APIRouter()

PAGE_SIZE = 50


def add(kind: str, uid: int | None = None, detail: str = "") -> None:
    """自己開一條連線；不要在交易裡呼叫（會等自己的寫入鎖）。"""
    try:
        conn = db.connect()
        try:
            now = clock.ts()
            with db.transaction(conn):
                cur = conn.execute(
                    "UPDATE notices SET count = count + 1, updated_at = ?, read_at = NULL "
                    "WHERE kind = ? AND uid IS ? AND detail = ? AND updated_at > ?",
                    (now, kind, uid, detail, now - config.NOTICE_MERGE))
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT INTO notices(kind, uid, detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (kind, uid, detail, now, now))
                    conn.execute(
                        "DELETE FROM notices WHERE id NOT IN "
                        "(SELECT id FROM notices ORDER BY updated_at DESC, id DESC LIMIT ?)", (config.NOTICE_KEEP,))
        finally:
            conn.close()
    except Exception as e:
        print(f"[notice] {kind} failed: {type(e).__name__}: {e}", flush=True)


def unread_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM notices WHERE read_at IS NULL").fetchone()[0]


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@router.get("/api/admin/notices")
def list_notices(offset: int = Query(0, ge=0), admin=Depends(admin_user),
                 conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT n.*, u.display_name FROM notices n LEFT JOIN users u ON u.uid = n.uid "
        "ORDER BY n.updated_at DESC, n.id DESC LIMIT ? OFFSET ?", (PAGE_SIZE, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    return {
        "items": [{
            "id": r["id"], "kind": r["kind"], "detail": r["detail"], "count": r["count"],
            "uid_str": None if r["uid"] is None else fmt_uid(r["uid"]), "display_name": r["display_name"],
            "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"]),
            "unread": r["read_at"] is None,
        } for r in rows],
        "total": total,
        "upto": max((r["updated_at"] for r in rows), default=0),
    }


class ReadIn(BaseModel):
    upto: int


@router.post("/api/admin/notices/read")
def mark_read(body: ReadIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """只標到畫面上最新的那一則為止，之後才進來的維持未讀。"""
    conn.execute("UPDATE notices SET read_at = ? WHERE read_at IS NULL AND updated_at <= ?",
                 (clock.ts(), body.upto))
    return {"unread": unread_count(conn)}
