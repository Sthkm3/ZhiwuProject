# 吱鼯計畫

> 你今天吱吱了嗎

每天吱吱簽到一次，產生一張吱吱小卡（簽到天數 + 吱吱次數 + 隨機圖片），可以圖文分享。
用信箱驗證碼註冊、登入（沒有密碼）。介面支援繁中、簡中、日、韓、英。網址是 `zhiwu-project.sthkm3.org`。

寫法沿用 My_INMU（FastAPI + SQLite + 純 HTML/JS），差別在簽到畫面和吱吱次數。

| 服務 | 內容 | port |
|---|---|---|
| `zhiwuproject` | FastAPI + SQLite：API、網站、小卡圖片（Pillow + Noto Sans CJK） | 容器內 `2040`；本機除錯 `127.0.0.1:2041` |

## 啟動

```
cp .env.example .env      # SMTP_USER / SMTP_PASSWORD 留空時驗證碼印在 log
docker network create tunnel   # 只有第一次；已經有 cloudflared 的網路就把名稱填進 .env 的 TUNNEL_NETWORK
docker compose up -d --build
```

打開 `http://127.0.0.1:2041`，輸入信箱後用 `docker compose logs zhiwuproject` 找驗證碼（`[DEV] verify code for ...`）。

| 指令 | 用途 |
|---|---|
| `docker compose logs -f zhiwuproject` | log（SMTP 帳密沒填時，驗證碼印在這裡） |
| `docker compose down` | 停止（資料保留） |
| `docker compose down -v` | 停止並刪除 volume `zhiwuproject_zhiwu-data`（清空帳號與簽到紀錄） |
| `docker compose exec zhiwuproject python -m app.admin info <email>` | 查看帳號 |
| `docker compose exec zhiwuproject python -m app.admin grant-uid <email> 777` | 把帳號的 UID 換成指定號碼（1–99999999） |
| `docker compose exec zhiwuproject python -m app.admin unlock` | 解除管理員登入鎖定 |
| `docker compose exec zhiwuproject python -m app.admin totp-uri` | 印出 Aegis 用的 `otpauth://` 連結（換手機時用） |
| `docker compose exec zhiwuproject python -m app.admin reset-progress --yes` | 清空所有簽到進度與吱吱次數（保留帳號；不加 `--yes` 只顯示會刪什麼） |

## 對外部署（zhiwu-project.sthkm3.org）

cloudflared 用容器名稱連進來，所以兩個容器要在同一個 Docker 網路：

1. `.env`：`PUBLIC_URL=https://zhiwu-project.sthkm3.org`、`COOKIE_SECURE=1`、`TUNNEL_NETWORK=<cloudflared 所在的網路>`（`docker network ls` 查）
2. Cloudflare 後台的 Public Hostname：`zhiwu-project.sthkm3.org → http://zhiwuproject:2040`
3. `docker compose up -d --build`

- 容器名稱與 hostname 固定是 `zhiwuproject`，容器內固定聽 `2040`
- `.env` 的 `PORT`（預設 2041）只是把容器的 2040 綁到本機 `127.0.0.1` 方便除錯，tunnel 不經過它；預設不用 2040 是為了不和同一台機器上的 My_INMU 相撞
- 寄信是容器主動連到 SMTP 伺服器，不經過 tunnel。`MAIL_FROM` 的網域要和寄信服務驗證過的網域相同

備份：volume `zhiwuproject_zhiwu-data` 裡的 `zhiwu.db`、`avatars/`、`submissions/`（`cards/` 是快取，可以刪），以及 `image/`。

## 吱吱（簽到畫面）

今天還沒簽到時，登入後整個畫面會被吱吱影片蓋住：

1. 還沒點擊：停在影片第一幀，中間寫「點擊吱吱」
2. 點一下：影片有聲播到完（約 1.6 秒），同時送出簽到
3. 影片播完、小卡也下載好之後，亮出小卡

小卡下方的「**繼續吱吱**」會回到同一個全螢幕畫面：點一下播一次、吱吱次數 +1，播完才能再點（播放中點了沒有反應），不會跳去小卡；按左上角「回到小卡」離開，小卡會換成新的次數。

繼續吱吱畫面中下方有「**全自動吱吱**」開關：打開後每播完一次就自動接著吱下一次（一樣一次 +1、受一秒一次的限制），再按一次或回到小卡就停止。

