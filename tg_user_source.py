"""Telegram-группы через MTProto (Telethon): бот в группу не нужен."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

import tg_ssl  # noqa: F401  — до telethon

from telethon import events
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, User, ChatInviteAlready
from telethon.utils import get_peer_id

from config import Settings
from database import PostDatabase
from models import logger, redact_secrets
from tg_source import (
    META_PREFIX,
    bind_chat,
    bound_chat_ids,
    chat_id_aliases,
    chat_in_set,
    evaluate_and_deliver_post,
    invite_hash,
    is_invite_ref,
    public_username,
    source_ref,
    telethon_message_to_post,
    unbound_invites,
)


class JoinRateLimited(Exception):
    """Telegram FloodWait на join — не спать минутами, отложить остальные вступления."""

    def __init__(self, seconds: int) -> None:
        self.seconds = int(seconds or 0)
        super().__init__(f"FloodWait {self.seconds}s")


async def _dialog_by_title(client: TelegramClient, title: str) -> tuple[int, str] | None:
    want = (title or "").strip().casefold()
    if not want:
        return None
    async for dialog in client.iter_dialogs():
        name = (getattr(dialog, "name", None) or getattr(dialog, "title", None) or "").strip()
        if name.casefold() != want:
            continue
        entity = dialog.entity
        if isinstance(entity, (Channel, Chat)):
            return int(get_peer_id(entity)), name or title
    return None


def _parse_proxy(raw: str | None):
    """SOCKS/HTTP для Telethon. MTProto — через parse_proxy_spec / _client."""
    spec = parse_proxy_spec(raw)
    if not spec or spec.kind != "socks":
        return None
    return spec.socks


def parse_proxy_spec(raw: str | None):
    """Разобрать TELEGRAM_PROXY: socks5/http или mtproto://host:port/secret."""
    from dataclasses import dataclass

    @dataclass
    class _Spec:
        kind: str  # socks | mtproto
        socks: tuple | None = None
        mt_host: str = ""
        mt_port: int = 1443
        mt_secret: str = ""

    if not raw:
        return None
    text = raw.strip()
    # mtproto://127.0.0.1:1443/ee...  или  mtproto://127.0.0.1:1443:ee...
    if text.casefold().startswith("mtproto://"):
        rest = text[len("mtproto://") :]
        host = "127.0.0.1"
        port = 1443
        secret = ""
        if "/" in rest:
            left, secret = rest.split("/", 1)
        elif rest.count(":") >= 2:
            # host:port:secret (secret may contain :)
            host, port_s, secret = rest.split(":", 2)
            left = f"{host}:{port_s}"
        else:
            left = rest
        if ":" in left:
            host, port_s = left.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                port = 1443
        else:
            host = left or "127.0.0.1"
        secret = (secret or "").strip().lstrip("?")
        if secret.startswith("secret="):
            secret = secret[7:]
        if not secret:
            return None
        return _Spec(kind="mtproto", mt_host=host or "127.0.0.1", mt_port=port, mt_secret=secret)

    url = urlparse(text)
    if url.scheme not in {"socks5", "socks4", "http", "https"}:
        return None
    try:
        import socks
    except ImportError:
        return None
    host = url.hostname
    port = url.port or (1080 if "socks" in url.scheme else 8080)
    if not host:
        return None
    if url.scheme == "socks5":
        ptype = socks.SOCKS5
    elif url.scheme == "socks4":
        ptype = socks.SOCKS4
    else:
        ptype = socks.HTTP
    return _Spec(
        kind="socks",
        socks=(ptype, host, port, True, url.username, url.password),
    )


