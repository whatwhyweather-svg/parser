"""Нормализация и проверка хэштегов / топиков Threads."""

from __future__ import annotations

import re
from typing import Any

from nested_lookup import nested_lookup


def normalize_hashtag(raw: str) -> str:
    """Возвращает тег без # и пробелов."""
    tag = (raw or "").strip().lstrip("#").strip()
    tag = re.sub(r"\s+", "", tag)
    return tag


def normalize_hashtags(items: list[str] | str) -> list[str]:
    if isinstance(items, str):
        parts = re.split(r"[,;\s]+", items)
    else:
        parts = items

    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = normalize_hashtag(part)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def display_hashtag(tag: str) -> str:
    return f"#{normalize_hashtag(tag)}"


def clean_post_text(text: str) -> str:
    """Убирает служебное время Threads («1 мин.», «1 minute», «3h») из текста."""
    if not text:
        return ""
    time_line = re.compile(
        r"^(?:"
        r"\d+\s*(?:мин\.?|минуту|минуты|минут|minutes?|mins?|min|m|"
        r"ч\.?|час|часа|часов|hours?|hrs?|h|"
        r"д\.?|день|дня|дней|days?|d|"
        r"сек\.?|seconds?|secs?|s)"
        r"(?:\s+ago)?"
        r"|"
        r"(?:a|an|one)\s+(?:minute|hour|day|second)\s+ago|"
        r"только что|just now|now|сейчас"
        r")$",
        re.IGNORECASE,
    )
    time_prefix = re.compile(
        r"^(?:\d+\s*(?:мин\.?|minutes?|mins?|min|m|ч\.?|hours?|hrs?|h|"
        r"д\.?|days?|d|сек\.?|seconds?|secs?|s)(?:\s+ago)?|"
        r"(?:a|an|one)\s+(?:minute|hour|day|second)\s+ago)\s*",
        re.IGNORECASE,
    )
    ui_noise = {
        "перевести",
        "translate",
        "like",
        "reply",
        "share",
        "ответить",
        "поделиться",
    }
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip().strip("·•|").strip()
        if not s:
            continue
        if time_line.match(s):
            continue
        if s.casefold() in ui_noise:
            continue
        s = time_prefix.sub("", s).strip()
        if s and not time_line.match(s):
            out.append(s)
    return "\n".join(out).strip()


