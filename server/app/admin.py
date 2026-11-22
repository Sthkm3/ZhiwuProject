"""管理指令（在容器裡執行）：

    docker compose exec zhiwuproject python -m app.admin info <email>
    docker compose exec zhiwuproject python -m app.admin grant-uid <email> 777
    docker compose exec zhiwuproject python -m app.admin set-title <uid> --title 稱號 --color '#ff8a1f'   # --clear 清除
    docker compose exec zhiwuproject python -m app.admin test-mail <email>
    docker compose exec zhiwuproject python -m app.admin unlock        # 解除管理員登入鎖定
    docker compose exec zhiwuproject python -m app.admin totp-uri      # 印出 Aegis 用的 otpauth:// 連結
    docker compose exec zhiwuproject python -m app.admin reset-progress --yes   # 清空簽到進度與吱吱次數
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

from . import card, config, db, totp
from .titles import fmt_uid


def cmd_info(args):
    conn = db.connect()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (args.email.strip().lower(),)).fetchone()
    if row is None:
        sys.exit("找不到這個信箱")
    days = conn.execute("SELECT COUNT(*) FROM checkins WHERE uid = ?", (row["uid"],)).fetchone()[0]
    print(f"uid={fmt_uid(row['uid'])} name={row['display_name']} lang={row['lang']} checkins={days}")
    next_uid = conn.execute("SELECT value FROM meta WHERE key = 'next_uid'").fetchone()[0]
    print(f"next_uid={next_uid}")


def cmd_set_title(args):
    """指定某個 UID 的顏色與稱號；--clear 清除。網頁上管理員也可以在個人頁設定。"""
    from .titles import DEFAULT_TITLE_COLOR, valid_color
    from . import card

    title, color = ("", "") if args.clear else (args.title.strip(), args.color.strip().lower())
    if len(title) > config.TITLE_MAX:
        sys.exit(f"稱號最多 {config.TITLE_MAX} 個字")
    if color and not valid_color(color):
        sys.exit("顏色要寫成 #rrggbb，例如 #ff8a1f")
    if title and not color:
        color = DEFAULT_TITLE_COLOR
    conn = db.connect()
    cur = conn.execute("UPDATE users SET title = ?, title_color = ? WHERE uid = ?", (title, color, args.uid))
    if cur.rowcount == 0:
        sys.exit("找不到這個 UID")
    card.invalidate(args.uid)
    print(f"uid={fmt_uid(args.uid)} title={title!r} color={color or '-'}")


def cmd_grant_uid(args):
    """把某個帳號的 UID 換成指定號碼（1–99999999，例如 777）。不會影響之後的依序給號。"""
    conn = db.connect()
    email = args.email.strip().lower()
    new_uid = args.uid
    if not 1 <= new_uid <= config.UID_MAX:
        sys.exit(f"UID 要在 1–{config.UID_MAX} 之間（0 是管理員）")
    with db.transaction(conn):
        row = conn.execute("SELECT uid FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            sys.exit("找不到這個信箱")
        if conn.execute("SELECT 1 FROM users WHERE uid = ?", (new_uid,)).fetchone():
            sys.exit(f"UID {new_uid} 已經有人使用")
        old_uid = row["uid"]
        if old_uid == config.ADMIN_UID:
            sys.exit("不能改管理員的 UID")
        # sessions、checkins 是 ON UPDATE CASCADE，會一起改
        conn.execute("UPDATE users SET uid = ? WHERE uid = ?", (new_uid, old_uid))
    old_av = config.DATA_DIR / "avatars" / f"{old_uid}.webp"
    if old_av.exists():
        os.replace(old_av, config.DATA_DIR / "avatars" / f"{new_uid}.webp")
    card.invalidate(old_uid)
    print(f"{email}: UID {fmt_uid(old_uid)} -> {fmt_uid(new_uid)}")


def cmd_test_mail(args):
    """實際寄一封測試信，失敗時印出 SMTP 的錯誤訊息。"""
    from . import mailer

    print(f"SMTP {config.SMTP_HOST}:{config.SMTP_PORT} user={config.SMTP_USER or '(empty)'} "
          f"password={'(set)' if config.SMTP_PASSWORD else '(empty)'} from={config.MAIL_FROM}")
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        sys.exit("SMTP_USER / SMTP_PASSWORD 沒有設定，不會寄信。改好 .env 後執行 docker compose up -d")
    try:
        mailer.send_code(args.email, "123456", args.lang)
    except Exception as e:
        sys.exit(f"寄送失敗：{type(e).__name__}: {e}")
    print(f"SMTP 伺服器已收下，寄到 {args.email}（驗證碼 123456 只是測試用）")


def cmd_unlock(args):
    conn = db.connect()
    for key in ("admin_fails", "admin_lock_level", "admin_lock_until"):
        db.set_meta(conn, key, 0)
    print("管理員登入已解除鎖定")


def cmd_totp_uri(args):
    if not config.ADMIN_LOGIN or not config.ADMIN_TOTP:
        sys.exit("ADMIN_LOGIN / ADMIN_TOTP 沒有設定")
    print(totp.uri(config.ADMIN_TOTP, config.ADMIN_LOGIN))


def cmd_reset_progress(args):
    """清空所有簽到紀錄與小卡（Beta 結束、正式上線時用）。帳號、UID、名稱、大頭貼、自介都保留。"""
    conn = db.connect()
    checkins = conn.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]
    users = conn.execute("SELECT COUNT(DISTINCT uid) FROM checkins").fetchone()[0]
    print(f"會刪除 {checkins} 筆簽到紀錄（{users} 個帳號）、所有吱吱次數與小卡；帳號、UID、名稱、大頭貼、自介保留")
    if not args.yes:
        print("沒有做任何變更。確定要執行請加上 --yes")
        return
    backup = config.DATA_DIR / f"zhiwu.db.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    conn.execute("DELETE FROM checkins")
    conn.execute("UPDATE users SET clicks = 0")
    shutil.rmtree(config.DATA_DIR / "cards", ignore_errors=True)
    (config.DATA_DIR / "cards").mkdir(exist_ok=True)
    print(f"完成。備份在 {backup}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="查看帳號")
    s.add_argument("email")
    s.set_defaults(func=cmd_info)
    s = sub.add_parser("grant-uid", help="指定 UID")
    s.add_argument("email")
    s.add_argument("uid", type=int)
    s.set_defaults(func=cmd_grant_uid)
    s = sub.add_parser("set-title", help="指定 UID 的顏色與稱號")
    s.add_argument("uid", type=int)
    s.add_argument("--title", default="")
    s.add_argument("--color", default="", help="#rrggbb")
    s.add_argument("--clear", action="store_true", help="清除稱號與顏色")
    s.set_defaults(func=cmd_set_title)
    s = sub.add_parser("test-mail", help="寄一封測試信")
    s.add_argument("email")
    s.add_argument("--lang", default=config.DEFAULT_LANG)
    s.set_defaults(func=cmd_test_mail)
    s = sub.add_parser("unlock", help="解除管理員登入鎖定")
    s.set_defaults(func=cmd_unlock)
    s = sub.add_parser("totp-uri", help="印出 Aegis 用的 otpauth:// 連結")
    s.set_defaults(func=cmd_totp_uri)
    s = sub.add_parser("reset-progress", help="清空所有簽到進度（保留帳號）")
    s.add_argument("--yes", action="store_true", help="確定執行")
    s.set_defaults(func=cmd_reset_progress)
    args = p.parse_args(argv)
    db.init()
    args.func(args)


if __name__ == "__main__":
    main()
