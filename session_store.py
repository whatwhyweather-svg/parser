"""Сохранение / загрузка сессии Threads (переносится между запусками и ПК)."""

from __future__ import annotations

import json
from pathlib import Path

from config import BASE_DIR

SESSION_PATH = BASE_DIR / "threads_session.json"
USERNAME_PATH = BASE_DIR / "threads_username.txt"


def session_exists() -> bool:
    if not SESSION_PATH.exists():
        return False
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    cookies = data.get("cookies") or []
    names = {c.get("name") for c in cookies if isinstance(c, dict)}
    return "sessionid" in names or "ds_user_id" in names or len(cookies) >= 5


def save_username(username: str | None) -> None:
    if not username:
        return
    username = username.strip().lstrip("@")
    if not username:
        return
    USERNAME_PATH.write_text(username, encoding="utf-8")


def load_username() -> str | None:
    if not USERNAME_PATH.exists():
        return None
    try:
        value = USERNAME_PATH.read_text(encoding="utf-8").strip().lstrip("@")
    except OSError:
        return None
    return value or None


def session_status_text() -> str:
    if session_exists():
        user = load_username()
        return f"SESSION OK · @{user}" if user else "SESSION OK"
    if SESSION_PATH.exists():
        return "Session file incomplete · sign in again"
    return "No session · sign in first"
