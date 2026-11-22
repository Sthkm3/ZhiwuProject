"""把 media-src/zhizhi.mp4（吱吱影片原檔）轉成網頁用的檔案：

    server/.venv/Scripts/python -m pip install imageio-ffmpeg   # 自帶 ffmpeg，不用另外安裝
    server/.venv/Scripts/python tools/make_media.py

輸出：
    web/media/zhizhi.mp4           H.264 + AAC、moov 放在檔頭（faststart），手機瀏覽器可以邊載邊播
    web/media/zhizhi-poster.webp   第一幀；還沒點擊時顯示的畫面
"""
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "media-src" / "zhizhi.mp4"
MEDIA = ROOT / "web" / "media"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


MEDIA.mkdir(parents=True, exist_ok=True)

video = MEDIA / "zhizhi.mp4"
ffmpeg("-i", str(SRC),
       "-c:v", "libx264", "-profile:v", "main", "-pix_fmt", "yuv420p", "-crf", "23", "-preset", "slow",
       "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
       "-c:a", "aac", "-b:a", "128k", "-ac", "2",
       "-movflags", "+faststart", str(video))

frame = MEDIA / "_first.png"
ffmpeg("-i", str(SRC), "-frames:v", "1", str(frame))
with Image.open(frame) as im:
    im = im.convert("RGB")
    im.save(MEDIA / "zhizhi-poster.webp", "WEBP", quality=88, method=6)
    size = im.size
frame.unlink()

print(f"{SRC.name} -> web/media/zhizhi.mp4 ({video.stat().st_size // 1024} KB), "
      f"zhizhi-poster.webp {size[0]}x{size[1]}")
