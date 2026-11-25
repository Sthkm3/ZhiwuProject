"""環境變數設定。其他模組一律用 `config.X` 取值（測試會直接改這裡的屬性）。"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

# 每日簽到的分界：台灣時間 04:00（程式一律明確用這個時區，不依賴容器的 TZ）
TZ = ZoneInfo("Asia/Taipei")
DAY_START_HOUR = 4

LANGS = ("zh-TW", "zh-CN", "ja", "ko", "en")
DEFAULT_LANG = "zh-TW"

_HERE = Path(__file__).resolve().parent


def _default_web_dir() -> Path:
    # Docker 裡是 /app/web；本機開發是 <repo>/web
    for p in (_HERE.parent / "web", _HERE.parent.parent / "web"):
        if p.is_dir():
            return p
    return _HERE.parent / "web"


def _default_meme_dir() -> Path:
    return _HERE.parent.parent / "image"


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MEME_DIR = Path(os.environ.get("MEME_DIR") or _default_meme_dir())
WEB_DIR = Path(os.environ.get("WEB_DIR") or _default_web_dir())

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:2040").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

# 寄信用的 SMTP。預設 Gmail（帳號 + 應用程式密碼）；換成其他寄信服務只要改這幾個值
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 465)   # 465 = SSL；587 = STARTTLS
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM") or "noreply@zhiwu-project.sthkm3.org"

# 小卡字型；沒設定時依序找 Noto Sans CJK（Docker）→ Windows 內建字型 → Pillow 預設字型
FONT_REGULAR = os.environ.get("FONT_REGULAR", "")
FONT_BOLD = os.environ.get("FONT_BOLD", "")

CODE_TTL = 5 * 60            # 驗證碼有效 5 分鐘
CODE_RESEND = 60             # 同一信箱 60 秒內不能重寄
CODE_MAX_ATTEMPTS = 5        # 錯 5 次作廢
IP_LIMIT = 10                # 同一 IP 10 分鐘最多寄 10 次
IP_WINDOW = 10 * 60
SESSION_TTL = 90 * 24 * 3600
ADMIN_SESSION_TTL = 7 * 24 * 3600

# 全站寄信上限：擋轟炸別人信箱，也避免把寄信服務的額度用光
MAIL_LIMIT = 40
MAIL_WINDOW = 10 * 60
MAIL_DAY_LIMIT = 400
MAIL_DAY_WINDOW = 24 * 3600

# 請求大小上限
BODY_MAX = 64 * 1024
UPLOAD_BODY_MAX = 6 * 1024 * 1024     # 圖片上限 5MB，加上表單的部分
UPLOAD_PATHS = ("/api/me/avatar", "/api/submissions")

# 上傳的圖：像素上限（2400 萬像素的手機照片可以過）、等前一張處理完最多幾秒
IMAGE_MAX_PIXELS = 30_000_000
IMAGE_WAIT = 10
# 每個帳號每分鐘最多處理幾次（失敗也算）
AVATAR_LIMIT = 3
SUBMIT_LIMIT = 10
UPLOAD_WINDOW = 60

# 管理員：ADMIN_LOGIN 要是信箱格式（不會寄信），驗證碼是 ADMIN_TOTP（base32 密鑰）算出的 TOTP。兩個都設定才啟用
ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "").strip()
ADMIN_TOTP = os.environ.get("ADMIN_TOTP", "").strip()
ADMIN_UID = 0
ADMIN_MAX_FAILS = 3                 # 錯 3 次就鎖
ADMIN_LOCK_BASE = 15 * 60           # 第一次鎖 15 分，之後每次加倍
ADMIN_LOCK_MAX = 24 * 3600

UID_MAX = 99_999_999                # UID 顯示成 8 碼

NAME_MAX = 32
TITLE_MAX = 16                      # 管理員指定的稱號長度上限
BIO_MAX = 2000
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_SIZE = 1024

# 使用者投稿迷因圖：每個帳號每天（04:00 換日）最多 5 張，被拒絕的也算
SUBMIT_PER_DAY = 5
SUBMIT_MAX_BYTES = 5 * 1024 * 1024    # 和大頭貼一樣，錯誤訊息寫的是 5MB
SUBMIT_MAX_SIDE = 2048             # 長邊超過就等比例縮小

# 繼續吱吱：一個帳號平均每 1.5 秒只能吱一次
ZHI_BATCH_MAX = 200                # 一次請求的 n 上限（超過直接拒絕）；不管 n 是多少，一次請求最多加 1 下
ZHI_GAP = 1.5
ZHI_JITTER = 0.3                   # 網路抖動時最多可以提早這麼多秒到，提早的會從下一下扣回來
# 太頻繁：COOL_WINDOW 秒內被上面的限制擋下 COOL_REJECTS 次，就冷靜 COOL_SECONDS 秒
COOL_REJECTS = 5
COOL_WINDOW = 10
COOL_SECONDS = 10

# 反作弊：多久檢查一次吱吱次數有沒有超過理論上限（見 anticheat.py）
CHECK_INTERVAL = 3600

# 管理員通知匣：同樣的通知 NOTICE_MERGE 秒內併成一則，最多留 NOTICE_KEEP 則
NOTICE_MERGE = 3600
NOTICE_KEEP = 500

# 訪客小卡：伺服器不存任何訪客資料，每次請求即時畫。畫一張要一點 CPU，所以依 IP 限流
GUEST_CARD_LIMIT = 30              # 同一 IP 每分鐘最多畫幾張（從快取拿的不算）
GUEST_CARD_WINDOW = 60
GUEST_CARD_CACHE = 32              # 記憶體裡留最近幾張（一張約 0.6 MB）
GUEST_CLICKS_MAX = 9_999_999

COOKIE_NAME = "zhiwu_session"
