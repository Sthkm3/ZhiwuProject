FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Pillow 畫小卡需要中日韓字型（NotoSansCJK-Regular/Bold.ttc）
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app ./app
COPY web ./web

ENV DATA_DIR=/data MEME_DIR=/memes WEB_DIR=/app/web
EXPOSE 2040

# 不用 root 執行。掛進來的 /data、/memes 要給這個 UID（或用 docker 的 user 設定換成別的 UID）
RUN groupadd -g 3004 zhiwu && useradd -u 3007 -g 3004 -M -s /usr/sbin/nologin zhiwu \
    && mkdir -p /data /memes && chown 3007:3004 /data /memes
USER 3007:3004

# SQLite 與記憶體裡的限流都假設只有一個 worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "2040", "--workers", "1"]
