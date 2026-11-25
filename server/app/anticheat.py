"""反作弊：每小時檢查一次有沒有帳號的吱吱次數超過理論上限。

理論上限 = 從最初架設（meta.server_start）到現在的秒數 / ZHI_GAP。
超過就標記（users.flagged_at）並通知管理員；只是標記，不扣次數也不封鎖。
"""
import asyncio

from . import clock, config, db, notices
from .titles import fmt_uid


def click_limit(conn) -> int:
    start = db.get_meta(conn, "server_start", clock.ts())
    return max(0, int((clock.ts() - start) / config.ZHI_GAP))


def check() -> list[int]:
    """標記超過上限的帳號，回傳這次新標記的 UID。"""
    conn = db.connect()
    try:
        limit = click_limit(conn)
        rows = conn.execute("SELECT uid, clicks FROM users WHERE clicks > ? AND uid != ? AND flagged_at IS NULL",
                            (limit, config.ADMIN_UID)).fetchall()
        for r in rows:
            conn.execute("UPDATE users SET flagged_at = ? WHERE uid = ?", (clock.ts(), r["uid"]))
            notices.add("flagged", r["uid"], f"{r['clicks']:,} > {limit:,}")
            print(f"[anticheat] flagged UID {fmt_uid(r['uid'])} clicks={r['clicks']} limit={limit}", flush=True)
        return [r["uid"] for r in rows]
    finally:
        conn.close()


def check_safe() -> None:
    try:
        check()
    except Exception as e:
        print(f"[anticheat] check failed: {type(e).__name__}: {e}", flush=True)
        notices.add("error", None, f"anticheat: {type(e).__name__}")


async def loop() -> None:
    while True:
        await asyncio.sleep(config.CHECK_INTERVAL)
        await asyncio.to_thread(check_safe)
