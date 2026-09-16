"""Windows-сторож: если парсер завис/убили — алерт в Telegram (+ balloon).

Обычно поднимается из windows_supervisor.py / start.bat.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import load_settings
from heartbeat import age_seconds, app_version, notify_local, progress_age_seconds, read
from models import logger, redact_secrets
from telegram_sender import TelegramSender

STATE_PATH = BASE_DIR / "watchdog_state.json"
STALE_SEC = 180
# GUI пульсирует heartbeat каждые 20с — смотрим progress_ts воркера
STUCK_SCAN_SEC = 12 * 60
POLL_SEC = 30
ALIVE = {"running", "scanning", "sleeping", "starting", "gui-alive"}
# Если GUI/воркер умерли на STOP, heartbeat часто остаётся "stopping" и больше не обновляется.
STALE_WATCH = ALIVE | {"stopping", "idle", "error"}


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"alerted_down": False, "alerted_stuck": False}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _send(text: str) -> bool:
    try:
        settings = load_settings()
        ok = TelegramSender(settings).send_alert(text)
        if not ok:
            notify_local("Парсер", text.replace("<b>", "").replace("</b>", "")[:200])
        return ok
    except Exception as exc:
        logger.error("watchdog telegram: %s", redact_secrets(exc))
        notify_local("Парсер", text.replace("<b>", "").replace("</b>", "")[:200])
        return False


def _kill_main() -> None:
    """Сорвать зависший main — supervisor поднимет заново."""
    try:
        from instance_lock import kill_duplicate_parsers

        kill_duplicate_parsers(include_watchdog=False, include_supervisor=False)
    except Exception as exc:
        logger.error("watchdog kill: %s", exc)


def check_once() -> str:
    data = read() or {}
    age = age_seconds()
    prog_age = progress_age_seconds()
    status = str(data.get("status") or "")
    version = str(data.get("version") or app_version())
    state = _load_state()
    stale = age is None or age > STALE_SEC
    should_watch = status in STALE_WATCH or (not data and not state.get("alerted_down"))

    # Зависший поиск: GUI держит ts свежим, progress_ts — нет
    if (
        status == "scanning"
        and prog_age is not None
        and prog_age > STUCK_SCAN_SEC
        and not stale
    ):
        if not state.get("alerted_stuck"):
            extra = data.get("extra") or ""
            msg = (
                f"<b>Threads Posts ARTFrance</b>\n"
                f"⚠ Поиск завис ({int(prog_age)}с без прогресса) — перезапуск\n"
                f"версия {version}"
                + (f"\n{extra}" if extra else "")
            )
            _send(msg)
            state["alerted_stuck"] = True
            _save_state(state)
            _kill_main()
            return "stuck-restart"
        return "still-stuck"

    if not stale and status in ALIVE and prog_age is not None and prog_age < 60:
        state["alerted_stuck"] = False

    if stale and status in STALE_WATCH:
        if not state.get("alerted_down"):
            extra = data.get("extra") or ""
            msg = (
                f"<b>Threads Posts ARTFrance</b>\n"
                f"⛔ Парсер не отвечает (heartbeat {int(age or 0)}с)\n"
                f"версия {version}\n"
                f"последний статус: {status}"
                + (f"\n{extra}" if extra else "")
            )
            _send(msg)
            state["alerted_down"] = True
            _save_state(state)
            return "down-alert"
        return "still-down"

    if not stale and status in ALIVE and state.get("alerted_down"):
        _send(
            f"<b>Threads Posts ARTFrance</b>\n"
            f"✅ Парсер снова жив\nверсия {version}"
        )
        state["alerted_down"] = False
        state["alerted_stuck"] = False
        _save_state(state)
        return "recovered"

    if status == "stopped":
        state["alerted_down"] = False
        state["alerted_stuck"] = False
        _save_state(state)
        return "stopped"

    _ = should_watch
    return "ok"


def main() -> int:
    logger.info("watchdog старт · version %s · stale>%ss", app_version(), STALE_SEC)
    while True:
        try:
            check_once()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            logger.error("watchdog: %s", exc)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(check_once())
        raise SystemExit(0)
    raise SystemExit(main())
