import secrets
import sqlite3
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import clock, config
from .db import get_db, transaction
from .deps import current_user, fail
from .i18n import t
from .titles import fmt_uid

router = APIRouter()

MEME_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def list_memes() -> list[str]:
    if not config.MEME_DIR.is_dir():
        return []
    return sorted(
        p.name for p in config.MEME_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in MEME_EXTS and not p.name.startswith(".")
    )


def pick_meme() -> str | None:
    memes = list_memes()
    return secrets.choice(memes) if memes else None


def stats(conn: sqlite3.Connection, uid: int, upto: str | None = None, *, live: bool = False) -> tuple[int, int]:
    """回傳 (累計天數, 連續天數)，只算 upto（含）以前的簽到。

    live=True 用在個人資料：今天還沒簽到時，昨天以前的連續紀錄仍然算數。
    """
    upto = upto or clock.today()
    days = [r[0] for r in conn.execute(
        "SELECT day FROM checkins WHERE uid = ? AND day <= ? ORDER BY day DESC", (uid, upto))]
    expected = date.fromisoformat(upto)
    if live and days and days[0] != upto:
        expected -= timedelta(days=1)
    streak = 0
    for d in days:
        if d != expected.isoformat():
            break
        streak += 1
        expected -= timedelta(days=1)
    return len(days), streak


def card_urls(uid: int, day: str) -> dict:
    return {
        # 前端會自己加 ?c=<吱吱次數>，次數變了就抓新的小卡
        "card_url": f"/card/{fmt_uid(uid)}/{day}.png",
        "share_url": f"{config.PUBLIC_URL}/c/{fmt_uid(uid)}/{day}",
    }


def today_payload(conn: sqlite3.Connection, user: sqlite3.Row, day: str, already: bool | None = None) -> dict:
    checked = conn.execute(
        "SELECT 1 FROM checkins WHERE uid = ? AND day = ?", (user["uid"], day)).fetchone() is not None
    total, streak = stats(conn, user["uid"], day, live=True)
    clicks = conn.execute("SELECT clicks FROM users WHERE uid = ?", (user["uid"],)).fetchone()[0]
    out = {
        "day": day,
        "checked_in": checked,
        "total_days": total,
        "streak": streak,
        "clicks": clicks,
        "next_reset": clock.next_reset().isoformat(),
    }
    if already is not None:
        out["already"] = already
    if checked:
        out.update(card_urls(user["uid"], day))
        out["share_text"] = t(user["lang"], "share.text", n=total)
    return out


@router.post("/api/checkin")
def do_checkin(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    day = clock.today()  # 只取一次，避免剛好跨過換日時前後不一致
    with transaction(conn):
        # PRIMARY KEY(uid, day) 擋重複簽到：同時送出兩次也只會寫入一筆
        cur = conn.execute(
            "INSERT INTO checkins(uid, day, meme, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uid, day) DO NOTHING",
            (user["uid"], day, pick_meme(), clock.ts()),
        )
        already = cur.rowcount == 0
        if not already:
            # 簽到那一下也算一次吱吱
            conn.execute("UPDATE users SET clicks = clicks + 1 WHERE uid = ?", (user["uid"],))
    return today_payload(conn, user, day, already=already)


# ---------- 繼續吱吱：只加點擊次數，和簽到天數無關 ----------

_now = time.monotonic          # 測試會換掉
_buckets: dict[int, tuple[float, float]] = {}   # uid -> (剩餘額度, 上次更新時間)


def _take(uid: int, n: int) -> int:
    """token bucket：回傳這次實際可以加幾下（超過速率的部分直接丟掉）。"""
    now = _now()
    tokens, last = _buckets.get(uid, (float(config.ZHI_BURST), now))
    tokens = min(float(config.ZHI_BURST), tokens + (now - last) * config.ZHI_RATE)
    ok = min(n, int(tokens))
    _buckets[uid] = (tokens - ok, now)
    return ok


class ZhiIn(BaseModel):
    n: int = 1


@router.post("/api/zhi")
def do_zhi(body: ZhiIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    if not 1 <= body.n <= config.ZHI_BATCH_MAX:
        fail(400, "bad_request")
    added = _take(user["uid"], body.n)
    if added:
        conn.execute("UPDATE users SET clicks = clicks + ? WHERE uid = ?", (added, user["uid"]))
    clicks = conn.execute("SELECT clicks FROM users WHERE uid = ?", (user["uid"],)).fetchone()[0]
    return {"clicks": clicks, "added": added}


@router.get("/api/checkin/today")
def get_today(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return today_payload(conn, user, clock.today())
