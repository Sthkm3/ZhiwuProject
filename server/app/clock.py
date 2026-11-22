"""時間集中在這裡，測試只要 monkeypatch `clock.now` 就能模擬換日。"""
from datetime import datetime, timedelta, timezone

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> int:
    return int(now().timestamp())


def today() -> str:
    """簽到日：台灣時間 04:00 換日，格式 YYYY-MM-DD。"""
    local = now().astimezone(config.TZ) - timedelta(hours=config.DAY_START_HOUR)
    return local.date().isoformat()


def next_reset() -> datetime:
    """下一個台灣時間 04:00（帶時區，前端換算成裝置時間）。"""
    local = now().astimezone(config.TZ)
    reset = local.replace(hour=config.DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local >= reset:
        reset += timedelta(days=1)
    return reset
