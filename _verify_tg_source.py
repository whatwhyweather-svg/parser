"""Проверка готовности парсинга Telegram-группы (без вывода секретов)."""
from __future__ import annotations

import sqlite3
import sys

import httpx
from dotenv import load_dotenv

from config import BASE_DIR, load_settings
from database import PostDatabase
from tg_source import bound_chat_ids, invite_hash, unbound_invites

load_dotenv(BASE_DIR / ".env")
settings = load_settings()
db = PostDatabase(settings.database_path)

token = settings.telegram_bot_token
invite_url = "https://t.me/+VP0VxHa2SyU-HZf1"
invite = invite_hash(invite_url)

issues: list[str] = []
ok: list[str] = []


def check(name: str, passed: bool, detail: str) -> None:
    line = f"[{'OK' if passed else 'FAIL'}] {name}: {detail}"
    print(line)
    (ok if passed else issues).append(line)


# 1. Config
check(
    "invite in .env",
    invite in settings.telegram_source_invites,
    f"parsed hash={invite!r}, env={list(settings.telegram_source_invites)}",
)
check("deepseek enabled", settings.deepseek_enabled, str(settings.deepseek_enabled))
check(
    "deepseek key present",
    bool(settings.deepseek_api_key),
    "yes" if settings.deepseek_api_key else "no",
)
check(
    "whitelist not empty",
    bool(settings.allowed_tg_ids),
    f"{len(settings.allowed_tg_ids)} ids",
)

# 2. DB binding
bound = bound_chat_ids(db, list(settings.telegram_source_invites))
free = unbound_invites(db, list(settings.telegram_source_invites))
rows = []
with sqlite3.connect(settings.database_path) as conn:
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'tg_source_chat:%'"
    ).fetchall()

check(
    "chat_id bound in DB",
    bool(bound) or bool(settings.telegram_source_chat_ids),
    f"bound={sorted(bound)} env_chat_ids={list(settings.telegram_source_chat_ids)} unbound_invites={free}",
)
for k, v in rows:
    print(f"  meta {k} = {v}")

# 3. Bot API
try:
    me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=30).json()
    check("bot getMe", me.get("ok"), f"@{me.get('result', {}).get('username')}")
except Exception as exc:
    check("bot getMe", False, str(exc))

# 4. If chat bound — getChat
all_ids = set(settings.telegram_source_chat_ids) | bound
for cid in sorted(all_ids):
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": cid},
            timeout=30,
        ).json()
        if r.get("ok"):
            title = (r.get("result") or {}).get("title") or cid
            check(f"getChat {cid}", True, title)
        else:
            check(
                f"getChat {cid}",
                False,
                r.get("description") or "unknown error",
            )
    except Exception as exc:
        check(f"getChat {cid}", False, str(exc))

# 5. Recent updates — bot in group?
try:
    upd = httpx.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"limit": 100, "timeout": 1},
        timeout=30,
    ).json()
    group_events = []
    if upd.get("ok"):
        for u in upd.get("result", []):
            if "my_chat_member" in u:
                c = u["my_chat_member"].get("chat", {})
                st = u["my_chat_member"].get("new_chat_member", {}).get("status")
                group_events.append(
                    (c.get("id"), c.get("title"), c.get("type"), st)
                )
            if "message" in u:
                c = u["message"].get("chat", {})
                if c.get("type") in {"group", "supergroup", "channel"}:
                    group_events.append(
                        (
                            c.get("id"),
                            c.get("title"),
                            c.get("type"),
                            "message",
                        )
                    )
    if group_events:
        print("[INFO] recent group-related updates:")
        for ev in group_events[-5:]:
            print(f"  chat_id={ev[0]} title={ev[1]!r} type={ev[2]} event={ev[3]}")
    else:
        print("[INFO] no recent group updates in getUpdates buffer (may be empty if polling active)")
except Exception as exc:
    print(f"[WARN] getUpdates: {exc}")

# 6. Code path sanity
try:
    import tg_bot  # noqa: F401
    import tg_source  # noqa: F401
    import tg_user_source  # noqa: F401

    check("modules import", True, "tg_bot + tg_source + tg_user_source")
except Exception as exc:
    check("modules import", False, str(exc))

# 6b. Telethon user mode
user_on = bool(getattr(settings, "telegram_user_enabled", False))
has_creds = bool(getattr(settings, "telegram_api_id", 0)) and bool(
    getattr(settings, "telegram_api_hash", "")
)
session_path = getattr(settings, "telegram_user_session", None)
session_ok = bool(session_path and session_path.exists())
check(
    "telethon user mode",
    not user_on or has_creds,
    f"enabled={user_on} api_id={'yes' if getattr(settings, 'telegram_api_id', 0) else 'no'} "
    f"api_hash={'yes' if getattr(settings, 'telegram_api_hash', '') else 'no'}",
)
if user_on and has_creds:
    check(
        "telethon session file",
        session_ok,
        f"exists={session_ok} path={session_path}",
    )
    if not session_ok:
        issues.append("[BLOCKER] запусти tg_user_login.bat для входа по телефону")

# 7. Recipients for delivery
users = db.all_started_users()
allowed = set(settings.allowed_tg_ids)
recipients = [u for u in users if int(u.get("user_id", 0)) in allowed] if allowed else []
check(
    "recipients (/start + whitelist)",
    bool(recipients),
    f"{len(recipients)} of {len(users)} started users",
)

print("\n=== SUMMARY ===")
if not bound and not settings.telegram_source_chat_ids:
    if user_on and has_creds and session_ok:
        pass  # user mode can bind on join
    elif user_on and has_creds:
        pass  # already flagged session
    else:
        issues.append(
            "[BLOCKER] chat_id не привязан: Telethon (tg_user_login.bat) "
            "или добавь бота в группу + Group Privacy Off"
        )

if issues:
    print("PROBLEMS:")
    for i in issues:
        print(" ", i)
    sys.exit(1)
else:
    print("All critical checks passed.")
    sys.exit(0)
