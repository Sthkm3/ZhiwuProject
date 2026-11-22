"""從 icon/ 裡的圖片產生網頁用的圖：

    server/.venv/Scripts/python tools/make_icons.py            # 用 icon/ 裡的第一張圖當圖標
    server/.venv/Scripts/python tools/make_icons.py icon/x.png

吱吱影片另外用 tools/make_media.py 轉檔。
"""
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

src = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
    p for p in sorted((ROOT / "icon").iterdir()) if p.is_file() and p.suffix.lower() in IMG_EXTS)
with Image.open(src) as im:
    im = ImageOps.exif_transpose(im).convert("RGB")
    im = ImageOps.fit(im, (512, 512), Image.LANCZOS)  # 不是正方形時置中裁切

im.save(WEB / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
for name, size in {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180, "logo.png": 64}.items():
    im.resize((size, size), Image.LANCZOS).save(WEB / name, optimize=True)
print(f"{src.name} -> web/favicon.ico, icon-192.png, icon-512.png, apple-touch-icon.png, logo.png")

# 圖庫（image/）是空的時候，小卡上方用這張
assets = ROOT / "server" / "app" / "assets"
assets.mkdir(exist_ok=True)
im.save(assets / "default-meme.jpg", "JPEG", quality=90)
print(f"{src.name} -> server/app/assets/default-meme.jpg")