左上角的「等等再吱」可以先關掉全螢幕（用側欄、換語言、登出），頁面上會留一個「去吱吱」的入口。

### 兩個獨立的數字

| | 怎麼加 | 存在哪 |
|---|---|---|
| 簽到天數（累計 / 連續） | 每天（台灣時間 04:00 換日）第一次吱吱 +1 | `checkins` 一天一筆 |
| 吱吱次數 | 每吱一下 +1：簽到那一下算一次，繼續吱吱每點一下算一次 | `users.clicks` |

- 繼續吱吱每點一下就送一次 `POST /api/zhi {n: 1}`；沒送成功的會留著補送
- **一個帳號一秒只能吱一次**：伺服器端也擋，同一秒內多送的、或一次送很多下的，都只算一下（`config.py` 的 `ZHI_RATE` / `ZHI_BURST`）
- 兩個數字都顯示在小卡、個人頁和簽到頁

### 影片與圖標

原檔不打包進 Docker，換檔後重新產生再 build：

```
server/.venv/Scripts/python -m pip install imageio-ffmpeg     # 自帶 ffmpeg
server/.venv/Scripts/python tools/make_media.py    # media-src/zhizhi.mp4 → web/media/zhizhi.mp4（faststart）、zhizhi-poster.webp（第一幀）
server/.venv/Scripts/python tools/make_icons.py    # icon/zhiwu.jpg → web/ 的 favicon.ico、icon-192/512.png、apple-touch-icon.png、logo.png
docker compose up -d --build
```

`make_icons.py` 也會產生 `server/app/assets/default-meme.jpg`：圖庫是空的時候小卡用這張。

## 圖庫

放在 `image/`（jpg / png / webp / gif 取第一幀），容器掛載成 `/memes`。
每次簽到時隨機抽一張，檔名存進資料庫，所以同一天的小卡重畫也一樣。新增圖片不用重啟；**已經被抽過的圖片不要改名或刪除**，否則舊的小卡會變成預設圖。

### 使用者投稿

- 登入後側欄的「投稿圖片」（`/upload`）：每個帳號每天最多 5 張（台灣時間 04:00 重置，被拒絕的也算），每張上限 5MB
- 上傳的圖會重新編碼成 JPEG（有透明度就用 PNG），去掉 EXIF / GPS；長邊超過 2048 會縮小
- 待審的檔案放在 volume 的 `/data/submissions/<id>.<ext>`
- 管理員側欄的「審核投稿 (N)」（`/review`）一次顯示一張，按同意或拒絕：
  - 同意：存成 `image/zhiwu_<目前最大編號 + 1>.jpg`（所以 `image/` 要可寫入），馬上加入抽卡
  - 拒絕：刪掉檔案，紀錄保留（用來算額度）

## UID

- UID 顯示成 8 碼（`00000001`），資料庫存整數。`00000000` 是管理員（黃底標示），一般使用者從 `00000001` 開始依序給號
- 預設名稱是 `吱鼯_<uid>`
- 給號用 `meta.next_uid`，不用 `AUTOINCREMENT`：否則 `grant-uid` 給出很大的號碼之後，之後的號碼會全部跳過去

### 顏色與稱號

管理員可以幫任何 UID 指定一個顏色和一個稱號（只是顯示用，不帶權限）。顏色會套在 UID 和稱號上，顯示在側欄、個人頁、審核頁和小卡。

- 網頁：用管理員登入後打開對方的個人頁 `/u/<uid>`，下方會多一個「稱號與顏色」表單；「清除」可以拿掉
- 指令：`docker compose exec zhiwuproject python -m app.admin set-title <uid> --title 稱號 --color '#ff8a1f'`（`--clear` 清除）
- 稱號最多 16 個字，可以留空只上顏色；只給稱號沒選顏色時用主色。文字顏色會依底色的亮度自動選深色或白色
- 管理員自己沒指定時維持預設的黃底

## 管理員

- `.env` 設定 `ADMIN_LOGIN`（信箱欄要輸入的字）和 `ADMIN_TOTP`（base32 密鑰），改完執行 `docker compose up -d`。兩個都留空就停用管理員登入
- 登入：在網頁的信箱欄輸入 `ADMIN_LOGIN`，按「寄送驗證碼」（不會真的寄信），再輸入 Aegis 上的 6 位數（TOTP、SHA1、6 位、30 秒）
- 錯 3 次就全域鎖定（15 分、30 分、1 小時…上限 24 小時）；每組碼只能用一次；session 有效 7 天
- 被自己鎖住時，執行 `python -m app.admin unlock`

