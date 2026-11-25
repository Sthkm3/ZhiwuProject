# 改一個自己的

clone 之後要改哪些地方、怎麼在自己的電腦跑起來、怎麼部署。

站上的影片、圖片與角色版權屬於原作者，改成自己的站請換成你有權使用的素材。

## 要改的地方

| 想改什麼 | 在哪裡 |
|---|---|
| 站名、標題 | `web/index.html`、`web/manifest.webmanifest`、`server/app/share.py`、`server/app/mailer.py`（寄件人名稱）、`server/app/main.py`、`server/app/totp.py` |
| 介面文字（五種語言） | `web/i18n/*.json`；每個語言的 key 要一樣 |
| 信件、小卡、分享文字 | `server/app/i18n/*.json` |
| 吱吱影片 | 換掉 `media-src/zhizhi.mp4`，執行 `tools/make_media.py` |
| 圖標 | 換掉 `icon/zhiwu.jpg`，執行 `tools/make_icons.py`（也會產生圖庫是空的時候用的預設圖） |
| 圖庫 | 圖片放進 `image/`（jpg / png / webp / gif），不用重啟 |
| 網頁顏色 | `web/style.css` 最上面的 `:root` |
| 小卡版面與顏色 | `server/app/card.py`；改完把 `CARD_VERSION` 加 1，舊的小卡才會重畫 |
| UID 底色 | `server/app/titles.py` 和 `web/style.css` 的 `.uid`，兩邊要一樣 |
| 頁尾連結 | `web/index.html` 的 `.site-footer`、`server/app/share.py` |
| 換日時間、各種上限 | `server/app/config.py` |
| 版本號 | `server/app/__init__.py` |
| 容器名稱、port | `docker-compose.yml` |

兩個 `tools/` 的程式要先裝 Pillow 和 imageio-ffmpeg：

```
pip install pillow imageio-ffmpeg
python tools/make_media.py
python tools/make_icons.py
```

## 在自己的電腦跑

需要 Docker。

```
cp .env.example .env
docker network create tunnel
docker compose up -d --build
```

`.env` 先改成本機用的：

```
PUBLIC_URL=http://localhost:2041
COOKIE_SECURE=0
```

打開 `http://localhost:2041`。`SMTP_PASSWORD` 留空時不會寄信，驗證碼印在 `docker compose logs zhiwuproject`。

跑測試：

```
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt    # macOS / Linux 是 .venv/bin/python
.venv/Scripts/python -m pytest -q
```

## .env

| 名稱 | 用途 |
|---|---|
| `PUBLIC_URL` | 對外網址。分享連結、預覽圖、信件和小卡角落的網域都用它 |
| `COOKIE_SECURE` | 用 https 對外時設 `1` |
| `PORT` | 本機的 port，只綁 127.0.0.1（預設 2041） |
| `TUNNEL_NETWORK` | cloudflared 所在的 Docker 網路 |
| `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD` | 寄驗證碼的信箱服務。465 用 SSL，其他用 STARTTLS |
| `MAIL_FROM` | 寄件人，網域要在寄信服務驗證過 |
| `ADMIN_LOGIN`、`ADMIN_TOTP` | 管理員登入用的名稱與 base32 密鑰，兩個都留空就停用。名稱要是信箱格式（不會寄信），前綴用猜不到的字，不要用任何使用者的信箱。登入時在信箱欄輸入它，驗證碼用 Aegis 等 TOTP App 看 |

產生一組 `ADMIN_TOTP`：

```
python -c "import base64, secrets; print(base64.b32encode(secrets.token_bytes(20)).decode())"
```

## 部署

1. 設好 `PUBLIC_URL`、`COOKIE_SECURE=1`、SMTP 和管理員
2. 把 cloudflared 放在同一個 Docker 網路，Public Hostname 指到 `http://zhiwuproject:2040`
3. `docker compose up -d --build`

不要直接把容器的 port 開到外網：限流會信任 `CF-Connecting-IP`，繞過 Cloudflare 就能偽造。

資料在 `/data`（資料庫、大頭貼、投稿、小卡快取），圖庫在 `/memes`，這兩個要掛出來並備份。

容器用 UID/GID **3007:3004** 執行（不是 root），掛進來的資料夾要給它：

```
chown -R 3007:3004 <資料夾>
chmod -R u=rwX,go= <資料夾>
```

也可以用 Docker 的 `user` 設定換成別的 UID，資料夾的擁有者一起換就好。從以 root 執行的舊版升級時也要做一次；資料夾不能讀寫時，啟動的 log 會寫要怎麼改。

程式只能跑一個 worker，限流和冷靜時間都記在記憶體。
