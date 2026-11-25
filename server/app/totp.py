"""RFC 6238 TOTP（SHA1、6 碼、30 秒），和 Aegis / Google Authenticator 預設相同。只用標準函式庫。"""
import base64
import hashlib
import hmac
import struct
from urllib.parse import quote

PERIOD = 30
DIGITS = 6


def decode_secret(secret: str) -> bytes:
    s = secret.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(key: bytes, step: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def verify(secret: str, code: str, unix_time: int, window: int = 1) -> int | None:
    """比對前後各 window 個時間窗（容忍手機時鐘誤差），成功回傳符合的時間步。"""
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    key = decode_secret(secret)
    current = unix_time // PERIOD
    matched = None
    for step in range(current - window, current + window + 1):
        # 全部比完，不提早結束，避免從回應時間猜出哪個時間窗符合
        if hmac.compare_digest(code_at(key, step), code) and matched is None:
            matched = step
    return matched


def uri(secret: str, account: str, issuer: str = "吱鼯計畫") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret.replace(' ', '').upper()}&issuer={quote(issuer)}"
