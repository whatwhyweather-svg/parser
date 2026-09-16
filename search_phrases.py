"""Ключевые слова / фразы для поиска в Threads (не хэштеги).

Хранятся в search_phrases.txt — по одной фразе на строку.
Управление: Telegram /addphrase /delphrase /phrases.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import BASE_DIR

PHRASES_FILE = BASE_DIR / "search_phrases.txt"

# Слишком общие одиночные слова — не тащим в поиск как запрос
_SKIP_SHORT = {
    "кто",
    "как",
    "уже",
    "лс",
    "влс",
    "в лс",
    "бан",
    "минус",
    "агс",
    "cms",
    "301",
    "sku",
    "акт",
    "фильтр",
    "бюджет",
    "подряд",
    "нейро",
    "контекст",
    "директ",
    "индекс",
    "аудит",
    "гео",
    "geo",
    "aeo",
    "llmo",
}

_SECTION_IMPORT = re.compile(
    r"(?iu)запрос|опечатк|топ-?\s*15|ежедневн|еженедельн"
)
_SECTION_SKIP = re.compile(
    r"(?iu)анти-?маркер|оператор\w*\s+смысл|маркер\w*\s+лида|пример"
)


def phrases_path() -> Path:
    return PHRASES_FILE


def normalize_phrase(raw: str) -> str:
    """Одна поисковая фраза: пробелы схлопываем, # оставляем."""
    text = (raw or "").strip()
    if not text or text.startswith("#") and len(text) <= 1:
        return ""
    # заголовки файла-каталога
    if text.startswith("=") or re.match(r"^\d+\.\s", text):
        return ""
    if text.isupper() and len(text) > 12:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 2:
        return ""
    # целые абзацы из «примеров» в поиск Threads не годятся
    if len(text) > 90:
        return ""
    return text


def parse_phrase_list(raw: str) -> list[str]:
    """Несколько фраз: с новой строки или через ; (запятая — только если фраза без пробелов)."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for block in re.split(r"[\n;]+", text):
        block = block.strip()
        if not block:
            continue
        if "," in block and " " not in block.strip(","):
            for p in block.split(","):
                n = normalize_phrase(p)
                if n:
                    parts.append(n)
        else:
            n = normalize_phrase(block)
            if n:
                parts.append(n)
    return dedupe_phrases(parts)


def dedupe_phrases(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def read_phrases() -> list[str]:
    path = phrases_path()
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        n = normalize_phrase(line)
        if n and not n.startswith("//"):
            out.append(n)
    return dedupe_phrases(out)


def write_phrases(phrases: list[str]) -> None:
    path = phrases_path()
    lines = dedupe_phrases([normalize_phrase(p) for p in phrases if normalize_phrase(p)])
    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def append_phrases(raw: str) -> list[str]:
    """Добавить фразы. Возвращает реально новые."""
    incoming = parse_phrase_list(raw)
    if not incoming:
        return []
    current = read_phrases()
    have = {p.casefold() for p in current}
    added: list[str] = []
    for p in incoming:
        if p.casefold() in have:
            continue
        current.append(p)
        have.add(p.casefold())
        added.append(p)
    if added:
        write_phrases(current)
    return added


def remove_phrases(raw: str) -> list[str]:
    """Удалить фразы (точное совпадение без регистра)."""
    targets = {p.casefold() for p in parse_phrase_list(raw)}
    if not targets:
        return []
    current = read_phrases()
    kept: list[str] = []
    removed: list[str] = []
    for p in current:
        if p.casefold() in targets:
            removed.append(p)
        else:
            kept.append(p)
    if removed:
        write_phrases(kept)
    return removed


def import_catalog_file(path: Path | str) -> list[str]:
    """Разобрать каталог threads-seo-geo-phrases.txt → добавить в search_phrases.txt."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    text = src.read_text(encoding="utf-8")
    section = ""
    collected: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("="):
            continue
        if re.match(r"^\d+\.\s", s) or (
            len(s) > 8 and s == s.upper() and re.search(r"[A-ZА-Я]", s)
        ):
            section = s
            continue
        if _SECTION_SKIP.search(section):
            continue
        # секции с готовыми запросами + короткие нишевые слова (осторожно)
        is_query_sec = bool(_SECTION_IMPORT.search(section))
        is_short_sec = bool(re.search(r"(?iu)короткие\s+слов", section))
        n = normalize_phrase(s)
        if not n:
            continue
        if n.casefold() in _SKIP_SHORT:
            continue
        if is_query_sec:
            collected.append(n)
        elif is_short_sec:
            # одно слово / #тег длиной ≥4, не оператор
            if n.startswith("#") or (len(n) >= 4 and " " not in n):
                collected.append(n)
        elif re.search(r"(?iu)^\d+\.\s*топ|ежеднев|еженедел|пример", section):
            collected.append(n)

    added = append_phrases("\n".join(collected))
    # если файл был пуст — append уже записал; если всё уже было — ok
    if not added and collected:
        # возможно файл уже содержал те же — всё равно убедимся что база есть
        if not read_phrases():
            write_phrases(collected)
            return dedupe_phrases(collected)
    return added


