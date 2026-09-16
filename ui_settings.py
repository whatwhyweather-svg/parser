"""Сохранение настроек GUI между запусками."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from hashtags import normalize_hashtags

BASE_DIR = Path(__file__).resolve().parent
UI_SETTINGS_PATH = BASE_DIR / "ui_settings.json"
ENV_PATH = BASE_DIR / ".env"

DEFAULT_PARSE_POSTS = 5
MAX_PARSE_POSTS = 100


def normalize_tg_ids(raw) -> list[int]:
    """Список Telegram user id из списка/строки."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,;\s]+", raw.strip())
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [raw]
    out: list[int] = []
    seen: set[int] = set()
    for part in parts:
        try:
            uid = int(str(part).strip())
        except (TypeError, ValueError):
            continue
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


@dataclass
class UISettings:
    hashtags: list[str] = field(default_factory=list)
    interval_minutes: int = 2
    parse_posts: int = DEFAULT_PARSE_POSTS
    headless: bool = True
    allowed_tg_ids: list[int] = field(default_factory=list)
    geometry: str = "1040x680"


def _clamp_parse(value: int) -> int:
    return max(1, min(MAX_PARSE_POSTS, int(value)))


def load_ui_settings() -> UISettings:
    if not UI_SETTINGS_PATH.exists():
        return UISettings()
    try:
        raw = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UISettings()

    hashtags = normalize_hashtags(raw.get("hashtags") or [])
    try:
        interval = max(1, int(raw.get("interval_minutes") or 2))
    except (TypeError, ValueError):
        interval = 2
    try:
        raw_parse = raw.get("parse_posts", raw.get("scroll_count", DEFAULT_PARSE_POSTS))
        parse_posts = _clamp_parse(raw_parse)
    except (TypeError, ValueError):
        parse_posts = DEFAULT_PARSE_POSTS
    headless = bool(raw.get("headless", True))
    allowed_tg_ids = normalize_tg_ids(raw.get("allowed_tg_ids") or [])
    geometry = str(raw.get("geometry") or "1040x680")
    return UISettings(
        hashtags=hashtags,
        interval_minutes=interval,
        parse_posts=parse_posts,
        headless=headless,
        allowed_tg_ids=allowed_tg_ids,
        geometry=geometry,
    )


def save_ui_settings(settings: UISettings) -> None:
    settings.hashtags = normalize_hashtags(settings.hashtags)
    settings.interval_minutes = max(1, int(settings.interval_minutes))
    settings.parse_posts = _clamp_parse(settings.parse_posts)
    settings.headless = bool(settings.headless)
    settings.allowed_tg_ids = normalize_tg_ids(settings.allowed_tg_ids)
    UI_SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _sync_env(
        settings.hashtags,
        settings.interval_minutes,
        settings.parse_posts,
        settings.headless,
        settings.allowed_tg_ids,
    )


def _sync_env(
    hashtags: list[str],
    interval_minutes: int,
    parse_posts: int,
    headless: bool,
    allowed_tg_ids: list[int],
) -> None:
    if not ENV_PATH.exists():
        return
    text = ENV_PATH.read_text(encoding="utf-8")
    tags_line = ",".join(hashtags)
    if re.search(r"(?m)^SEARCH_HASHTAGS=", text):
        text = re.sub(
            r"(?m)^SEARCH_HASHTAGS=.*$",
            f"SEARCH_HASHTAGS={tags_line}",
            text,
        )
    elif re.search(r"(?m)^SEARCH_KEYWORDS=", text):
        text = re.sub(
            r"(?m)^SEARCH_KEYWORDS=.*$",
            f"SEARCH_HASHTAGS={tags_line}",
            text,
        )
    else:
        text += f"\nSEARCH_HASHTAGS={tags_line}\n"

    replacements = {
        "CHECK_INTERVAL_MINUTES": str(interval_minutes),
        "PARSE_POSTS": str(parse_posts),
        "PLAYWRIGHT_HEADLESS": "true" if headless else "false",
        "ALLOWED_TG_IDS": ",".join(str(i) for i in allowed_tg_ids),
    }
    for key, value in replacements.items():
        if re.search(rf"(?m)^{key}=", text):
            text = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", text)
        else:
            text += f"\n{key}={value}\n"

    text = re.sub(r"(?m)^SCROLL_COUNT=.*\n?", "", text)
    ENV_PATH.write_text(text, encoding="utf-8")
