"""Telegram-бот: меню, теги, подписки пользователей."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from config import Settings
from database import PostDatabase
from hashtags import display_hashtag, normalize_hashtags
from models import logger, redact_secrets

# infinity_polling иначе пишет полный traceback 409 в консоль start.bat
logging.getLogger("TeleBot").setLevel(logging.CRITICAL)

TagsChangedCallback = Callable[[list[str], list[str]], None]
GroupsChangedCallback = Callable[[list[str], list[str]], None]

telebot.apihelper.CONNECT_TIMEOUT = 20
telebot.apihelper.READ_TIMEOUT = 25
telebot.apihelper.RETRY_ON_ERROR = False

BTN_ADD = "➕ Добавить тег"
BTN_DEL = "➖ Удалить тег"
BTN_TAGS = "🏷 Мои теги"
BTN_ADD_PHRASE = "➕ Ключ.слово"
BTN_DEL_PHRASE = "➖ Ключ.слово"
BTN_PHRASES = "🔑 Ключ.слова"
BTN_ADD_GROUP = "➕ Группа TG"
BTN_DEL_GROUP = "➖ Группа TG"
BTN_GROUPS = "📋 Группы TG"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"

HELP_TEXT = (
    "Я ищу посты в <b>Threads</b> по тегам "
    "(например #SEO) и <b>ключевым словам/фразам</b>, "
    "плюс в <b>Telegram-группах</b>, "
    "присылаю подходящие <b>сюда в личку</b>.\n\n"
    "<b>Что делаю:</b>\n"
    "• смотрю свежие посты по хэштегу и фразам в Threads\n"
    "• читаю новые сообщения в группах-источниках (Telethon)\n"
    "• DeepSeek подстраивается под тег и отбирает тех, кто ищет подрядчика\n"
    "• старые не повторяю\n"
    "• пишу только разрешённым (белый список по ID)\n\n"
    "Пользуйся <b>меню внизу</b> или командами:\n"
    "/add SEO, GEO — теги (хэштеги)\n"
    "/del SEO, GEO — удалить теги\n"
    "/tags — список тегов\n"
    "/addphrase фраза — ключевое слово/фраза поиска\n"
    "/delphrase фраза — убрать фразу\n"
    "/phrases — все ключевые слова\n"
    "/addgroup https://t.me/... — группа-источник\n"
    "/delgroup @name — убрать группу\n"
    "/groups — список групп\n"
    "/help — справка"
)


class TelegramControlBot:
    """Фоновый long-polling бот с меню и белым списком по Telegram ID."""

    def __init__(
        self,
        settings: Settings,
        db: PostDatabase,
        *,
        on_tags_changed: TagsChangedCallback | None = None,
        on_groups_changed: GroupsChangedCallback | None = None,
        on_log: Callable[[str], None] | None = None,
        get_allowed_ids: Callable[[], set[int]] | None = None,
        on_user_seen: Callable[[int], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.on_tags_changed = on_tags_changed
        self.on_groups_changed = on_groups_changed
        self.on_log = on_log
        self.get_allowed_ids = get_allowed_ids
        self.on_user_seen = on_user_seen
        proxy = getattr(settings, "telegram_proxy", None) or None
        if proxy and str(proxy).strip().lower().startswith("mtproto://"):
            proxy = None
        if proxy:
            telebot.apihelper.proxy = {"https": proxy, "http": proxy}
        self.bot = telebot.TeleBot(
            settings.telegram_bot_token,
            parse_mode="HTML",
            threaded=False,
        )
        self._bot_username = ""
        try:
            me = self.bot.get_me()
            self._bot_username = (getattr(me, "username", None) or "").strip()
        except Exception:
            self._bot_username = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[int, str] = {}
        self._comment_busy: set[int] = set()
        self._register_handlers()
        invites = list(getattr(settings, "telegram_source_invites", ()) or ())
        chats = set(getattr(settings, "telegram_source_chat_ids", ()) or ())
        if invites or chats:
            bound = set()
            try:
                from tg_source import bound_chat_ids

                bound = bound_chat_ids(db, invites)
            except Exception:
                bound = set()
            n = len(chats | bound)
            if n:
                self._log(f"TG-источники: {n} чат(ов) подключено")
            elif invites:
                self._log(
                    "TG-группа: бот обычные посты не видит. "
                    "Нажми «ВОЙТИ В TELEGRAM» в программе и введи номер — "
                    "бота в группу звать не надо."
                )

    def _deepseek(self):
        from deepseek_filter import DeepSeekFilter

        key = getattr(self.settings, "deepseek_api_key", "") or ""
        return DeepSeekFilter(
            key,
            base_url=getattr(
                self.settings, "deepseek_base_url", "https://api.deepseek.com"
            ),
            model=getattr(self.settings, "deepseek_model", "deepseek-chat"),
        )

    def _show_draft(self, chat_id: int, lead_id: int, draft: str) -> None:
        from models import escape_html
        from telegram_sender import draft_keyboard

        text = (
            "Черновик комментария:\n\n"
            f"<i>{escape_html(draft)}</i>\n\n"
            "Можно отправить в Threads, сгенерировать заново или написать свой."
        )
        self.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=draft_keyboard(lead_id),
        )

    def _make_draft(self, lead_id: int) -> str:
        lead = self.db.get_lead(lead_id)
        if not lead:
            return ""
        ds = self._deepseek()
        draft = ds.generate_comment(
            lead.get("post_text") or "", lead.get("username") or ""
        )
        if not draft:
            draft = (
                "Привет! Если ещё ищете подрядчика по SEO/GEO — напишите, "
                "подскажу по делу."
            )
        self.db.set_lead_draft(lead_id, draft, status="draft")
        return draft

    def _log(self, message: str) -> None:
        safe = redact_secrets(message)
        logger.info("[TG] %s", safe)
        if self.on_log:
            self.on_log(safe)

    def _allowed_set(self) -> set[int]:
        if self.get_allowed_ids:
            try:
                return {int(x) for x in self.get_allowed_ids()}
            except Exception:
                pass
        return {int(x) for x in (getattr(self.settings, "allowed_tg_ids", ()) or ())}

    def _is_allowed(self, user_id: int) -> bool:
        allowed = self._allowed_set()
        if not allowed:
            return False
        return int(user_id) in allowed

    def _remember_user(self, message) -> tuple[int, bool]:
        """Сохраняем любого, кто написал боту. (uid, is_new)."""
        user = message.from_user
        if not user:
            return 0, False
        uid = int(user.id)
        display = (user.first_name or "").strip()
        if user.last_name:
            display = f"{display} {user.last_name}".strip()
        is_new = self.db.upsert_user(
            uid,
            message.chat.id,
            user.username or "",
            display_name=display,
            active=True,
        )
        return uid, is_new

    def _deny(self, message) -> bool:
        """True = отказано (уже ответили). Пользователь всё равно попадает в список GUI."""
        uid, is_new = self._remember_user(message)
        if not uid:
            return True
        if self._is_allowed(uid):
            return False
        text = (
            "⛔ Пока нет доступа.\n"
            f"Твой Telegram ID: <code>{uid}</code>\n"
            "Админ увидит тебя в программе и сможет разрешить."
        )
        try:
            self.bot.reply_to(message, text, parse_mode="HTML")
        except Exception:
            pass
        if is_new:
            self._log(f"TG запрос доступа · {uid}")
            if self.on_user_seen:
                try:
                    self.on_user_seen(uid)
                except Exception:
                    pass
        return True

    def _main_keyboard(self) -> types.ReplyKeyboardMarkup:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(
            types.KeyboardButton(BTN_ADD),
            types.KeyboardButton(BTN_DEL),
        )
        kb.add(
            types.KeyboardButton(BTN_TAGS),
            types.KeyboardButton(BTN_PHRASES),
        )
        kb.add(
            types.KeyboardButton(BTN_ADD_PHRASE),
            types.KeyboardButton(BTN_DEL_PHRASE),
        )
        kb.add(
            types.KeyboardButton(BTN_ADD_GROUP),
            types.KeyboardButton(BTN_DEL_GROUP),
        )
        kb.add(
            types.KeyboardButton(BTN_GROUPS),
            types.KeyboardButton(BTN_HELP),
        )
        return kb

    def _cancel_keyboard(self) -> types.ReplyKeyboardMarkup:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(types.KeyboardButton(BTN_CANCEL))
        return kb

    def _setup_menu_commands(self) -> None:
        try:
            self.bot.set_my_commands(
                [
                    types.BotCommand("start", "Приветствие и меню"),
                    types.BotCommand("help", "Справка"),
                    types.BotCommand("add", "Добавить теги через запятую"),
                    types.BotCommand("del", "Удалить теги через запятую"),
                    types.BotCommand("tags", "Мои теги"),
                    types.BotCommand("addphrase", "Добавить ключ.слово / фразу"),
                    types.BotCommand("delphrase", "Удалить ключ.слово"),
                    types.BotCommand("phrases", "Список ключ.слов"),
                    types.BotCommand("addgroup", "Добавить TG-группу источник"),
                    types.BotCommand("delgroup", "Удалить TG-группу"),
                    types.BotCommand("groups", "Список TG-групп"),
                ]
            )
        except Exception as exc:
            self._log(f"Не удалось поставить меню команд: {exc}")

    def _ask_keyword(self, message, action: str) -> None:
        uid = message.from_user.id
        self.db.upsert_user(
            uid, message.chat.id, message.from_user.username or "", active=True
        )
        self._pending[uid] = action
        if action == "add":
            text = (
                "Введите <b>теги</b> через запятую, например:\n"
                "<code>SEO, GEO, SMM</code>\n"
                "Или один: <code>SEO</code>\n"
                "<i>Это хэштеги, не ключевые слова.</i>"
            )
        elif action == "addphrase":
            text = (
                "Введите <b>ключевое слово или фразу</b> для поиска в Threads "
                "(не хэштег).\n"
                "Несколько — с новой строки или через <code>;</code>:\n"
                "<code>ищу сеошника</code>\n"
                "<code>упал трафик; видимость chatgpt</code>"
            )
        elif action == "delphrase":
            text = (
                "Какую фразу убрать? Точный текст или несколько через "
                "<code>;</code> / с новой строки.\n"
                "Список: /phrases"
            )
        elif action == "addgroup":
            text = (
                "Пришли ссылку на группу (можно несколько через запятую):\n"
                "<code>https://t.me/seo_chat</code>\n"
                "<code>https://t.me/+AbCdEfGhIjK</code>\n"
                "Или username: <code>@seo_chat</code>"
            )
        elif action == "delgroup":
            text = (
                "Что убрать? Ссылка, @username или +hash:\n"
                "<code>@seo_chat</code> или <code>https://t.me/seo_chat</code>"
            )
        else:
            text = (
                "Что удалить? Можно несколько через запятую:\n"
                "<code>SEO, GEO</code>"
            )
        self.bot.reply_to(
            message,
            text,
            parse_mode="HTML",
            reply_markup=self._cancel_keyboard(),
        )

    def _do_add(self, message, raw: str) -> None:
        uid = message.from_user.id
        added = []
        for tag in normalize_hashtags(raw):
            self.db.add_user_tag(uid, tag)
            added.append(tag)
        if not added:
            self.bot.reply_to(
                message,
                "Не разобрал слово. Пример: SEO, GEO",
                reply_markup=self._main_keyboard(),
            )
            return
        self.bot.reply_to(
            message,
            "Добавлено: " + ", ".join(display_hashtag(t) for t in added),
            reply_markup=self._main_keyboard(),
        )
        self._log(
            f"TG /add · {uid} · {', '.join(display_hashtag(t) for t in added)}"
        )
        if self.on_tags_changed:
            self.on_tags_changed(added, [])

    def _do_del(self, message, raw: str) -> None:
        uid = message.from_user.id
        removed = []
        for tag in normalize_hashtags(raw):
            if self.db.remove_user_tag(uid, tag):
                removed.append(tag)
        if not removed:
            self.bot.reply_to(
                message,
                "Такого тега нет.",
                reply_markup=self._main_keyboard(),
            )
            return
        self.bot.reply_to(
            message,
            "Удалено: " + ", ".join(display_hashtag(t) for t in removed),
            reply_markup=self._main_keyboard(),
        )
        self._log(
            f"TG /del · {uid} · {', '.join(display_hashtag(t) for t in removed)}"
        )
        if self.on_tags_changed:
            self.on_tags_changed([], removed)

    def _do_add_phrase(self, message, raw: str) -> None:
        from search_phrases import append_phrases, read_phrases

        added = append_phrases(raw)
        if not added:
            self.bot.reply_to(
                message,
                "Не разобрал фразу или она уже есть.\n"
                "Пример: <code>ищу сеошника</code>",
                parse_mode="HTML",
                reply_markup=self._main_keyboard(),
            )
            return
        preview = "\n".join(f"• {p}" for p in added[:15])
        more = f"\n…и ещё {len(added) - 15}" if len(added) > 15 else ""
        self.bot.reply_to(
            message,
            f"Добавлено ключ.слов: <b>{len(added)}</b> "
            f"(всего {len(read_phrases())}):\n{preview}{more}",
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )
        self._log(f"TG /addphrase · {message.from_user.id} · +{len(added)}")

    def _do_del_phrase(self, message, raw: str) -> None:
        from search_phrases import read_phrases, remove_phrases

        removed = remove_phrases(raw)
        if not removed:
            self.bot.reply_to(
                message,
                "Такой фразы нет. Смотри /phrases",
                reply_markup=self._main_keyboard(),
            )
            return
        preview = "\n".join(f"• {p}" for p in removed[:15])
        self.bot.reply_to(
            message,
            f"Удалено: <b>{len(removed)}</b> (осталось {len(read_phrases())}):\n"
            f"{preview}",
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )
        self._log(f"TG /delphrase · {message.from_user.id} · -{len(removed)}")

    def _list_phrases(self, message) -> None:
        from io import BytesIO

        from search_phrases import phrases_path, read_phrases

        phrases = read_phrases()
        if not phrases:
            self.bot.reply_to(
                message,
                "Ключевых слов пока нет. Нажми «➕ Ключ.слово» или /addphrase",
                reply_markup=self._main_keyboard(),
            )
            return
        if len(phrases) <= 35:
            body = "\n".join(f"• {p}" for p in phrases)
            self.bot.reply_to(
                message,
                f"Ключ.слова для поиска ({len(phrases)}):\n{body}",
                reply_markup=self._main_keyboard(),
            )
            return
        # длинный список — файлом
        raw = ("\n".join(phrases) + "\n").encode("utf-8")
        bio = BytesIO(raw)
        bio.name = "search_phrases.txt"
        self.bot.send_document(
            message.chat.id,
            bio,
            caption=f"Ключ.слова: {len(phrases)} шт. "
            f"(файл {phrases_path().name})",
            reply_markup=self._main_keyboard(),
        )

    def _do_add_group(self, message, raw: str) -> None:
        from tg_source import append_community_urls, merge_invites_into_settings

        added = append_community_urls(raw)
        if not added:
            self.bot.reply_to(
                message,
                "Не разобрал ссылку. Пример:\n"
                "<code>https://t.me/seo_chat</code>\n"
                "<code>https://t.me/+inviteHash</code>",
                parse_mode="HTML",
                reply_markup=self._main_keyboard(),
            )
            return
        merge_invites_into_settings(self.settings, added)
        lines = ", ".join(f"<code>{r}</code>" for r in added)
        self.bot.reply_to(
            message,
            f"Группы добавлены ({len(added)}):\n{lines}\n"
            "Слушатель переподключится и вступит сам (нужен вход «ВОЙТИ В TELEGRAM»).",
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )
        self._log(f"TG /addgroup · {message.from_user.id} · {', '.join(added)}")
        if self.on_groups_changed:
            self.on_groups_changed(added, [])

    def _do_del_group(self, message, raw: str) -> None:
        from tg_source import remove_community_urls

        removed = remove_community_urls(raw)
        if not removed:
            self.bot.reply_to(
                message,
                "Такой группы в списке нет. Смотри /groups",
                reply_markup=self._main_keyboard(),
            )
            return
        # пересобрать settings из файла + .env leftovers уже в settings —
        # просто убрать удалённые
        cur = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        drop = {r.casefold() for r in removed}
        self.settings.telegram_source_invites = tuple(
            x for x in cur if x.casefold() not in drop
        )
        lines = ", ".join(f"<code>{r}</code>" for r in removed)
        self.bot.reply_to(
            message,
            f"Убрано ({len(removed)}):\n{lines}",
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )
        self._log(f"TG /delgroup · {message.from_user.id} · {', '.join(removed)}")
        if self.on_groups_changed:
            self.on_groups_changed([], removed)

    def _list_groups(self, message) -> None:
        from tg_source import read_community_urls

        urls = read_community_urls()
        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        if not urls and not invites:
            self.bot.reply_to(
                message,
                "Групп пока нет. Нажми «➕ Группа TG» или /addgroup",
                reply_markup=self._main_keyboard(),
            )
            return
        lines = urls or [f"https://t.me/{x.lstrip('+')}" if not x.startswith("+") else f"https://t.me/{x}" for x in invites]
        # лимит сообщения TG
        chunk = lines[:40]
        body = "\n".join(f"• <code>{u}</code>" for u in chunk)
        more = f"\n…и ещё {len(lines) - 40}" if len(lines) > 40 else ""
        self.bot.reply_to(
            message,
            f"Группы-источники ({len(lines)}):\n{body}{more}",
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )

    def _source_chat_ids(self) -> set[int]:
        from tg_source import bound_chat_ids

        ids = set(getattr(self.settings, "telegram_source_chat_ids", ()) or ())
        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        ids |= bound_chat_ids(self.db, invites)
        return ids

    def _is_source_chat(self, chat_id: int) -> bool:
        from tg_source import chat_in_set

        return chat_in_set(int(chat_id), self._source_chat_ids())

    def _bind_source_chat(self, chat_id: int, title: str = "") -> None:
        from tg_source import bind_chat, unbound_invites

        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        free = unbound_invites(self.db, invites)
        # Явный chat_id из .env — ничего биндить не нужно
        if int(chat_id) in set(getattr(self.settings, "telegram_source_chat_ids", ()) or ()):
            self._log(f"TG-источник уже в .env · {title or chat_id}")
            return
        if not free:
            # Если инвайт один и уже был — перезапишем на актуальный chat_id
            if len(invites) == 1:
                free = list(invites)
            else:
                return
        bind_chat(self.db, free[0], int(chat_id), title=title)
        self._log(
            f"TG-источник привязан · {title or chat_id} · invite +{free[0]}"
        )

    def _handle_source_message(self, message) -> None:
        chat = message.chat
        if not chat or getattr(chat, "type", "") not in {
            "group",
            "supergroup",
            "channel",
        }:
            return

        # Если бот уже в группе, а chat_id ещё не записан — привяжем по первому посту
        if not self._is_source_chat(int(chat.id)):
            from tg_source import unbound_invites

            invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
            free = unbound_invites(self.db, invites)
            configured = set(getattr(self.settings, "telegram_source_chat_ids", ()) or ())
            if len(invites) == 1 and len(free) == 1 and not configured:
                title = getattr(chat, "title", None) or str(chat.id)
                self._bind_source_chat(int(chat.id), title=title)
            if not self._is_source_chat(int(chat.id)):
                return

        preview = (getattr(message, "text", None) or getattr(message, "caption", None) or "")[:80]
        self._log(
            f"TG-группа увидела пост · {getattr(chat, 'title', None) or chat.id} · {preview}"
        )

        def job() -> None:
            try:
                from tg_source import evaluate_and_deliver

                ok, why = evaluate_and_deliver(
                    self.settings,
                    self.db,
                    message,
                    require_russian=bool(
                        getattr(self.settings, "require_russian", True)
                    ),
                )
                if ok:
                    self._log(f"TG-группа → лид · {why}")
                else:
                    # Не спамим лог на каждый оффтоп
                    if why.startswith("DeepSeek") or why in {
                        "не RU",
                        "пусто",
                        "некому слать",
                    }:
                        self._log(f"TG-группа skip · {why}")
            except Exception as exc:
                self._log(f"TG-группа ошибка: {redact_secrets(exc)}")

        threading.Thread(
            target=job, name=f"tg-src-{getattr(message, 'message_id', 0)}", daemon=True
        ).start()

    def _mentions_me(self, message) -> bool:
        uname = (self._bot_username or "").casefold()
        if not uname:
            return False
        text = (
            getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        ).casefold()
        if f"@{uname}" in text:
            return True
        for ent in list(getattr(message, "entities", None) or []) + list(
            getattr(message, "caption_entities", None) or []
        ):
            et = getattr(ent, "type", "") or ""
            if et in {"mention", "text_mention"}:
                return True
        return False

    def _want_source_message(self, message) -> bool:
        if not message or not message.chat:
            return False
        if getattr(message.chat, "type", "") not in {"group", "supergroup", "channel"}:
            return False
        if self._is_source_chat(int(message.chat.id)):
            return True
        from tg_source import unbound_invites

        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        free = unbound_invites(self.db, invites)
        configured = set(getattr(self.settings, "telegram_source_chat_ids", ()) or ())
        if len(invites) == 1 and len(free) == 1 and not configured:
            return True
        # Group Privacy: бот видит только упоминания / ответы себе
        return bool(invites) and self._mentions_me(message)

    def _register_handlers(self) -> None:
        @self.bot.my_chat_member_handler(func=lambda u: True)
        def on_my_chat_member(update) -> None:
            try:
                chat = update.chat
                new = update.new_chat_member
                status = getattr(new, "status", "") or ""
                if status not in {"member", "administrator"}:
                    return
                if getattr(chat, "type", "") not in {"group", "supergroup", "channel"}:
                    return
                title = getattr(chat, "title", None) or str(chat.id)
                self._bind_source_chat(int(chat.id), title=title)
            except Exception as exc:
                self._log(f"my_chat_member: {redact_secrets(exc)}")

        @self.bot.message_handler(
            func=lambda m: self._want_source_message(m),
            content_types=["text", "photo", "video", "document", "animation"],
        )
        def source_group_message(message) -> None:
            self._handle_source_message(message)

        @self.bot.channel_post_handler(
            func=lambda m: self._want_source_message(m),
            content_types=["text", "photo", "video", "document", "animation"],
        )
        def source_channel_post(message) -> None:
            self._handle_source_message(message)

        @self.bot.callback_query_handler(func=lambda c: True)
        def on_callback(call) -> None:
            uid = int(getattr(call.from_user, "id", 0) or 0)
            if not uid or not self._is_allowed(uid):
                try:
                    self.bot.answer_callback_query(call.id, "Нет доступа")
                except Exception:
                    pass
                return
            data = (call.data or "").strip()
            if len(data) < 3 or data[1] != ":":
                try:
                    self.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                return
            action, raw_id = data[0], data[2:]
            try:
                lead_id = int(raw_id)
            except ValueError:
                self.bot.answer_callback_query(call.id, "битая кнопка")
                return
            lead = self.db.get_lead(lead_id)
            if not lead:
                self.bot.answer_callback_query(call.id, "пост не найден")
                return
            lead_url = (lead.get("url") or "").strip()
            if lead_url.startswith("https://t.me/") or str(
                lead.get("hashtag") or ""
            ).casefold() in {"telegram", "#telegram"}:
                self.bot.answer_callback_query(
                    call.id,
                    "Это лид из Telegram — пиши автору в ЛС, не в Threads",
                    show_alert=True,
                )
                return
            chat_id = call.message.chat.id if call.message else uid

            if action in {"c", "r"}:
                self.bot.answer_callback_query(call.id, "DeepSeek пишет комментарий…")
                self._log(f"TG comment gen · {uid} · lead {lead_id}")

                def gen_job() -> None:
                    try:
                        draft = self._make_draft(lead_id)
                        if not draft:
                            self.bot.send_message(
                                chat_id,
                                "DeepSeek не дал текст. Нажми «Свой текст» и напиши комментарий руками.",
                            )
                            return
                        self._show_draft(chat_id, lead_id, draft)
                    except Exception as exc:
                        try:
                            self.bot.send_message(
                                chat_id, f"Черновик не собрался: {exc}"
                            )
                        except Exception:
                            pass

                threading.Thread(
                    target=gen_job, daemon=True, name="tg-draft"
                ).start()
                return
            if action == "e":
                self._pending[uid] = f"comment:{lead_id}"
                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    chat_id,
                    "Пришли текстом свой комментарий одним сообщением.",
                )
                return
            if action == "s":
                draft = (lead.get("draft") or "").strip()
                if not draft:
                    self.bot.answer_callback_query(
                        call.id, "Сначала сгенерируй текст"
                    )
                    return
                busy = getattr(self, "_comment_busy", None)
                if busy is None:
                    self._comment_busy = set()
                    busy = self._comment_busy
                if lead_id in busy:
                    self.bot.answer_callback_query(
                        call.id, "Уже отправляю этот комментарий"
                    )
                    return
                busy.add(lead_id)
                self.bot.answer_callback_query(call.id, "Отправляю в Threads…")
                try:
                    self.bot.send_message(
                        chat_id,
                        "Открываю Threads (окно Chrome) и публикую комментарий.\n"
                        "Не трогай это окно ~30–60 секунд — сюда придёт ✅ или ошибка.",
                    )
                except Exception:
                    pass
                msg_id = call.message.message_id if call.message else None

                def send_job() -> None:
                    try:
                        from commenter import post_comment

                        err = post_comment(
                            lead.get("url") or "", draft, self.settings
                        )
                        if err:
                            self.db.set_lead_draft(
                                lead_id, draft, status="failed"
                            )
                            self.bot.send_message(
                                chat_id, f"Не отправилось: {err}"
                            )
                            self._log(f"TG comment fail · {uid} · {err}")
                            return
                        self.db.set_lead_draft(lead_id, draft, status="sent")
                        if msg_id:
                            try:
                                self.bot.edit_message_reply_markup(
                                    chat_id=chat_id,
                                    message_id=msg_id,
                                    reply_markup=None,
                                )
                            except Exception:
                                pass
                        from models import escape_html

                        self.bot.send_message(
                            chat_id,
                            "✅ Комментарий опубликован в Threads:\n\n"
                            f"<i>{escape_html(draft)}</i>",
                            parse_mode="HTML",
                        )
                        self._log(f"TG comment sent · {uid} · lead {lead_id}")
                    except Exception as exc:
                        try:
                            self.bot.send_message(
                                chat_id, f"Не отправилось: {exc}"
                            )
                        except Exception:
                            pass
                    finally:
                        busy.discard(lead_id)

                threading.Thread(
                    target=send_job, daemon=True, name="tg-send-comment"
                ).start()
                return
            self.bot.answer_callback_query(call.id)

        def _private(m) -> bool:
            return bool(m and m.chat and getattr(m.chat, "type", "") == "private")

        @self.bot.message_handler(commands=["start", "help"], func=_private)
        def cmd_start(message) -> None:
            if self._deny(message):
                return
            uid = message.from_user.id
            self.db.upsert_user(
                uid, message.chat.id, message.from_user.username or "", active=True
            )
            self._pending.pop(uid, None)
            name = message.from_user.first_name or message.from_user.username or "друг"
            safe_name = (
                str(name)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            text = (
                f"👋 Привет, <b>{safe_name}</b>!\n"
                f"Это <b>Threads Posts ARTFrance</b>.\n"
                f"Твой ID: <code>{uid}</code>\n\n"
                f"{HELP_TEXT}"
            )
            self.bot.reply_to(
                message, text, parse_mode="HTML", reply_markup=self._main_keyboard()
            )
            self._log(f"TG /start · user {uid}")

        @self.bot.message_handler(commands=["tags"], func=_private)
        def cmd_tags(message) -> None:
            if self._deny(message):
                return
            uid = message.from_user.id
            self.db.upsert_user(
                uid, message.chat.id, message.from_user.username or "", active=True
            )
            self._pending.pop(uid, None)
            tags = self.db.get_user_tags(uid)
            if not tags:
                self.bot.reply_to(
                    message,
                    "Тегов пока нет. Нажми «Добавить тег».",
                    reply_markup=self._main_keyboard(),
                )
                return
            lines = ", ".join(display_hashtag(t) for t in tags)
            self.bot.reply_to(
                message,
                f"Твои теги:\n{lines}",
                reply_markup=self._main_keyboard(),
            )

        @self.bot.message_handler(
            commands=["phrases", "phrase", "keywords", "keys"], func=_private
        )
        def cmd_phrases(message) -> None:
            if self._deny(message):
                return
            self._pending.pop(message.from_user.id, None)
            self._list_phrases(message)

        @self.bot.message_handler(
            commands=["addphrase", "add_phrase", "addkey"], func=_private
        )
        def cmd_addphrase(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_add_phrase(message, parts[1])
                return
            self._ask_keyword(message, "addphrase")

        @self.bot.message_handler(
            commands=["delphrase", "rmphrase", "delkey"], func=_private
        )
        def cmd_delphrase(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_del_phrase(message, parts[1])
                return
            self._ask_keyword(message, "delphrase")

        @self.bot.message_handler(commands=["add"], func=_private)
        def cmd_add(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_add(message, parts[1])
                return
            self._ask_keyword(message, "add")

        @self.bot.message_handler(commands=["del", "remove", "rm"], func=_private)
        def cmd_del(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_del(message, parts[1])
                return
            self._ask_keyword(message, "del")

        @self.bot.message_handler(commands=["addgroup", "add_group"], func=_private)
        def cmd_addgroup(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_add_group(message, parts[1])
                return
            self._ask_keyword(message, "addgroup")

        @self.bot.message_handler(
            commands=["delgroup", "rmgroup", "removegroup"], func=_private
        )
        def cmd_delgroup(message) -> None:
            if self._deny(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) >= 2:
                self._pending.pop(message.from_user.id, None)
                self._do_del_group(message, parts[1])
                return
            self._ask_keyword(message, "delgroup")

        @self.bot.message_handler(commands=["groups", "grouplist"], func=_private)
        def cmd_groups(message) -> None:
            if self._deny(message):
                return
            self._pending.pop(message.from_user.id, None)
            self._list_groups(message)

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_ADD
        )
        def btn_add(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "add")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_DEL
        )
        def btn_del(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "del")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_TAGS
        )
        def btn_tags(message) -> None:
            if self._deny(message):
                return
            cmd_tags(message)

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_PHRASES
        )
        def btn_phrases(message) -> None:
            if self._deny(message):
                return
            self._pending.pop(message.from_user.id, None)
            self._list_phrases(message)

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_ADD_PHRASE
        )
        def btn_add_phrase(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "addphrase")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_DEL_PHRASE
        )
        def btn_del_phrase(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "delphrase")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_ADD_GROUP
        )
        def btn_add_group(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "addgroup")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_DEL_GROUP
        )
        def btn_del_group(message) -> None:
            if self._deny(message):
                return
            self._ask_keyword(message, "delgroup")

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_GROUPS
        )
        def btn_groups(message) -> None:
            if self._deny(message):
                return
            self._list_groups(message)

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_HELP
        )
        def btn_help(message) -> None:
            if self._deny(message):
                return
            cmd_start(message)

        @self.bot.message_handler(
            func=lambda m: _private(m) and (m.text or "") == BTN_CANCEL
        )
        def btn_cancel(message) -> None:
            if self._deny(message):
                return
            self._pending.pop(message.from_user.id, None)
            self.bot.reply_to(
                message, "Ок, отменил.", reply_markup=self._main_keyboard()
            )

        @self.bot.message_handler(
            func=lambda m: _private(m)
            and m.from_user
            and m.from_user.id in self._pending
        )
        def pending_keyword(message) -> None:
            if self._deny(message):
                self._pending.pop(message.from_user.id, None)
                return
            uid = message.from_user.id
            action = self._pending.pop(uid, None)
            raw = (message.text or "").strip()
            if action and str(action).startswith("comment:"):
                try:
                    lead_id = int(str(action).split(":", 1)[1])
                except ValueError:
                    self.bot.reply_to(message, "Не понял черновик.")
                    return
                if not raw or raw.startswith("/"):
                    self.bot.reply_to(message, "Пришли обычный текст комментария.")
                    return
                self.db.set_lead_draft(lead_id, raw, status="draft")
                self._show_draft(message.chat.id, lead_id, raw)
                return
            if action in {"addgroup", "delgroup"}:
                if not raw or raw.startswith("/"):
                    self.bot.reply_to(
                        message,
                        "Нужна ссылка на группу, например https://t.me/seo_chat",
                        reply_markup=self._main_keyboard(),
                    )
                    return
                if action == "addgroup":
                    self._do_add_group(message, raw)
                else:
                    self._do_del_group(message, raw)
                return
            if action in {"addphrase", "delphrase"}:
                if not raw or raw.startswith("/"):
                    self.bot.reply_to(
                        message,
                        "Нужна фраза, например: ищу сеошника",
                        reply_markup=self._main_keyboard(),
                    )
                    return
                if action == "addphrase":
                    self._do_add_phrase(message, raw)
                else:
                    self._do_del_phrase(message, raw)
                return
            if not raw or raw.startswith("/"):
                self.bot.reply_to(
                    message,
                    "Нужно просто слово, например: SEO",
                    reply_markup=self._main_keyboard(),
                )
                return
            if action == "add":
                self._do_add(message, raw)
            elif action == "del":
                self._do_del(message, raw)
            else:
                self.bot.reply_to(
                    message, "Ок.", reply_markup=self._main_keyboard()
                )

        # Любое другое сообщение — только в личке
        @self.bot.message_handler(
            func=lambda m: bool(
                m
                and m.chat
                and getattr(m.chat, "type", "") == "private"
            ),
            content_types=["text"],
        )
        def any_text(message) -> None:
            if self._deny(message):
                return
            raw = (message.text or "").strip()
            # Fallback: /phrases и синонимы, если command-handler не сработал
            low = raw.casefold().split("@", 1)[0].strip()
            if low in {
                "/phrases",
                "/phrase",
                "/keywords",
                "/keys",
                "/addphrase",
                "/delphrase",
            } or low.startswith(
                ("/phrases ", "/phrase ", "/keywords ", "/keys ", "/addphrase ", "/delphrase ")
            ):
                if low.startswith(("/addphrase", "/add_phrase", "/addkey")):
                    parts = raw.split(maxsplit=1)
                    if len(parts) >= 2:
                        self._do_add_phrase(message, parts[1])
                    else:
                        self._ask_keyword(message, "addphrase")
                    return
                if low.startswith(("/delphrase", "/rmphrase", "/delkey")):
                    parts = raw.split(maxsplit=1)
                    if len(parts) >= 2:
                        self._do_del_phrase(message, parts[1])
                    else:
                        self._ask_keyword(message, "delphrase")
                    return
                self._list_phrases(message)
                return
            self.bot.reply_to(
                message,
                "Используй меню внизу или /help",
                reply_markup=self._main_keyboard(),
            )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        from instance_lock import acquire_bot_poll_lock

        if not acquire_bot_poll_lock():
            self._log(
                "Бот уже слушает обновления в этом ПК — второй polling не стартую"
            )
            return
        self._setup_menu_commands()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="tg-control-bot", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.bot.stop_polling()
        except Exception:
            pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=12)
        self._thread = None

    def _run(self) -> None:
        # Меню /add /del. Лиды и группы от getUpdates не зависят.
        allowed = [
            "message",
            "edited_message",
            "channel_post",
            "callback_query",
            "my_chat_member",
        ]
        try:
            self.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        try:
            info = self.bot.get_webhook_info()
            url = (getattr(info, "url", None) or "").strip()
            if url:
                self._log("Бот: был webhook — сбросил, дальше polling")
                self.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        self._stop.wait(2)
        conflicts = 0
        paused = False
        announced = False
        while not self._stop.is_set():
            try:
                updates = self.bot.get_updates(
                    offset=self.bot.last_update_id + 1,
                    timeout=15,
                    long_polling_timeout=5,
                    allowed_updates=allowed,
                )
                if not announced or paused or conflicts:
                    self._log("Меню бота слушает /add /del")
                    announced = True
                conflicts = 0
                paused = False
                if updates:
                    self.bot.process_new_updates(updates)
            except ApiTelegramException as exc:
                desc = str(getattr(exc, "description", "") or exc)
                if "409" in str(exc) or "Conflict" in desc:
                    conflicts += 1
                    if conflicts >= 3:
                        if not paused:
                            self._log(
                                "Меню бота в Telegram занято другим сеансом "
                                "(другой ПК или старый запуск). "
                                "Парсер, группы и лиды работают. Теги ставь в окне. "
                                "Проверю слот снова через 5 мин"
                            )
                            paused = True
                        self._stop.wait(300)
                        continue
                    self._stop.wait(25)
                    continue
                conflicts = 0
                self._log(f"TG bot API: {redact_secrets(exc)}")
                self._stop.wait(15)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.debug("TG bot сеть: %s", redact_secrets(exc))
                self._stop.wait(20)