def load_tg_ws_proxy_secret() -> str | None:
    """Secret из %APPDATA%/TgWsProxy или tools/tg_ws_proxy.secret."""
    import json
    import os
    import re
    from pathlib import Path

    # Наш CLI-секрет (если поднимали exe с --secret)
    local_secret = Path(__file__).resolve().parent / "tools" / "tg_ws_proxy.secret"
    if local_secret.is_file():
        try:
            val = local_secret.read_text(encoding="ascii", errors="ignore").strip()
            if len(val) >= 16:
                return val
        except OSError:
            pass

    env_secret = (os.getenv("TG_WS_PROXY_SECRET") or "").strip()
    if len(env_secret) >= 16:
        return env_secret

    base = Path(os.environ.get("APPDATA", "")) / "TgWsProxy"
    if not base.is_dir():
        alt = Path(__file__).resolve().parent / "tools" / "TgWsProxy_data"
        if alt.is_dir():
            base = alt
        else:
            return None

    key_names = (
        "secret",
        "Secret",
        "mtproto_secret",
        "proxy_secret",
        "ee_secret",
    )

    def _from_obj(data: object) -> str | None:
        if isinstance(data, dict):
            for k in key_names:
                v = data.get(k)
                if isinstance(v, str) and len(v.strip()) >= 16:
                    return v.strip()
            for v in data.values():
                found = _from_obj(v)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = _from_obj(item)
                if found:
                    return found
        return None

    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".json", ".txt", ".cfg", ".ini", ".conf", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
            try:
                found = _from_obj(json.loads(text))
                if found:
                    return found
            except json.JSONDecodeError:
                pass
        m = re.search(
            r"(?i)(?:secret|mtproto)[=:\s]+([0-9a-f]{32,}|ee[0-9a-f]{32,}|dd[0-9a-f]{32,})",
            text,
        )
        if m:
            return m.group(1).strip()
    return None


def auto_mtproto_proxy_url() -> str | None:
    """Если tg-ws-proxy слушает :1443 — собрать mtproto:// URL."""
    import os
    import socket

    host, port = "127.0.0.1", 1443
    try:
        with socket.create_connection((host, port), timeout=0.4):
            pass
    except OSError:
        return None
    secret = load_tg_ws_proxy_secret()
    if not secret:
        return None
    # Flowseal / Telegram Desktop используют префикс dd
    if not secret.startswith(("dd", "ee", "DD", "EE")):
        secret = f"dd{secret}"
    return f"mtproto://{host}:{port}/{secret}"


