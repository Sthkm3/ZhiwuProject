"""讀使用者上傳的圖：限制格式、限制像素、同時只處理一張。"""
import io
import threading
import warnings
from contextlib import contextmanager

from PIL import Image, ImageOps

from . import config
from .deps import fail

# 只開這幾種。Pillow 還能讀 PSD、FITS、TIFF 等幾十種，漏洞多半出在那些格式
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")

# 第二道保險：Pillow 自己在超過這個值的兩倍時會拒絕（例如檔頭和實際內容不一樣大）
Image.MAX_IMAGE_PIXELS = config.IMAGE_MAX_PIXELS // 2
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

_slot = threading.Lock()


@contextmanager
def slot():
    """解碼很吃記憶體，全站同時只處理一張；等太久就請使用者稍後再試。"""
    if not _slot.acquire(timeout=config.IMAGE_WAIT):
        fail(429, "too_frequent")
    try:
        yield
    finally:
        _slot.release()


def decode(data: bytes) -> tuple[Image.Image, bool]:
    """回傳 (RGB 或 RGBA 的圖, 有沒有透明度)。GIF 只取第一格；方向依 EXIF 轉正。"""
    try:
        with Image.open(io.BytesIO(data), formats=FORMATS) as im:
            if im.width * im.height > config.IMAGE_MAX_PIXELS:  # 只讀了檔頭，還沒解碼
                raise ValueError("too many pixels")
            im.seek(0)
            im.load()
            ImageOps.exif_transpose(im, in_place=True)
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            return im.convert("RGBA" if alpha else "RGB"), alpha
    except Exception:
        fail(400, "invalid_image")
