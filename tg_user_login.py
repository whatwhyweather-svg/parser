"""Одноразовый вход Telethon (личный аккаунт) + вступление в группы-источники."""

from __future__ import annotations

import asyncio
import getpass
import sys

from telethon.errors import SessionPasswordNeededError

from config import load_settings
from database import PostDatabase
from tg_mtproto import make_telegram_client
from tg_user_auth import mark_user_ready
from tg_user_source import ensure_joined


async def _main() -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Ошибка .env: {exc}")
        return 1

    api_id = int(getattr(settings, "telegram_api_id", 0) or 0)
    api_hash = (getattr(settings, "telegram_api_hash", "") or "").strip()
    if not api_id or not api_hash:
        print("Нет API Telegram — проверь config")
        return 1

    session_path = getattr(settings, "telegram_user_session", None)
    if not session_path:
        print("Не задан TELEGRAM_USER_SESSION")
        return 1

    client = make_telegram_client(
        session_path,
        api_id,
        api_hash,
        proxy_raw=getattr(settings, "telegram_proxy", None),
    )

    phone = (getattr(settings, "telegram_user_phone", "") or "").strip()
    if not phone:
        phone = input("Номер телефона (+7...): ").strip()

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Код из Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = getpass.getpass("Облачный пароль 2FA: ")
            await client.sign_in(password=pwd)
    mark_user_ready()
    me = await client.get_me()
    print(f"OK · @{getattr(me, 'username', None) or me.id}")

    db = PostDatabase(settings.database_path)
    invites = list(getattr(settings, "telegram_source_invites", ()) or ())
    if not invites:
        print("TELEGRAM_SOURCE_INVITES пуст — группы не добавлены")
    for inv in invites:
        cid = await ensure_joined(client, db, inv)
        print(f"группа: {inv} → {cid}")

    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
