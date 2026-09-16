"""Парсинг постов из Telegram-групп (те же лиды SEO/GEO, что и в Threads)."""

from __future__ import annotations

import re
import time
from typing import Iterable

from config import Settings
from database import PostDatabase
from deepseek_filter import DeepSeekFilter
from filters import (
    has_intent,
    has_seo_marker,
    is_mostly_russian,
    looks_ukrainian,
    scrub_post_text,
)
from models import ThreadPost, logger
from telegram_sender import TelegramSender, lead_keyboard, tg_lead_keyboard

INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
PUBLIC_USER_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$",
    re.IGNORECASE,
)
META_PREFIX = "tg_source_chat:"


def is_invite_ref(raw: str) -> bool:
    text = (raw or "").strip()
    if not text:
        return False
    if text.startswith("+"):
        return True
    if INVITE_RE.search(text):
        return True
    if "joinchat/" in text.casefold():
        return True
    return False


def public_username(raw: str) -> str:
    """Публичный @username чата. Invite-hash (+/joinchat) — не username."""
    text = (raw or "").strip()
    if not text or is_invite_ref(text):
        return ""
    if text.startswith("@"):
        name = text[1:].strip()
        return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name) else ""
    m = PUBLIC_USER_RE.search(text)
    if m:
        name = m.group(1)
        if name.casefold() in {"joinchat", "share", "addstickers", "socks", "proxy", "c"}:
            return ""
        return name
    # Голый токен только если явно похож на username (без «случайного» invite-hash)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", text):
        # Invite-хэши часто длинные и с цифрами вперемешку — username обычно короче
        if len(text) >= 22 and re.search(r"\d", text):
            return ""
        return text
    return ""