async def ensure_joined(
    client: TelegramClient,
    db: PostDatabase,
    invite: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> int | None:
    """Вступить по +invite / @username и сохранить chat_id."""
    raw = (invite or "").strip()
    ref = source_ref(raw) or raw
    if not ref:
        return None

    # Уже привязан в БД — не долбим Telegram повторно
    for variant in {ref, ref.casefold(), ref.lstrip("+"), f"+{ref.lstrip('+')}"}:
        raw_id = db.get_meta(f"{META_PREFIX}{variant}")
        if not raw_id:
            continue
        try:
            return int(raw_id)
        except ValueError:
            break

    if not client.is_connected():
        raise ConnectionError("disconnected during join")

    # ── публичный username ──
    if not is_invite_ref(ref) and not ref.startswith("+"):
        uname = public_username(ref) or public_username("@" + ref) or ref.lstrip("@")
        if not uname:
            return None
        try:
            ent = await client.get_entity(uname)
        except Exception as exc:
            if "disconnected" in str(exc).casefold():
                raise ConnectionError("disconnected during join") from exc
            if on_log:
                on_log(f"не открыл @{uname}: {redact_secrets(exc)}")
            return None
        try:
            await client(JoinChannelRequest(ent))
        except UserAlreadyParticipantError:
            pass
        except FloodWaitError as exc:
            # Долгий sleep на FloodWait 996с рвёт прокси и убивает сессию.
            # Пропускаем чат — доберём позже по одному из poll.
            if on_log:
                on_log(
                    f"FloodWait {exc.seconds}с на @{uname} — "
                    f"пропускаю, доберу позже (не жду {exc.seconds}с)"
                )
            raise JoinRateLimited(int(exc.seconds))
        except Exception as exc:
            if "disconnected" in str(exc).casefold():
                raise ConnectionError("disconnected during join") from exc
            # Уже участник / приват без прав join — всё равно пробуем peer id
            if on_log:
                on_log(f"join @{uname}: {redact_secrets(exc)}")
        if isinstance(ent, (Channel, Chat)):
            chat_id = int(get_peer_id(ent))
            title = getattr(ent, "title", None) or uname
            bind_chat(db, uname, chat_id, title=title)
            if on_log:
                on_log(f"источник @{uname} · {title}")
            return chat_id
        return None

    # ── приватный invite (+hash / joinchat) ──
    h = invite_hash(ref) or ref.lstrip("+")
    if not h:
        return None

    chat_id: int | None = None
    title = ""

    try:
        check = await client(CheckChatInviteRequest(h))
        if isinstance(check, ChatInviteAlready):
            chat = getattr(check, "chat", None)
            if chat is not None:
                chat_id = int(get_peer_id(chat))
                title = getattr(chat, "title", None) or str(chat_id)
        else:
            chat = getattr(check, "chat", None)
            if chat is not None:
                chat_id = int(get_peer_id(chat))
                title = getattr(chat, "title", None) or str(chat_id)
            elif getattr(check, "title", None):
                title = str(check.title)
    except (InviteHashInvalidError, InviteHashExpiredError) as exc:
        if on_log:
            on_log(f"битый инвайт +{h}: {exc}")
        return None
    except FloodWaitError as exc:
        if on_log:
            on_log(
                f"FloodWait {exc.seconds}с check +{h} — пропускаю, доберу позже"
            )
        raise JoinRateLimited(int(exc.seconds))
    except Exception as exc:
        if "disconnected" in str(exc).casefold():
            raise ConnectionError("disconnected during join") from exc
        if on_log:
            on_log(f"check invite +{h}: {redact_secrets(exc)}")

    if chat_id:
        bind_chat(db, f"+{h}", chat_id, title=title)
        if on_log:
            on_log(f"источник +{h} · {title or chat_id}")
        return chat_id

    if not client.is_connected():
        raise ConnectionError("disconnected during join")

    try:
        updates = await client(ImportChatInviteRequest(h))
        chats = getattr(updates, "chats", None) or []
        for chat in chats:
            if isinstance(chat, (Channel, Chat)):
                chat_id = int(get_peer_id(chat))
                title = getattr(chat, "title", None) or str(chat_id)
                break
        if chat_id:
            bind_chat(db, f"+{h}", chat_id, title=title)
            if on_log:
                on_log(f"вступил · {title or chat_id}")
        return chat_id
    except UserAlreadyParticipantError:
        if title:
            found = await _dialog_by_title(client, title)
            if found:
                chat_id, title = found
                bind_chat(db, f"+{h}", chat_id, title=title)
                if on_log:
                    on_log(f"уже в чате · {title}")
                return chat_id
        if on_log:
            on_log(f"уже в чате +{h}, chat_id неизвестен — открой чат в Telegram")
        return None
    except FloodWaitError as exc:
        if on_log:
            on_log(
                f"FloodWait {exc.seconds}с вход +{h} — пропускаю, доберу позже"
            )
        raise JoinRateLimited(int(exc.seconds))
    except Exception as exc:
        if "disconnected" in str(exc).casefold():
            raise ConnectionError("disconnected during join") from exc
        if on_log:
            on_log(f"вход +{h}: {redact_secrets(exc)}")
        return None


async def verify_user_authorized(client: TelegramClient) -> bool:
    """
    Надёжная проверка входа.
    Telethon.is_user_authorized() при ЛЮБОМ RPCError ставит False навсегда —
    после обрыва прокси это выглядит как «сессия слетела».
    AuthKeyUnregistered/Duplicated — пробрасываем наверх (ключ реально мёртв).
    """
    from telethon.tl.functions.updates import GetStateRequest

    last: BaseException | None = None
    for attempt in range(1, 4):
        try:
            client._authorized = None
            me = await client.get_me()
            if me is not None:
                client._authorized = True
                return True
            # get_me=None часто при мёртвом ключе без исключения — проверяем GetState
            await client(GetStateRequest())
            client._authorized = True
            return True
        except (AuthKeyUnregisteredError, AuthKeyDuplicatedError):
            client._authorized = False
            raise
        except Exception as exc:
            last = exc
            if not client.is_connected():
                raise ConnectionError("disconnected during auth check") from exc
            await asyncio.sleep(1.2 * attempt)

    try:
        client._authorized = None
        await client(GetStateRequest())
        client._authorized = True
        return True
    except (AuthKeyUnregisteredError, AuthKeyDuplicatedError):
        client._authorized = False
        raise
    except Exception as exc:
        if last and "disconnected" in str(last).casefold():
            raise ConnectionError("disconnected during auth check") from last
        raise ConnectionError(f"auth check failed: {exc}") from exc


class TelegramUserSource:
    """Фоновый слушатель групп через личный аккаунт (Telethon)."""

    def __init__(
        self,
        settings: Settings,
        db: PostDatabase,
        *,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.on_log = on_log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: TelegramClient | None = None
        self._watch_ids: set[int] = set()
        self._titles: dict[int, str] = {}
        self._tick_all = 0
        self._tick_src = 0
        self._miss_logs = 0
        self._last_ids: dict[int, int] = {}
        self._refresh_event = threading.Event()

    def request_refresh_sources(self) -> None:
        """Перечитать invites из settings и дозайти в новые группы (без полного рестарта)."""
        self._refresh_event.set()
        self._log("обновляю список групп…")

    def _log(self, message: str) -> None:
        safe = redact_secrets(message)
        logger.info("[TG] %s", safe)
        if self.on_log:
            self.on_log(safe)

    def _source_chat_ids(self) -> set[int]:
        ids = set(getattr(self.settings, "telegram_source_chat_ids", ()) or ())
        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        ids |= bound_chat_ids(self.db, invites)
        return ids

    def _unique_watch_ids(self) -> list[int]:
        """Один chat_id на группу, без дублей -100 / короткого id."""
        used: set[int] = set()
        out: list[int] = []
        for cid in sorted(self._watch_ids):
            aliases = chat_id_aliases(cid)
            if aliases & used:
                continue
            used |= aliases
            out.append(int(cid))
        return out

    def _title(self, chat_id: int) -> str:
        return self._titles.get(chat_id) or str(chat_id)

    def start(self) -> None:
        if not getattr(self.settings, "telegram_user_enabled", False):
            self._log("выключен в .env (TELEGRAM_USER_ENABLED)")
            return
        if self._thread and self._thread.is_alive():
            return
        api_id = int(getattr(self.settings, "telegram_api_id", 0) or 0)
        api_hash = (getattr(self.settings, "telegram_api_hash", "") or "").strip()
        if not api_id or not api_hash:
            self._log("нет TELEGRAM_API_ID / HASH")
            return
        invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
        if not invites and not self._source_chat_ids():
            self._log("нет TELEGRAM_SOURCE_INVITES — некого слушать")
            return
        try:
            from instance_lock import acquire_telethon_session_lock

            if not acquire_telethon_session_lock(wait_ms=5000):
                self._log(
                    "сессию Telegram уже держит другой процесс — "
                    "закрой второе окно парсера, иначе Telegram снесёт вход"
                )
                return
        except Exception as exc:
            self._log(f"не взял блокировку сессии: {redact_secrets(exc)}")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_thread, name="tg-user-source", daemon=True
        )
        self._thread.start()
        self._log("подключаюсь к аккаунту…")

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        client = self._client
        if loop and client:
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(8)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        try:
            from instance_lock import release_telethon_session_lock

            release_telethon_session_lock()
        except Exception:
            pass

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._log(f"критическая ошибка: {redact_secrets(exc)}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            try:
                from instance_lock import release_telethon_session_lock

                release_telethon_session_lock()
            except Exception:
                pass

    async def _async_main(self) -> None:
        """Крутит сессии: при обрыве переподключается; файл не сносим зря."""
        from tg_mtproto import SessionDeadError

        backoff = 8
        auth_fail_streak = 0
        while not self._stop.is_set():
            try:
                await self._session_once()
                backoff = 8
                auth_fail_streak = 0
            except AuthKeyDuplicatedError:
                # Ключ уже убит на стороне Telegram — обычно из‑за 2 окон.
                auth_fail_streak += 1
                self._log(
                    "Telegram: эту сессию открыли дважды (второе окно/процесс). "
                    "Закрой лишний парсер. Через 25с проверю ещё раз…"
                )
                for _ in range(25):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(1)
                if auth_fail_streak >= 2:
                    try:
                        from tg_user_auth import clear_user_ready, force_reset_session

                        clear_user_ready()
                        force_reset_session()
                    except Exception:
                        pass
                    self._log(
                        "сессия сброшена после AuthKeyDuplicated — "
                        "нажми «ВОЙТИ В TELEGRAM»"
                    )
                    return
                continue
            except (AuthKeyUnregisteredError, SessionDeadError) as exc:
                auth_fail_streak += 1
                self._log(
                    f"ключ сессии не принят ({type(exc).__name__}) — "
                    f"попытка {auth_fail_streak}/2, файл пока не трогаю"
                )
                if auth_fail_streak >= 2:
                    try:
                        from tg_user_auth import clear_user_ready, force_reset_session

                        clear_user_ready()
                        force_reset_session()
                    except Exception:
                        pass
                    self._log(
                        "старый ключ мёртв (AuthKeyUnregistered) — "
                        "нажми «ВОЙТИ В TELEGRAM» один раз. "
                        "Потом сессию больше не сносим при обрывах связи."
                    )
                    return
            except Exception as exc:
                self._log(f"обрыв TG: {redact_secrets(exc)} · повтор через {backoff}с")
            if self._stop.is_set():
                return
            # Важно: после disconnect сервер ещё держит старый TCP —
            # мгновенный reconnect = AuthKeyDuplicated = ключ мёртв.
            cool = max(backoff, 12)
            self._log(f"пауза {cool}с перед новым подключением (защита ключа)")
            for _ in range(cool):
                if self._stop.is_set():
                    return
                await asyncio.sleep(1)
            backoff = min(backoff + 6, 45)

    async def _session_once(self) -> None:
        session_path = getattr(self.settings, "telegram_user_session", None)
        if not session_path:
            self._log("не задан путь сессии")
            return
        from tg_mtproto import SessionDeadError, _fmt_exc, connect_telegram_client
        from tg_user_auth import clear_user_ready, mark_user_ready

        delay = 8
        client = None
        while not self._stop.is_set():
            try:
                client = await connect_telegram_client(
                    session_path,
                    int(self.settings.telegram_api_id),
                    self.settings.telegram_api_hash,
                    proxy_raw=getattr(self.settings, "telegram_proxy", None),
                    timeout=20,
                    on_log=self._log,
                    roam_dcs=False,
                )
                self._client = client
                break
            except SessionDeadError as exc:
                try:
                    from tg_user_auth import LOGIN_STATE, session_file_ready

                    if LOGIN_STATE.exists() and session_file_ready():
                        self._log("идёт вход по коду — сессию не сбрасываю")
                        return
                except Exception:
                    pass
                # Не сносим файл здесь — _async_main сделает повтор и сброс только со 2-го раза
                self._log(str(exc))
                raise
            except Exception as exc:
                self._log(f"нет связи: {_fmt_exc(exc)} · повтор через {delay}с")
                for _ in range(delay):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(1)
                delay = min(delay + 8, 40)
        else:
            return

        assert client is not None
        try:
            try:
                ok = await verify_user_authorized(client)
            except (AuthKeyUnregisteredError, AuthKeyDuplicatedError):
                raise
            except ConnectionError as exc:
                self._log(f"проверка входа: {exc} — переподключусь")
                raise
            if not ok:
                clear_user_ready()
                self._log("нет входа — нажми «ВОЙТИ В TELEGRAM» и введи номер")
                await client.disconnect()
                self._client = None
                return

            mark_user_ready()
            me = await client.get_me()
            uname = getattr(me, "username", None) or getattr(me, "first_name", None) or me.id
            self._log(f"аккаунт @{uname} · смотрю группы своим аккаунтом")

            invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
            pending = unbound_invites(self.db, invites)
            already = len(invites) - len(pending)
            if already:
                self._log(f"уже привязано {already}/{len(invites)} — добираю остальные")
            joined = already
            # За один заход максимум N новых — иначе FloodWait/прокси убивают ключ
            budget = 8
            for i, inv in enumerate(pending):
                if self._stop.is_set():
                    return
                if budget <= 0:
                    self._log(
                        f"лимит вступлений за заход — осталось {len(pending) - i}, "
                        f"добью по 1 / 30с"
                    )
                    break
                if not client.is_connected():
                    self._log("связь пропала на вступлениях — переподключусь и продолжу")
                    raise ConnectionError("disconnected during join")
                try:
                    cid = await ensure_joined(client, self.db, inv, on_log=self._log)
                except JoinRateLimited as exc:
                    self._log(
                        f"Telegram ограничил join (~{exc.seconds}с) — "
                        f"останавливаю вступления, слушаю уже привязанные"
                    )
                    break
                except ConnectionError:
                    raise
                except Exception as exc:
                    self._log(f"join skip: {redact_secrets(exc)}")
                    cid = None
                if cid:
                    joined += 1
                    budget -= 1
                await asyncio.sleep(3.5)
            self._log(f"вступлений/привязок: {joined}/{len(invites)}")

            self._watch_ids = self._source_chat_ids()
            watch = self._unique_watch_ids()
            if watch:
                titles = []
                for cid in watch:
                    try:
                        if not client.is_connected():
                            break
                        ent = await client.get_entity(cid)
                        name = getattr(ent, "title", None) or str(cid)
                    except Exception:
                        name = str(cid)
                    self._titles[cid] = name
                    titles.append(name)
                self._log(f"источник ({len(watch)}): {', '.join(titles)}")
            elif invites:
                self._log("нет chat_id источника — чужие чаты не трогаю")

            if not watch:
                self._log("слушать нечего — задай TELEGRAM_SOURCE_INVITES / telegram_communities.txt")
                await client.disconnect()
                return

            @client.on(events.NewMessage(chats=watch))
            async def on_new_message(event) -> None:
                if self._stop.is_set():
                    return
                try:
                    await self._handle_message(event)
                except Exception as exc:
                    self._log(f"ошибка сообщения: {redact_secrets(exc)}")

            # Лёгкий догон: не рвём сессию на 50 чатах подряд
            try:
                await self._catch_up(client)
            except ConnectionError:
                self._log("догон оборвался — слушаю живые апдейты, догон в следующем цикле")
            self._log(f"слушаю {len(watch)} групп · чек каждые 30с")

            poll = asyncio.create_task(self._poll_loop(client))
            try:
                await client.run_until_disconnected()
            finally:
                poll.cancel()
                try:
                    await poll
                except Exception:
                    pass
        finally:
            self._client = None
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
            # Дать Telegram отпустить auth key на старом TCP
            await asyncio.sleep(2.5)

    async def _poll_loop(self, client: TelegramClient) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            if self._stop.is_set():
                return
            if not client.is_connected():
                self._log("чек: нет связи — жду переподключения")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return
            try:
                n = await self._poll_once(client)
            except Exception as exc:
                if "disconnected" in str(exc).casefold():
                    self._log("чек оборвался — переподключусь")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return
                self._log(f"чек не вышел: {redact_secrets(exc)}")
                continue
            names = ", ".join(self._title(i) for i in self._unique_watch_ids()) or "—"
            src = self._tick_src
            self._tick_src = 0
            self._tick_all = 0
            self._log(f"чек · {names} · новых {n} · за 30с обработал {src}")

            # Добираем группы, которые не успели привязать после обрыва
            try:
                invites = list(getattr(self.settings, "telegram_source_invites", ()) or ())
                pending = unbound_invites(self.db, invites)
                if pending and client.is_connected():
                    inv = pending[0]
                    try:
                        cid = await ensure_joined(client, self.db, inv, on_log=None)
                    except JoinRateLimited:
                        self._log("FloodWait — новые группы позже")
                        cid = None
                    if cid:
                        self._watch_ids = self._source_chat_ids()
                        self._log(f"добрал группу · осталось {len(pending) - 1}")
            except ConnectionError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return
            except Exception:
                pass

    async def _poll_once(self, client: TelegramClient) -> int:
        self._watch_ids = self._source_chat_ids()
        total = 0
        for cid in self._unique_watch_ids():
            last = int(self._last_ids.get(cid, 0) or 0)
            got = []
            try:
                kwargs = {"limit": 40}
                if last > 0:
                    kwargs["min_id"] = last
                async for m in client.iter_messages(cid, **kwargs):
                    mid = int(getattr(m, "id", 0) or 0)
                    if last and mid <= last:
                        break
                    got.append(m)
            except Exception as exc:
                self._log(
                    f"чек «{self._title(cid)}» не взялся: {redact_secrets(exc)}"
                )
                continue
            got.reverse()
            for m in got:
                if self._stop.is_set():
                    return total
                try:
                    await self._process_message(
                        m, cid, sender=getattr(m, "sender", None)
                    )
                    total += 1
                except Exception as exc:
                    self._log(f"ошибка чека: {redact_secrets(exc)}")
        return total

    def _remember_id(self, chat_id: int, msg_id: int) -> None:
        mid = int(msg_id or 0)
        if not mid or not chat_id:
            return
        cur = int(self._last_ids.get(int(chat_id), 0) or 0)
        if mid > cur:
            self._last_ids[int(chat_id)] = mid

    async def _catch_up(self, client: TelegramClient) -> None:
        """Короткий догон свежих постов. Обрыв → ConnectionError, без паники."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        for cid in self._unique_watch_ids():
            if self._stop.is_set():
                return
            if not client.is_connected():
                self._log("связь пропала на догоне — выхожу на переподключение")
                raise ConnectionError("disconnected during catch-up")
            # Уже знаем last_id — не тянем всю историю снова
            if int(self._last_ids.get(cid, 0) or 0) > 0:
                continue
            msgs = []
            try:
                async for m in client.iter_messages(cid, limit=15):
                    dt = getattr(m, "date", None)
                    if dt is not None:
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff:
                            break
                    msgs.append(m)
            except (AuthKeyUnregisteredError, AuthKeyDuplicatedError):
                raise
            except Exception as exc:
                if "disconnected" in str(exc).casefold():
                    raise ConnectionError("disconnected during catch-up") from exc
                self._log(f"история «{self._title(cid)}» не взялась: {redact_secrets(exc)}")
                continue
            msgs.reverse()
            if msgs:
                self._log(
                    f"догоняю {len(msgs)} постов · {self._title(cid)}"
                )
            for m in msgs:
                if self._stop.is_set():
                    return
                if not client.is_connected():
                    raise ConnectionError("disconnected during catch-up")
                try:
                    await self._process_message(m, cid, sender=getattr(m, "sender", None))
                    self._remember_id(cid, int(getattr(m, "id", 0) or 0))
                except Exception as exc:
                    self._log(f"ошибка догона: {redact_secrets(exc)}")
                await asyncio.sleep(0.08)

    async def _handle_message(self, event) -> None:
        message = event.message
        if not message:
            return
        self._tick_all += 1
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        chat = None
        if not chat_id:
            try:
                chat = await event.get_chat()
            except Exception:
                return
            chat_id = int(get_peer_id(chat)) if chat else 0
        if not chat_id:
            return
        title = self._titles.get(chat_id) or ""
        if not title:
            try:
                chat = chat or await event.get_chat()
                title = getattr(chat, "title", None) or str(chat_id)
            except Exception:
                title = str(chat_id)
            self._titles.setdefault(chat_id, title)

        watching = self._source_chat_ids()
        if not chat_in_set(chat_id, watching):
            return

        sender = await event.get_sender()
        await self._process_message(message, chat_id, sender=sender)

    async def _process_message(self, message, chat_id: int, *, sender=None) -> None:
        if sender is None:
            getter = getattr(message, "get_sender", None)
            if callable(getter):
                try:
                    sender = await getter()
                except Exception:
                    sender = None
        if sender is not None:
            try:
                message.sender = sender
            except Exception:
                pass
        if isinstance(sender, User) and bool(getattr(sender, "bot", False)):
            return

        post = telethon_message_to_post(message, chat_id=chat_id)
        if not post:
            self._tick_src += 1
            self._log(f"пост · {self._title(chat_id)} · пусто/медиа без текста")
            self._remember_id(chat_id, int(getattr(message, "id", 0) or 0))
            return

        self._tick_src += 1
        preview = (post.text or "").replace("\n", " ").strip()[:70]
        who = (post.username or "?").lstrip("@")
        self._log(f"пост · {self._title(chat_id)} · @{who} · {preview}")

        loop = asyncio.get_event_loop()
        ok, why = await loop.run_in_executor(
            None,
            lambda: evaluate_and_deliver_post(
                self.settings,
                self.db,
                post,
                require_russian=bool(getattr(self.settings, "require_russian", True)),
            ),
        )
        if ok:
            self._log(f"лид → {why}")
        else:
            self._log(f"skip · {why}")
        self._remember_id(chat_id, int(getattr(message, "id", 0) or 0))