def phrases_as_queries() -> list[tuple[str, str]]:
    """Для скрейпера: (label, query). Только фразы, пригодные как поисковый запрос."""
    return [(p, p) for p in search_worthy_phrases()]


_INTENT_IN_QUERY = re.compile(
    r"(?iu)ищу|ишу|ищем|ишем|нужен|нужна|нужно|нужны|посоветуйте|порекомендуйте|"
    r"подскажите|кто\s+(?:делает|ведёт|ведет|занимается|может)|заказать|"
    r"сколько\s+стоит|подрядчик|сеошник|looking\s+for|hire\s+seo|"
    r"need\s+seo|помогите|киньте|ведение\s+сео|ребят\s+нужен|народ\s+кто"
)

_PAIN_IN_QUERY = re.compile(
    r"(?iu)упал|просел|выпал|кинуло|воздух|нет\s+заявок|мало\s+трафика|"
    r"жр[её]т\s+бюджет|органика\s+упал|позиции\s+упал|агентство\s+кинуло"
)

_NOISE_SOLO = {
    "google",
    "яндекс",
    "chatgpt",
    "perplexity",
    "трафик",
    "заявки",
    "бюджет",
    "продвижение",
    "аудит",
    "семантика",
    "переезд",
    "индексация",
    "вебмастер",
    "органика",
    "директ",
    "контекст",
    "алиса",
    "нейро",
    "фильтр",
    "агс",
    "минус",
    "бан",
    "cms",
    "301",
    "sku",
    "подряд",
    "миграци",
    "сэо",
    "тильда",
}

# Не тащим в Threads Search — дают витрины и оффтоп, не заявки
_NOISE_QUERY = {
    "кассовый разрыв",
    "кассовый разрыв реклама",
    "минус площадка",
    "после смены cms",
    "раскрутка сайта",
    "оптимизация сайта",
    "продвижение",
    "тильда",
    "tilda",
    "wordpress",
    "битрикс",
    "shopify",
    "1с-битрикс",
    "напишите в лс",
    "пишите в лс",
    "киньте в лс",
    "сео или контекст",
    "сео или директ",
    "режем директ",
    "реклама дорогая",
    "реклама дорогая заявки",
    "директ жрёт бюджет",
    "google ai",
    "ai overviews",
    "seo audit",
    "технический аудит",
    "переезд сайта",
    "миграци",
}


def search_worthy_phrases() -> list[str]:
    """Только фразы найма/боли — иначе Threads заливает ленту оффтопом."""
    out: list[str] = []
    for p in read_phrases():
        low = p.casefold().strip()
        if not low or low in _NOISE_SOLO or low in _NOISE_QUERY:
            continue
        if low.startswith("#"):
            if low in {"#seo", "#сео", "#geo", "#гео", "#продвижениесайта", "#сеопродвижение"}:
                out.append(p)
            continue
        # одиночное «сеошник»/«подрядчик» без «ищу» — мусор в поиске
        if " " not in low:
            continue
        if _INTENT_IN_QUERY.search(low) or _PAIN_IN_QUERY.search(low):
            out.append(p)
    return dedupe_phrases(out)


def query_priority(query: str) -> int:
    """Выше = раньше в ротации (точнее к лиду)."""
    q = (query or "").strip().casefold()
    score = 0
    if _INTENT_IN_QUERY.search(q):
        score += 100
    if any(
        w in q
        for w in ("ищу", "ищем", "ишем", "нужен", "посоветуйте", "заказать", "подрядчик")
    ):
        score += 40
    if " " in q:
        score += 20
    if q.startswith("#"):
        score -= 50
    if q in _NOISE_SOLO:
        score -= 100
    score += min(30, len(q))
    return score


def text_hits_keyword(text: str) -> tuple[bool, str]:
    """
    Пост содержит ключ/фразу из search_phrases.txt.
    Пустой файл → не блокируем (только builtin-фильтры).
    """
    phrases = read_phrases()
    if not phrases:
        return True, ""
    body = re.sub(r"#[\wА-Яа-яЁё]+", " ", text or "")
    body = re.sub(r"\s+", " ", body).casefold()
    # длинные фразы важнее коротких «сео»
    for p in sorted(phrases, key=lambda x: len(x), reverse=True):
        key = p.casefold().strip().lstrip("#")
        if len(key) < 3:
            continue
        if key in body:
            return True, p
    return False, ""
