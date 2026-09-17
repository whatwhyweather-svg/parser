"""Загрузка настроек из .env."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from hashtags import normalize_hashtags
from ui_settings import normalize_tg_ids

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str | None = None) -> str:
    value = os.getenv(key, default)
    if value is None or value.strip() == "":
        raise ValueError(f"Не задана обязательная переменная окружения: {key}")
    return value.strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_cookies(raw: str, user_agent: str) -> list[dict]:
    """Преобразует строку Cookie или JSON в формат Playwright."""
    raw = raw.strip()
    if not raw:
        return []

    if raw.startswith("["):
        cookies = json.loads(raw)
        for cookie in cookies:
            cookie.setdefault("domain", ".threads.net")
            cookie.setdefault("path", "/")
        return cookies

    cookies: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".threads.net",
                "path": "/",
            }
        )
    extra = []
    for cookie in cookies:
        clone = dict(cookie)
        clone["domain"] = ".instagram.com"
        extra.append(clone)
    _ = user_agent
    return cookies + extra


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    search_hashtags: list[str]
    check_interval_minutes: int
    parse_posts: int
    max_comments: int
    user_agent: str
    threads_cookies: list[dict]
    playwright_headless: bool
    database_path: Path
    threads_username: str | None = None
    telegram_proxy: str | None = None
    require_russian: bool = True
    require_seo_intent: bool = True
    # 0 = не фильтровать по возрасту; иначе только посты новее N часов
    max_post_age_hours: int = 96
    app_name: str = "Threads Posts ARTFrance"
    allowed_tg_ids: tuple[int, ...] = ()
    alert_tg_id: int = 1229785135
    # Доп. получатели алертов/статуса: numeric IDs и/или @username
    alert_tg_extra: tuple[str, ...] = ()
    lead_target_per_day: int = 10
    lead_target_per_hour: int = 4
    status_interval_minutes: int = 180
    # Локальный час (0–23), после которого шлётся отчёт за вчера
    daily_report_hour: int = 9
    deepseek_api_key: str = ""
    deepseek_enabled: bool = False
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    # Invite-хэши t.me/+XXXX и/или готовые chat_id групп-источников
    telegram_source_invites: tuple[str, ...] = ()
    telegram_source_chat_ids: tuple[int, ...] = ()
    # MTProto (Telethon): читать группу личным аккаунтом, бот в группу не нужен
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_user_phone: str = ""
    telegram_user_session: Path | None = None
    telegram_user_enabled: bool = False


# Тестовый пин: все ЛС / статусы / алерты только сюда. 0 = обычный режим.
TEST_ONLY_TG_ID = 0


def pinned_delivery_id() -> int:
    try:
        return int(TEST_ONLY_TG_ID or 0)
    except (TypeError, ValueError):
        return 0


def _read_test_bot_token() -> str:
    env_tok = (
        os.getenv("TEST_TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN_TEST")
        or ""
    ).strip()
    if env_tok:
        return env_tok
    secret = BASE_DIR / "test_bot.secret"
    if not secret.is_file():
        return ""
    try:
        return secret.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve_bot_token() -> tuple[str, str]:
    """(token, source). Боевой бот из .env, пока TEST_ONLY_TG_ID = 0."""
    if pinned_delivery_id():
        test = _read_test_bot_token()
        if not test:
            raise ValueError(
                "Тестовый режим: нет токена второго бота. "
                "Положи его в test_bot.secret или TEST_TELEGRAM_BOT_TOKEN."
            )
        return test, "test_bot"
    prod = _env("TELEGRAM_BOT_TOKEN")
    return prod, "prod"


# Ключи MTProto самого приложения. Пользователь вводит только номер.
# Свои можно задать в .env (TELEGRAM_API_ID / TELEGRAM_API_HASH).
_BUILTIN_TG_API_ID = 6
_BUILTIN_TG_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"


def resolve_telegram_api() -> tuple[int, str]:
    raw_id = (os.getenv("TELEGRAM_API_ID") or "").strip()
    raw_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    try:
        api_id = int(raw_id) if raw_id else 0
    except ValueError:
        api_id = 0
    if api_id and raw_hash:
        return api_id, raw_hash
    return _BUILTIN_TG_API_ID, _BUILTIN_TG_API_HASH


def load_settings() -> Settings:
    # Каждый раз перечитываем .env (иначе галочка «скрыть браузер» не применяется)
    load_dotenv(BASE_DIR / ".env", override=True)

    # SEARCH_HASHTAGS приоритетнее; SEARCH_KEYWORDS — для совместимости
    raw = os.getenv("SEARCH_HASHTAGS") or os.getenv("SEARCH_KEYWORDS") or ""
    hashtags = normalize_hashtags(raw)
    # Пустой список допустим — теги могут прийти из Telegram-бота

    user_agent = _env(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    cookies_raw = os.getenv("THREADS_COOKIES", "") or ""
    username_raw = (os.getenv("THREADS_USERNAME") or "").strip().lstrip("@") or None
    try:
        raw_parse = os.getenv("PARSE_POSTS") or os.getenv("SCROLL_COUNT") or "40"
        parse_posts = max(1, min(200, int(raw_parse)))
    except ValueError:
        parse_posts = 40
    try:
        max_comments = max(0, int(os.getenv("MAX_COMMENTS", "40")))
    except ValueError:
        max_comments = 40
    try:
        max_age_h = max(0, int(os.getenv("MAX_POST_AGE_HOURS", "168")))
    except ValueError:
        max_age_h = 168
    try:
        lead_target = max(1, int(os.getenv("LEAD_TARGET_PER_DAY", "96")))
    except ValueError:
        lead_target = 96
    try:
        lead_hour = max(1, int(os.getenv("LEAD_TARGET_PER_HOUR", "4")))
    except ValueError:
        lead_hour = 4
    try:
        status_mins = max(30, int(os.getenv("STATUS_INTERVAL_MINUTES", "180")))
    except ValueError:
        status_mins = 180
    try:
        daily_hour = int(os.getenv("DAILY_REPORT_HOUR", "9"))
        daily_hour = max(0, min(23, daily_hour))
    except ValueError:
        daily_hour = 9
    proxy = (os.getenv("TELEGRAM_PROXY") or "").strip() or None
    allowed_ids = tuple(normalize_tg_ids(os.getenv("ALLOWED_TG_IDS") or ""))
    try:
        alert_tg_id = int((os.getenv("ALERT_TG_ID") or "1229785135").strip())
    except ValueError:
        alert_tg_id = 1229785135
    alert_extra_raw = (
        os.getenv("ALERT_TG_EXTRA")
        or os.getenv("ALERT_TG_USERNAMES")
        or "link_to_my_profile"
    )
    alert_extra = tuple(
        p.strip().lstrip("@")
        for p in re.split(r"[,;\s]+", alert_extra_raw)
        if p.strip()
    )

    deepseek_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    deepseek_enabled = _env_bool("DEEPSEEK_ENABLED", bool(deepseek_key))
    if deepseek_enabled and not deepseek_key:
        deepseek_enabled = False

    from tg_source import load_communities_file, parse_chat_ids, parse_invite_list

    source_invites = list(
        parse_invite_list(
            os.getenv("TELEGRAM_SOURCE_INVITES")
            or os.getenv("TELEGRAM_SOURCE_GROUPS")
            or ""
        )
    )
    # Полный каталог из xlsx (если файл есть) — дополняет .env
    file_invites = load_communities_file(BASE_DIR / "telegram_communities.txt")
    if file_invites:
        seen = {x.casefold() for x in source_invites}
        for inv in file_invites:
            if inv.casefold() not in seen:
                source_invites.append(inv)
                seen.add(inv.casefold())
    source_invites = tuple(source_invites)
    source_chats = tuple(
        sorted(
            parse_chat_ids(
                os.getenv("TELEGRAM_SOURCE_CHAT_IDS")
                or os.getenv("TELEGRAM_SOURCE_CHATS")
                or ""
            )
        )
    )

    api_id, api_hash = resolve_telegram_api()
    user_phone = (os.getenv("TELEGRAM_USER_PHONE") or "").strip()
    session_name = (
        os.getenv("TELEGRAM_USER_SESSION") or "tg_user.session"
    ).strip() or "tg_user.session"
    user_enabled = _env_bool("TELEGRAM_USER_ENABLED", False)

    bot_token, _bot_src = resolve_bot_token()

    return Settings(
        telegram_bot_token=bot_token,
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", "-1004428286062"),
        search_hashtags=hashtags,
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
        parse_posts=parse_posts,
        max_comments=max_comments,
        user_agent=user_agent,
        threads_cookies=parse_cookies(cookies_raw, user_agent),
        playwright_headless=_env_bool("PLAYWRIGHT_HEADLESS", True),
        database_path=BASE_DIR / os.getenv("DATABASE_PATH", "sent_posts.db"),
        threads_username=username_raw,
        telegram_proxy=proxy,
        require_russian=_env_bool("REQUIRE_RUSSIAN", True),
        # Дешёвый префильтр интента до DeepSeek. REQUIRE_SEO_INTENT=0 — всё в модель.
        require_seo_intent=_env_bool("REQUIRE_SEO_INTENT", True),
        max_post_age_hours=max_age_h,
        app_name=(os.getenv("APP_NAME") or "Threads Posts ARTFrance").strip()
        or "Threads Posts ARTFrance",
        allowed_tg_ids=allowed_ids,
        alert_tg_id=alert_tg_id,
        alert_tg_extra=alert_extra,
        lead_target_per_day=lead_target,
        lead_target_per_hour=lead_hour,
        status_interval_minutes=status_mins,
        daily_report_hour=daily_hour,
        deepseek_api_key=deepseek_key,
        deepseek_enabled=deepseek_enabled,
        deepseek_model=(os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
        or "deepseek-chat",
        deepseek_base_url=(
            os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        ).strip()
        or "https://api.deepseek.com",
        telegram_source_invites=source_invites,
        telegram_source_chat_ids=source_chats,
        telegram_api_id=api_id,
        telegram_api_hash=api_hash,
        telegram_user_phone=user_phone,
        telegram_user_session=BASE_DIR / session_name,
        telegram_user_enabled=user_enabled,
    )