def invite_hash(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    m = INVITE_RE.search(text)
    if m:
        return m.group(1)
    if text.startswith("+"):
        return text[1:].strip()
    if text.casefold().startswith("joinchat/"):
        return text.split("/", 1)[-1].strip()
    return ""


def source_ref(raw: str) -> str:
    """
    Каноническая ссылка источника:
    '+HASH' — приватный invite; 'username' — публичный чат.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    h = invite_hash(text) if is_invite_ref(text) else ""
    if h:
        return f"+{h}"
    # Иногда в .env уже лежит голый hash без '+'
    if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", text) and not public_username("@" + text):
        # если не проходит как @username — считаем invite
        if re.search(r"\d", text) and re.search(r"[A-Z]", text):
            return f"+{text}"
    uname = public_username(text)
    if uname:
        return uname
    # последний шанс: голый hash из parse
    h2 = invite_hash(text) or (text[1:] if text.startswith("+") else "")
    if h2 and is_invite_ref(text):
        return f"+{h2}"
    return ""


def parse_invite_list(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,;\n]+", raw.strip())
    else:
        parts = list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = (part or "").strip()
        if not part or part.startswith("#"):
            continue
        ref = source_ref(part)
        if not ref:
            continue
        key = ref.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def load_communities_file(path) -> list[str]:
    """Читает telegram_communities.txt (по строке / через запятую)."""
    try:
        from pathlib import Path

        p = Path(path)
        if not p.is_file():
            return []
        return parse_invite_list(p.read_text(encoding="utf-8"))
    except OSError:
        return []


def communities_file_path():
    from pathlib import Path

    return Path(__file__).resolve().parent / "telegram_communities.txt"


def read_community_urls(path=None) -> list[str]:
    """Сырые URL/строки из файла (для списка в боте)."""
    from pathlib import Path

    p = Path(path) if path else communities_file_path()
    if not p.is_file():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r"[,;]+", line):
            part = part.strip()
            if not part:
                continue
            ref = source_ref(part)
            key = (ref or part).casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(part)
    return out


def append_community_urls(raw_items: list[str] | str, path=None) -> list[str]:
    """
    Добавляет ссылки в telegram_communities.txt.
    Возвращает список новых канонических ref (+hash / username).
    """
    from pathlib import Path

    p = Path(path) if path else communities_file_path()
    if isinstance(raw_items, str):
        parts = re.split(r"[,;\s\n]+", raw_items.strip())
    else:
        parts = list(raw_items)

    existing_refs = {r.casefold() for r in load_communities_file(p)}
    added_refs: list[str] = []
    added_urls: list[str] = []
    for part in parts:
        part = (part or "").strip()
        if not part or part.startswith("#"):
            continue
        # разрешаем @name, t.me/..., +hash
        if part.startswith("@"):
            url = f"https://t.me/{part.lstrip('@')}"
        elif part.startswith("http") or part.startswith("t.me/") or part.startswith("+"):
            url = part if part.startswith("http") or part.startswith("+") else f"https://{part}"
        else:
            url = f"https://t.me/{part}"
        ref = source_ref(url) or source_ref(part)
        if not ref:
            continue
        if ref.casefold() in existing_refs:
            continue
        existing_refs.add(ref.casefold())
        added_refs.append(ref)
        added_urls.append(url if url.startswith("http") else part)

    if not added_urls:
        return []

    prev = ""
    if p.is_file():
        prev = p.read_text(encoding="utf-8").rstrip() + "\n"
    elif not prev:
        prev = "# Группы-источники лидов (бот /addgroup и каталог)\n"
    block = "\n".join(added_urls) + "\n"
    p.write_text(prev + block, encoding="utf-8")
    return added_refs


def remove_community_urls(raw_items: list[str] | str, path=None) -> list[str]:
    """Удаляет ссылки из файла. Возвращает удалённые ref."""
    from pathlib import Path

    p = Path(path) if path else communities_file_path()
    if isinstance(raw_items, str):
        want = parse_invite_list(raw_items)
    else:
        want = parse_invite_list(",".join(raw_items))
    want_keys = {w.casefold() for w in want}
    if not want_keys or not p.is_file():
        return []

    kept: list[str] = []
    removed: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            kept.append(line)
            continue
        ref = source_ref(s)
        if ref and ref.casefold() in want_keys:
            removed.append(ref)
            continue
        kept.append(line)
    p.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    return removed


def merge_invites_into_settings(settings, extra_refs: list[str]) -> list[str]:
    """Дописывает ref в settings.telegram_source_invites. Возвращает реально новые."""
    cur = list(getattr(settings, "telegram_source_invites", ()) or [])
    seen = {x.casefold() for x in cur}
    added = []
    for ref in extra_refs:
        if not ref or ref.casefold() in seen:
            continue
        cur.append(ref)
        seen.add(ref.casefold())
        added.append(ref)
    settings.telegram_source_invites = tuple(cur)
    return added


def chat_id_aliases(cid: int) -> set[int]:
    """-100… / внутренний id канала — один и тот же чат."""
    try:
        n = int(cid)
    except (TypeError, ValueError):
        return set()
    out = {n}
    s = str(n)
    if s.startswith("-100") and len(s) > 4:
        rest = int(s[4:])
        out.add(rest)
        out.add(-rest)
    elif n > 0:
        out.add(int(f"-100{n}"))
        out.add(-n)
    else:
        out.add(int(f"-100{abs(n)}"))
        out.add(abs(n))
    return out


def chat_in_set(cid: int, ids: set[int] | Iterable[int]) -> bool:
    want = chat_id_aliases(cid)
    for item in ids:
        if want & chat_id_aliases(int(item)):
            return True
    return False


def parse_chat_ids(raw: str | Iterable[int | str] | None) -> set[int]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = re.split(r"[,;\s]+", raw.strip())
    else:
        parts = [str(x) for x in raw]
    out: set[int] = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def chat_message_url(chat_id: int, message_id: int) -> str:
    """Публичная ссылка на сообщение в супергруппе/канале."""
    s = str(int(chat_id))
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{int(message_id)}"
    return f"https://t.me/c/{abs(int(chat_id))}/{int(message_id)}"


def message_text(message) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def message_to_post(message, *, source_label: str = "telegram") -> ThreadPost | None:
    text = scrub_post_text(message_text(message)) or message_text(message)
    if len(text) < 8:
        return None
    chat = message.chat
    chat_id = int(chat.id)
    msg_id = int(message.message_id)
    user = getattr(message, "from_user", None) or getattr(message, "sender_chat", None)
    username = ""
    if user is not None:
        username = (
            getattr(user, "username", None)
            or getattr(user, "title", None)
            or ""
        ).strip()
        if not username:
            uid = getattr(user, "id", None)
            first = (getattr(user, "first_name", None) or "").strip()
            username = first or (f"id{uid}" if uid else "unknown")
    username = username.lstrip("@") or "unknown"
    author_tg_id = None
    if user is not None:
        try:
            author_tg_id = int(getattr(user, "id", 0) or 0) or None
        except (TypeError, ValueError):
            author_tg_id = None
    return ThreadPost(
        post_id=f"tg:{chat_id}:{msg_id}",
        code=str(msg_id),
        username=username,
        text=text,
        url=chat_message_url(chat_id, msg_id),
        image_url=None,
        hashtag=source_label,
        comments_count=0,
        source="telegram",
        author_tg_id=author_tg_id,
    )


def bound_chat_ids(db: PostDatabase, invites: list[str]) -> set[int]:
    ids: set[int] = set()
    for h in invites:
        key = source_ref(h) or (h or "").strip()
        if not key:
            continue
        for variant in {key, key.casefold(), key.lstrip("+"), f"+{key.lstrip('+')}"}:
            raw = db.get_meta(f"{META_PREFIX}{variant}")
            if not raw:
                continue
            try:
                ids.add(int(raw))
                break
            except ValueError:
                continue
    return ids


def bind_chat(db: PostDatabase, invite: str, chat_id: int, title: str = "") -> None:
    key = source_ref(invite) or invite_hash(invite) or public_username(invite) or (invite or "").strip()
    if not key:
        return
    db.set_meta(f"{META_PREFIX}{key}", str(int(chat_id)))
    # дубли ключей для старых записей без '+'
    alt = key[1:] if key.startswith("+") else f"+{key}"
    if alt != key:
        db.set_meta(f"{META_PREFIX}{alt}", str(int(chat_id)))
    if title:
        db.set_meta(f"{META_PREFIX}{key}:title", title)


def unbound_invites(db: PostDatabase, invites: list[str]) -> list[str]:
    out = []
    for h in invites:
        key = source_ref(h) or (h or "").strip()
        if not key:
            continue
        if not any(
            db.get_meta(f"{META_PREFIX}{v}")
            for v in {key, key.casefold(), key.lstrip("+"), f"+{key.lstrip('+')}"}
        ):
            out.append(key)
    return out


def deliver_tg_post(
    settings: Settings,
    db: PostDatabase,
    post: ThreadPost,
    *,
    sender: TelegramSender | None = None,
) -> int:
    """Шлёт лид в ЛС белому списку. Возвращает число успешных ЛС."""
    bot = sender or TelegramSender(settings)
    allowed = set(getattr(settings, "allowed_tg_ids", ()) or ())
    try:
        extra = int(getattr(settings, "alert_tg_id", 0) or 0)
    except (TypeError, ValueError):
        extra = 0
    if extra:
        allowed.add(extra)
    users = db.all_started_users()
    if allowed:
        users = [u for u in users if int(u.get("user_id", 0)) in allowed]
    if not users:
        logger.warning("TG-источник: нет получателей (/start у whitelist)")
        return 0

    ok_n = 0
    for user in users:
        if db.user_already_got(user["user_id"], post.post_id):
            continue
        lead_id = db.save_lead(post)
        if getattr(post, "is_telegram", False):
            markup = tg_lead_keyboard(post)
        else:
            markup = lead_keyboard(lead_id) if lead_id else None
        if bot.send_post(post, chat_id=user["chat_id"], reply_markup=markup):
            db.mark_sent_to_user(user["user_id"], post.post_id)
            ok_n += 1
        time.sleep(0.35)
    if ok_n:
        db.add(post.post_id, post.hashtag, post.url)
    return ok_n


def evaluate_and_deliver(
    settings: Settings,
    db: PostDatabase,
    message,
    *,
    ai: DeepSeekFilter | None = None,
    sender: TelegramSender | None = None,
    require_russian: bool = True,
) -> tuple[bool, str]:
    """
    Фильтр DeepSeek (как для Threads) → рассылка.
    Returns (sent_or_skipped_ok, reason).
    """
    post = message_to_post(message)
    if not post:
        return False, "пусто"
    return evaluate_and_deliver_post(
        settings,
        db,
        post,
        ai=ai,
        sender=sender,
        require_russian=require_russian,
    )


def telethon_message_to_post(
    message,
    *,
    source_label: str = "telegram",
    chat_id: int | None = None,
) -> ThreadPost | None:
    """Сообщение Telethon → ThreadPost."""
    raw = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
    text = scrub_post_text(raw) or raw
    if len(text) < 8:
        return None
    msg_id = int(getattr(message, "id", 0) or 0)
    cid = int(chat_id or 0) or int(getattr(message, "chat_id", 0) or 0)
    if not cid:
        try:
            from telethon.utils import get_peer_id

            peer = getattr(message, "peer_id", None)
            if peer is not None:
                cid = int(get_peer_id(peer))
        except Exception:
            cid = 0
    if not cid or not msg_id:
        return None
    username = "unknown"
    sender = getattr(message, "sender", None)
    if sender is not None:
        username = (getattr(sender, "username", None) or "").strip()
        if not username:
            first = (getattr(sender, "first_name", None) or "").strip()
            title = (getattr(sender, "title", None) or "").strip()
            uid = getattr(sender, "id", None)
            username = first or title or (f"id{uid}" if uid else "unknown")
    username = username.lstrip("@") or "unknown"
    author_tg_id = None
    if sender is not None:
        try:
            author_tg_id = int(getattr(sender, "id", 0) or 0) or None
        except (TypeError, ValueError):
            author_tg_id = None
    return ThreadPost(
        post_id=f"tg:{cid}:{msg_id}",
        code=str(msg_id),
        username=username,
        text=text,
        url=chat_message_url(cid, msg_id),
        image_url=None,
        hashtag=source_label,
        comments_count=0,
        source="telegram",
        author_tg_id=author_tg_id,
    )


# Триггеры из xlsx «Триггеры» — дешёвый префильтр до DeepSeek
TG_LEAD_HINT_RE = re.compile(
    r"(?iu)(?:"
    r"ищу\s+сео|нужен\s+сео|нужна\s+сео|нужно\s+сео|сеошник|сео\s*агентств|"
    r"подрядчик\s+по\s+сео|сео\s+подрядчик|посоветуйте\s+сео|"
    r"кого\s+порекомендуете\s+по\s+сео|проверенн\w*\s+сео|"
    r"ищу\s+сео[\s\-]?специалист|вакансия\s+сео|сео\s+в\s+штат|"
    r"упал[аи]?\s+(?:позици|органик|трафик)|пропал\w*\s+из\s+поиска|"
    r"не\s+индексир|выпал\w*\s+из\s+(?:индекса|поиска)|"
    r"сео[\s\-]?аудит|аудит\s+(?:сайта|семантики)|"
    r"запускаем\s+сайт|редизайн\s+сайт|переезд\s+сайт|"
    r"больше\s+органики|рост\s+трафика\s+из\s+поиска|масштабировать\s+сео|"
    r"контент[\s\-]?стратег|статьи\s+не\s+дают\s+трафик|"
    r"собрать\s+семантик|кластеризац\w*\s+запрос|яндекс\s+карт|"
    r"google\s+maps|локальн\w+\s+выдач|продвинуть\s+карточк|"
    r"управлен\w*\s+репутац|serm|"
    r"категори\w*\s+не\s+ранжир|трафик\s+интернет[\s\-]?магазин|"
    r"конкурент\w*\s+выше\s+в\s+поиске|сменить\s+подрядчик|"
    r"агентство\s+не\s+отвечает|сео\s+не\s+дает|"
    r"поисков\w+\s+трафик|search\s+console|вебмастер|"
    r"переезд\s+домена|смен[аы]\s+cms|"
    r"видимость\s+в\s+(?:chatgpt|ии|нейросет)|гео\s+продвижен|"
    r"продвижени[еюя]\s+сайт|вывести\s+(?:сайт\s+)?в\s+топ|"
    r"сколько\s+стоит\s+сео|нужен\s+аудит"
    r")"
)


def tg_looks_like_lead(text: str) -> bool:
    """Дешёвый отсев болтовни в группах до DeepSeek (триггеры из xlsx)."""
    raw = scrub_post_text(text or "") or (text or "")
    if len(raw.strip()) < 12:
        return False
    if TG_LEAD_HINT_RE.search(raw):
        return True
    if has_seo_marker(raw) and has_intent(raw):
        return True
    return False


def evaluate_and_deliver_post(
    settings: Settings,
    db: PostDatabase,
    post: ThreadPost,
    *,
    ai: DeepSeekFilter | None = None,
    sender: TelegramSender | None = None,
    require_russian: bool = True,
) -> tuple[bool, str]:
    """Префильтр + DeepSeek + рассылка по готовому ThreadPost."""
    if db.exists(post.post_id):
        return False, "уже было"
    raw = post.text or ""
    if require_russian:
        if looks_ukrainian(raw):
            db.log_pipeline("telegram", "drop", 1, "украинский")
            return False, "украинский"
        if not is_mostly_russian(raw):
            db.log_pipeline("telegram", "drop", 1, "не RU")
            return False, "не RU"

    if not tg_looks_like_lead(raw):
        db.log_pipeline("telegram", "drop", 1, "нет SEO/GEO-сигнала")
        return False, "нет SEO/GEO-сигнала"

    filter_ai = ai
    if filter_ai is None and getattr(settings, "deepseek_enabled", False):
        key = getattr(settings, "deepseek_api_key", "") or ""
        if key:
            filter_ai = DeepSeekFilter(
                key,
                base_url=getattr(settings, "deepseek_base_url", "https://api.deepseek.com"),
                model=getattr(settings, "deepseek_model", "deepseek-chat"),
            )
    if filter_ai and filter_ai.enabled:
        ok, why = filter_ai.is_good_lead(raw, "telegram")
        if not ok:
            db.log_pipeline("telegram", "drop", 1, f"DeepSeek: {why}")
            return False, f"DeepSeek: {why}"
        reason = why
    else:
        reason = "без DeepSeek"

    n = deliver_tg_post(settings, db, post, sender=sender)
    if n <= 0:
        db.log_pipeline("telegram", "dm_fail", 1, post.post_id)
        return False, "некому слать"
    db.log_pipeline("telegram", "sent", n, post.post_id)
    return True, f"ЛС×{n} · {reason}"