def text_contains_hashtag(text: str, tag: str) -> bool:
    """В тексте есть классический #тег."""
    tag = normalize_hashtag(tag)
    if not tag:
        return False
    pattern = re.compile(
        rf"(?<![\wа-яА-ЯёЁ])#{re.escape(tag)}(?![\wа-яА-ЯёЁ])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text or ""))


def text_mentions_tag(text: str, tag: str) -> bool:
    """#тег или топик без # (как в UI: username > TopicName)."""
    tag = normalize_hashtag(tag)
    if not tag:
        return False
    if text_contains_hashtag(text, tag):
        return True
    pattern = re.compile(
        rf"(?<![\wа-яА-ЯёЁ]){re.escape(tag)}(?![\wа-яА-ЯёЁ])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text or ""))


def extract_topic_names(post: dict[str, Any]) -> list[str]:
    """Достаёт имена топиков только из явных topic-полей (без мусорных name)."""
    names: list[str] = []
    info = post.get("text_post_app_info") or {}

    for key in (
        "topic",
        "topic_tag",
        "tag",
        "display_topic",
        "text_post_app_topic",
        "topic_name",
    ):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            names.append(val.strip())
        elif isinstance(val, dict):
            for nk in ("name", "display_name", "title", "tag_name"):
                if isinstance(val.get(nk), str) and val[nk].strip():
                    names.append(val[nk].strip())

    # Иногда топик лежит глубже, но только по ключам topic*
    for key in ("topic_name", "display_topic", "tag_name"):
        for val in nested_lookup(key, info):
            if isinstance(val, str) and 2 < len(val) < 80:
                names.append(val.strip())

    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def post_matches_tag(
    text: str,
    post: dict[str, Any] | None,
    tag: str,
    *,
    from_tag_search: bool = False,
    own_post: bool = False,
) -> bool:
    """
    Пост с страницы #поиска уже в выдаче тега — текст без слова SEO тоже берём.
    Иначе «ищу продвижение сайта» в топике SEO выкидывался.
    """
    _ = own_post
    tag_n = normalize_hashtag(tag)
    if not tag_n:
        return False
    if from_tag_search:
        return True
    if text_mentions_tag(text or "", tag_n):
        return True
    topics = extract_topic_names(post or {})
    return any(normalize_hashtag(n).casefold() == tag_n.casefold() for n in topics)


# Threads режет частые поиски (HTTP 429). Один цикл — немного запросов,
# остальные доберём в следующих циклах по кругу.
QUERIES_PER_CYCLE = 5
CORE_QUERIES = 2
# За сутки #GEO съел 2699 поисков и дал 0 лидов, #SEO — 4 лида.
# Поэтому лимит запросов делим не поровну.
TAG_BUDGET = {"seo": 4, "сео": 4, "geo": 1, "гео": 1}
_rotation: dict[str, int] = {}


def extra_search_tags(tag: str) -> list[str]:
    """Совместимость: уникальные теги из поисковых запросов."""
    seen: set[str] = set()
    out: list[str] = []
    for _label, query in search_queries_for_tag(tag):
        raw = query.lstrip("#").split()[0] if query.strip() else ""
        n = normalize_hashtag(raw)
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out or [normalize_hashtag(tag)]


def search_queries_for_tag(tag: str) -> list[tuple[str, str]]:
    """Подмножество запросов на цикл: ядро + ротация (Threads даёт 429 за частые поиски)."""
    full = all_search_queries_for_tag(tag)
    key = normalize_hashtag(tag).casefold()
    budget = TAG_BUDGET.get(key, QUERIES_PER_CYCLE)
    if len(full) <= budget:
        return full
    core_n = min(CORE_QUERIES, max(0, budget - 1))
    core = full[:core_n]
    rest = full[core_n:]
    take = budget - core_n
    offset = _rotation.get(key, 0) % len(rest)
    picked = [rest[(offset + i) % len(rest)] for i in range(take)]
    _rotation[key] = offset + take
    return core + picked


def all_search_queries_for_tag(tag: str) -> list[tuple[str, str]]:
    """Полный список: hire-фразы + ленты тега."""
    key = normalize_hashtag(tag).casefold()
    t = normalize_hashtag(tag)

    if key in {"seo", "сео"}:
        # Кириллица первой: латинская лента #SEO на 70% англоязычная.
        # Много разных запросов = разные выдачи = шире охват заказчиков.
        base = [
            ("ищу сеошника", "ищу сеошника"),
            ("#сео лента", "#сео"),
            ("нужен сео", "нужен сео"),
            ("продвижение сайта", "продвижение сайта"),
            (f"#{t} лента", f"#{t}"),
            ("ищу seo специалиста", "ищу seo специалиста"),
            ("органика упала кто", "органика упала"),
            ("посоветуйте сеошника", "посоветуйте сеошника"),
            ("нужен сеошник", "нужен сеошник"),
            ("кто занимается seo", "кто занимается seo"),
            ("#сеопродвижение", "#сеопродвижение"),
            ("#продвижениесайта", "#продвижениесайта"),
            ("#сеоспециалист", "#сеоспециалист"),
            ("сайт не в поиске", "сайт не в поиске"),
            ("нужен аудит сайта", "нужен аудит сайта"),
            ("вывести сайт в топ", "вывести сайт в топ"),
        ]
    elif key in {"geo", "гео"}:
        # Свои запросы: общие hire-фразы уже отрабатывает #SEO, не дублируем
        base = [
            (f"#{t} лента", f"#{t}"),
            ("продвижение в chatgpt", "продвижение в chatgpt"),
            ("продвижение в нейросетях", "продвижение в нейросетях"),
            ("оптимизация под нейросети", "оптимизация под нейросети"),
            ("#гео лента", "#гео"),
            ("не находит chatgpt", "не находит chatgpt"),
        ]
    else:
        base = [
            (f"ищу {t}", f"ищу {t}"),
            (f"нужен {t}", f"нужен {t}"),
            (f"#{t} лента", f"#{t}"),
        ]

    # /addphrase — только многословные hire-фразы, и только для SEO:
    # под GEO они дали бы те же посты второй раз
    if key in {"seo", "сео"}:
        try:
            from search_phrases import search_worthy_phrases, query_priority

            seen = {q.casefold() for _l, q in base}
            extras: list[tuple[int, str]] = []
            for phrase in search_worthy_phrases():
                q = (phrase or "").strip()
                if not q or q.casefold() in seen:
                    continue
                if " " not in q or q.startswith("#"):
                    continue
                extras.append((query_priority(q), q))
            extras.sort(key=lambda x: -x[0])
            for _prio, q in extras[:3]:
                if len(base) >= 19:
                    break
                if q.casefold() in seen:
                    continue
                seen.add(q.casefold())
                base.append((q[:40], q))
        except Exception:
            pass
    return base[:19]


def query_fetch_cap(query: str) -> int:
    """Сколько карточек тянуть с одного Recent URL."""
    q = (query or "").strip().casefold()
    if q.startswith("#"):
        return 40
    return 30


def build_recent_search_url(query: str) -> str:
    return f"https://www.threads.com/search?q={quote_tag(query)}&filter=recent"


def build_search_urls(tag: str) -> list[tuple[str, str]]:
    """Сначала свежие (Recent), потом популярные."""
    tag = normalize_hashtag(tag)
    encoded_hash = quote_tag(f"#{tag}")
    return [
        (
            "recent filter",
            f"https://www.threads.com/search?q={encoded_hash}&filter=recent",
        ),
        (
            "plain #",
            f"https://www.threads.com/search?q={encoded_hash}",
        ),
    ]


def quote_tag(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
