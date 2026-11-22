import sqlite3
import time
from contextlib import contextmanager

from . import config

# init() 檢查管理員設定後填入；False 時管理員登入完全停用
admin_ok = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    uid          INTEGER PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    bio_md       TEXT NOT NULL DEFAULT '',
    lang         TEXT NOT NULL,
    avatar_ver   INTEGER NOT NULL DEFAULT 0,
    clicks       INTEGER NOT NULL DEFAULT 0,   -- 吱吱（點擊）總次數，和簽到天數分開算
    title        TEXT NOT NULL DEFAULT '',     -- 管理員指定的稱號（空字串 = 沒有）
    title_color  TEXT NOT NULL DEFAULT '',     -- 管理員指定的 UID 底色 #rrggbb（空字串 = 沒有）
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_codes (
    email        TEXT PRIMARY KEY COLLATE NOCASE,
    code_hash    TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    expires_at   INTEGER NOT NULL,
    last_sent_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    uid        INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE ON UPDATE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_uid ON sessions(uid);

CREATE TABLE IF NOT EXISTS checkins (
    uid        INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE ON UPDATE CASCADE,
    day        TEXT NOT NULL,
    meme       TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (uid, day)
);

-- 使用者投稿的迷因圖。待審的檔案在 DATA_DIR/submissions/<id>.<ext>，同意後搬進迷因資料夾
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE ON UPDATE CASCADE,
    day         TEXT NOT NULL,                     -- 上傳當天（clock.today()），算每日額度用
    ext         TEXT NOT NULL,                     -- jpg / png
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
    meme        TEXT,                              -- 同意後的檔名，例如 zhiwu_14.jpg
    created_at  INTEGER NOT NULL,
    reviewed_at INTEGER
);
CREATE INDEX IF NOT EXISTS submissions_uid_day ON submissions(uid, day);
CREATE INDEX IF NOT EXISTS submissions_status ON submissions(status, id);
"""


def db_path():
    return config.DATA_DIR / "zhiwu.db"


def connect() -> sqlite3.Connection:
    # isolation_level=None：自動 commit，需要交易時自己下 BEGIN
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "avatars").mkdir(exist_ok=True)
    (config.DATA_DIR / "cards").mkdir(exist_ok=True)
    (config.DATA_DIR / "submissions").mkdir(exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('next_uid', 1)")
        _migrate(conn)
        _init_admin(conn)
    finally:
        conn.close()


# 後來才加的 users 欄位：舊的資料庫（CREATE TABLE IF NOT EXISTS 不會補欄位）啟動時自動補上
_USER_COLUMNS = {
    "clicks": "INTEGER NOT NULL DEFAULT 0",
    "title": "TEXT NOT NULL DEFAULT ''",
    "title_color": "TEXT NOT NULL DEFAULT ''",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for name, decl in _USER_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")


def _init_admin(conn: sqlite3.Connection) -> None:
    """建立或更新管理員帳號（uid 0）。改了 ADMIN_LOGIN 重啟就會生效。"""
    global admin_ok
    from . import totp

    admin_ok = False
    login, secret = config.ADMIN_LOGIN, config.ADMIN_TOTP
    if not login or not secret:
        return
    try:
        if len(totp.decode_secret(secret)) < 10:
            raise ValueError("too short")
    except Exception:
        print("[ERROR] ADMIN_TOTP 不是合法的 base32 密鑰（至少 16 個字元），管理員登入停用", flush=True)
        return
    clash = conn.execute("SELECT uid FROM users WHERE email = ? AND uid != ?",
                         (login, config.ADMIN_UID)).fetchone()
    if clash:
        print(f"[ERROR] ADMIN_LOGIN 和 UID {clash[0]} 的信箱相同，管理員登入停用", flush=True)
        return
    conn.execute(
        "INSERT INTO users(uid, email, display_name, lang, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET email = excluded.email",
        (config.ADMIN_UID, login, "管理員", config.DEFAULT_LANG, int(time.time())),
    )
    admin_ok = True


def get_meta(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_db():
    """FastAPI dependency：每個請求一條連線。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：一開始就拿寫入鎖，避免兩個請求同時給號。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
