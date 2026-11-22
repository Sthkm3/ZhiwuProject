"""伺服器端文字（信件、小卡、頭銜、分享文字）。前端介面文字在 web/i18n。"""
import json
from functools import lru_cache
from pathlib import Path

from .. import config

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    with open(_DIR / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


def norm_lang(lang) -> str:
    return lang if lang in config.LANGS else config.DEFAULT_LANG


def t(lang: str, key: str, **kw) -> str:
    text = _load(norm_lang(lang)).get(key)
    if text is None:
        text = _load("en").get(key, key)
    return text.format(**kw) if kw else text
