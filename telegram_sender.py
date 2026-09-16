"""Отправка постов в Telegram (с ретраями при сетевых сбоях)."""

from __future__ import annotations

import re
import time
from io import BytesIO

import httpx
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from config import Settings
from filters import scrub_post_text
from hashtags import clean_post_text
from models import ThreadPost, escape_html, logger, truncate

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def lead_keyboard(lead_id: int) -> types.InlineKeyboardMarkup:
    """Кнопка комментария только для лидов из Threads."""
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "✍️ Комментарий",
            callback_data=f"c:{int(lead_id)}",
        )
    )
    return kb


def tg_lead_keyboard(post) -> types.InlineKeyboardMarkup | None:
    """Для лидов из TG-группы: ссылка в ЛС автору / на пост (без Threads)."""
    kb = types.InlineKeyboardMarkup()
    uname = (getattr(post, "username", None) or "").strip().lstrip("@")
    author_id = getattr(post, "author_tg_id", None)
    url = (getattr(post, "url", None) or "").strip()
    if uname and not uname.lower().startswith("id"):
        kb.add(
            types.InlineKeyboardButton(
                "✉️ Написать в ЛС",
                url=f"https://t.me/{uname}",
            )
        )
    elif author_id:
        kb.add(
            types.InlineKeyboardButton(
                "👤 Профиль",
                url=f"tg://user?id={int(author_id)}",
            )
        )
    if url.startswith("https://t.me/"):
        kb.add(
            types.InlineKeyboardButton("Открыть пост", url=url)
        )
    return kb if kb.keyboard else None


def draft_keyboard(lead_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "✅ Отправить в Threads",
            callback_data=f"s:{int(lead_id)}",
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            "🔄 Другой текст",
            callback_data=f"r:{int(lead_id)}",
        ),
        types.InlineKeyboardButton(
            "✏️ Свой текст",
            callback_data=f"e:{int(lead_id)}",
        ),
    )
    return kb

# Дольше ждём Telegram — у многих ConnectTimeout на 15с
telebot.apihelper.CONNECT_TIMEOUT = 60
telebot.apihelper.READ_TIMEOUT = 60


def _is_network_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "connecttimeout",
        "readtimeout",
        "timed out",
        "timeout",
        "connection aborted",
        "connection reset",
        "max retries exceeded",
        "temporarily unavailable",
        "name or service not known",
        "failed to establish",
        "network is unreachable",
    )
    return any(m in text for m in markers)


class TelegramSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.chat_id = settings.telegram_chat_id
        proxy = getattr(settings, "telegram_proxy", None) or None
        # MTProto (tg-ws-proxy) — только для Telethon user-API, не для Bot API / httpx
        if proxy and str(proxy).strip().lower().startswith("mtproto://"):
            proxy = None
        if proxy:
            telebot.apihelper.proxy = {"https": proxy, "http": proxy}
            logger.info("Telegram proxy: %s", proxy)
        self.bot = telebot.TeleBot(settings.telegram_bot_token, parse_mode="HTML")

    def _build_body(self, post: ThreadPost, limit: int) -> str:
        tag = (post.hashtag or "").lstrip("#")
        is_tg = bool(getattr(post, "is_telegram", False)) or (
            (post.url or "").startswith("https://t.me/")
            or tag.casefold() == "telegram"
        )
        if is_tg:
            author = escape_html(post.username)
            author_href = escape_html(getattr(post, "author_url", None) or post.url)
            footer = (
                f'\n\n<a href="{author_href}">@{author}</a>'
                f' · <a href="{escape_html(post.url)}">пост в Telegram</a>'
                f"\n#telegram · напиши в ЛС"
            )
        else:
            footer = (
                f'\n\n<a href="{escape_html(post.author_url)}">@{escape_html(post.username)}</a>'
                f' · <a href="{escape_html(post.url)}">открыть в Threads</a>'
                f"\n#{escape_html(tag)}"
            )
        reserve = len(footer)
        body = scrub_post_text(post.text or "") or clean_post_text(post.text or "")
        text = escape_html(truncate(body, max(0, limit - reserve - 5)))
        if text:
            return f"{text}{footer}"
        return footer.strip()

    def _download_image(self, url: str) -> bytes | None:
        try:
            headers = {
                "User-Agent": self.settings.user_agent,
                "Referer": "https://www.threads.net/",
            }
            with httpx.Client(timeout=45.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:
            logger.error("Не удалось скачать изображение: %s", exc)
            return None

    def _call_with_retry(self, label: str, fn, *, attempts: int = 5):
        last: BaseException | None = None
        for i in range(1, attempts + 1):
            try:
                return fn()
            except ApiTelegramException as exc:
                err = str(exc).lower()
                # Flood / retry_after
                retry_after = getattr(exc, "result_json", None) or {}
                wait = 0
                if isinstance(retry_after, dict):
                    wait = int(retry_after.get("parameters", {}).get("retry_after") or 0)
                if not wait:
                    m = re.search(r"retry.?after[\"\s:]+(\d+)", str(exc), re.I)
                    if m:
                        wait = int(m.group(1))
                if wait or "too many requests" in err or "flood" in err:
                    last = exc
                    sleep_for = max(wait, 2 * i) + 0.5
                    logger.warning(
                        "Telegram %s flood, жду %.0fс (%s/%s)",
                        label,
                        sleep_for,
                        i,
                        attempts,
                    )
                    time.sleep(sleep_for)
                    continue
                if _is_network_error(exc) and i < attempts:
                    last = exc
                    time.sleep(2 * i)
                    continue
                raise
            except Exception as exc:
                last = exc
                if _is_network_error(exc) and i < attempts:
                    time.sleep(2 * i)
                    continue
                raise
        assert last is not None
        raise last

    def verify_access(self) -> str:
        """Проверяет токен бота. Рассылка идёт в ЛС, канал не обязателен."""
        try:
            me = self._call_with_retry("getMe", lambda: self.bot.get_me())
        except Exception as exc:
            if _is_network_error(exc):
                raise RuntimeError(
                    "Нет связи с api.telegram.org (таймаут/сеть). "
                    "Проверь интернет, VPN или firewall."
                ) from exc
            raise RuntimeError(f"Telegram API: {exc}") from exc

        username = getattr(me, "username", None) or "bot"
        return f"@{username}"

    def verify_channel(self) -> str:
        """Опциональная проверка канала (если снова понадобится)."""
        try:
            chat = self._call_with_retry(
                "getChat", lambda: self.bot.get_chat(self.chat_id)
            )
        except Exception as exc:
            if _is_network_error(exc):
                raise RuntimeError(
                    "Нет связи с api.telegram.org при проверке канала."
                ) from exc
            raise RuntimeError(f"Telegram API: {exc}") from exc

        title = (
            getattr(chat, "title", None)
            or getattr(chat, "username", None)
            or str(self.chat_id)
        )
        return title

    def resolve_alert_targets(self, db=None) -> list[int]:
        """ALERT_TG_ID + ALERT_TG_EXTRA (@username / numeric id)."""
        targets: list[int] = []
        seen: set[int] = set()

        def _add(cid: int) -> None:
            try:
                n = int(cid)
            except (TypeError, ValueError):
                return
            if n and n not in seen:
                seen.add(n)
                targets.append(n)

        _add(int(getattr(self.settings, "alert_tg_id", 0) or 0))
        extras = getattr(self.settings, "alert_tg_extra", ()) or ()
        for raw in extras:
            item = str(raw or "").strip().lstrip("@")
            if not item:
                continue
            if item.isdigit() or (item.startswith("-") and item[1:].isdigit()):
                _add(int(item))
                continue
            # Сначала локальная база (кто уже писал боту)
            if db is not None:
                try:
                    row = db.find_user_by_username(item)
                except Exception:
                    row = None
                if row and row.get("chat_id"):
                    _add(int(row["chat_id"]))
                    continue
            try:
                chat = self._call_with_retry(
                    "getChat", lambda u=item: self.bot.get_chat(f"@{u}")
                )
                cid = int(getattr(chat, "id", 0) or 0)
                if cid:
                    _add(cid)
            except Exception as exc:
                logger.warning(
                    "Не резолвится @%s для алертов: %s "
                    "(пусть человек напишет боту /start)",
                    item,
                    exc,
                )
        return targets

    def send_alert(self, text: str, *, db=None) -> bool:
        """Служебное уведомление админу + ALERT_TG_EXTRA."""
        body = (text or "").strip()
        if not body:
            return False
        if len(body) > MESSAGE_LIMIT:
            body = body[: MESSAGE_LIMIT - 20] + "…"

        targets = self.resolve_alert_targets(db)
        if not targets:
            return False

        ok_any = False
        for target in targets:

            def _msg(chat_id: int = target) -> None:
                self.bot.send_message(
                    chat_id=chat_id,
                    text=body,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            try:
                self._call_with_retry("sendAlert", _msg, attempts=2)
                ok_any = True
            except Exception as exc:
                logger.error(
                    "Не удалось отправить алерт %s: %s", target, exc
                )
        return ok_any

    def send_broadcast(
        self,
        text: str,
        chat_ids: list[int],
        *,
        label: str = "broadcast",
    ) -> int:
        """Рассылка текста списку chat_id. Возвращает число успешных."""
        body = (text or "").strip()
        if not body or not chat_ids:
            return 0
        if len(body) > MESSAGE_LIMIT:
            body = body[: MESSAGE_LIMIT - 20] + "…"
        ok_n = 0
        seen: set[int] = set()
        for raw_id in chat_ids:
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not cid or cid in seen:
                continue
            seen.add(cid)

            def _msg(chat_id: int = cid) -> None:
                self.bot.send_message(
                    chat_id=chat_id,
                    text=body,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            try:
                self._call_with_retry(label, _msg, attempts=2)
                ok_n += 1
                time.sleep(0.35)
            except Exception as exc:
                logger.error("%s → %s: %s", label, cid, exc)
        return ok_n

    def send_post(
        self,
        post: ThreadPost,
        chat_id: str | int | None = None,
        reply_markup=None,
    ) -> bool:
        target = chat_id if chat_id is not None else self.chat_id
        # Скорость/надёжность: текст первым. Картинки часто валят отправку.
        text = self._build_body(post, MESSAGE_LIMIT)

        def _msg_html() -> None:
            self.bot.send_message(
                chat_id=target,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

        def _msg_plain() -> None:
            plain = scrub_post_text(post.text or "") or (post.text or "")
            plain = truncate(plain, 3500)
            footer = (
                f"\n\n@{post.username} · {post.url}\n"
                f"#{(post.hashtag or '').lstrip('#')}"
            )
            self.bot.send_message(
                chat_id=target,
                text=plain + footer,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

        try:
            self._call_with_retry("sendMessage", _msg_html)
            return True
        except ApiTelegramException as exc:
            err = str(exc).lower()
            logger.error("Telegram HTML fail: %s", exc)
            if (
                "can't parse" in err
                or "parse entities" in err
                or "bad request" in err
            ):
                try:
                    self._call_with_retry(
                        "sendMessagePlain", _msg_plain, attempts=3
                    )
                    return True
                except Exception as exc2:
                    logger.error("Telegram plain fail: %s", exc2)
                    return False
            return False
        except Exception as exc:
            if _is_network_error(exc):
                logger.error(
                    "Сеть Telegram недоступна при отправке: %s", exc
                )
            else:
                logger.error("Ошибка отправки в Telegram: %s", exc)
            try:
                self._call_with_retry(
                    "sendMessagePlain", _msg_plain, attempts=2
                )
                return True
            except Exception:
                return False