`.env` 的 `BETA=1` 會在網頁標題加「(Beta)」、小卡右下角加「· Beta」；預設是 0。

## API

會改資料的 `/api` 請求要帶 header `X-ZHIWU: 1`（防 CSRF）；用 `Authorization: Bearer <token>` 的客戶端不需要。
錯誤一律回 `{"detail": "<code>"}`。

| 方法 路徑 | 說明 |
|---|---|
| `POST /api/auth/request-code {email, lang}` | 寄 6 碼，5 分鐘有效；同信箱 60 秒一次、同 IP 10 分鐘 10 次 |
| `POST /api/auth/verify {email, code, lang}` | 錯 5 次作廢。沒有帳號就自動註冊。回傳 `{token, user}` 並設定 HttpOnly cookie |
| `POST /api/auth/logout` | 登出 |
| `GET /api/me`、`PATCH /api/me {display_name?, bio_md?, lang?}` | 名稱 1–32 字、自介 2000 字；回傳含 `total_days`、`streak`、`clicks` |
| `PUT /api/me/avatar`（multipart `file`） | 上限 5MB，裁成 1024×1024 WebP |
| `POST /api/checkin`、`GET /api/checkin/today` | `{day, checked_in, already?, total_days, streak, clicks, next_reset, card_url?, share_url?, share_text?}` |
| `POST /api/zhi {n}` | 繼續吱吱：吱吱次數 +1（一個帳號一秒一次）。回傳 `{clicks, added}`，`added` 是實際加的（被限流時是 0） |
| `GET /api/users/{uid}?lang=` | 公開個人資料（含 `title`、`title_color`、`uid_tier`） |
| `PUT /api/admin/users/{uid}/title {title, color}` | 管理員：指定 UID 的稱號與顏色（`#rrggbb`）；兩個都空 = 清除 |
| `POST /api/markdown {md}` | 自介預覽 |
| `GET /card/{uid}/{day}.png` | 小卡。快取在 `/data/cards/<uid>/<day>.v<版本>.c<吱吱次數>.png`，次數變了就重畫；網頁會加 `?c=<次數>` |
| `GET /c/{uid}/{day}` | 分享頁（`og:image`） |
| `GET /avatars/{uid}.webp`、`GET /healthz` | |
| `POST /api/submissions`（multipart `file`）、`GET /api/submissions` | 投稿圖片（每天 5 張）、自己最近 30 筆 |
| `GET /api/submissions/{id}/image` | 只有上傳者本人和管理員看得到 |
| `GET /api/admin/submissions`、`POST /api/admin/submissions/{id}/approve`、`/reject` | 管理員審核 |

每日分界是**台灣時間 04:00**（程式固定用 `Asia/Taipei`）。

## 開發

```
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # Windows
.venv/Scripts/python -m pytest -q
DATA_DIR=../data MEME_DIR=../image .venv/Scripts/python -m uvicorn app.main:app --port 2040 --reload
```

直接跑 uvicorn 不會讀 `.env`（那是給 docker compose 用的），所以不會寄信，驗證碼印在終端機。
本機沒有 Noto Sans CJK 時，小卡會改用 Windows 內建字型（`FONT_REGULAR` / `FONT_BOLD` 可以指定）。

## 檔案

```
server/app/
  main.py      路由、CSRF header 檢查、靜態檔
  auth.py      驗證碼登入、session
  users.py     UID 給號、個人資料、大頭貼、自介 Markdown
  checkin.py   簽到、累計 / 連續天數、抽圖、吱吱次數（/api/zhi）
  submissions.py  使用者投稿圖片、管理員審核
  card.py      畫小卡
  share.py     分享頁
  admin.py     管理指令
  mailer.py    SMTP 寄信
  i18n/        信件、小卡、分享文字
  assets/      小卡的預設圖
web/           網站（純 HTML/JS）；i18n/ 是介面文字，media/ 是吱吱影片與第一幀
image/         圖庫（管理員同意的投稿也會寫進來）
icon/、media-src/   圖標與影片的原檔（不打包進 Docker）
tools/         make_icons.py、make_media.py
```
