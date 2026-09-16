"""Точка входа: GUI по умолчанию, CLI через --cli."""

from __future__ import annotations

import argparse
import sys
import time

from playwright_env import enable_udp_for_app, fix_playwright_browsers_path

fix_playwright_browsers_path()
enable_udp_for_app()

from config import load_settings
from hashtags import display_hashtag
from models import logger
from worker import ParserWorker


def run_cli() -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error("Скопируйте .env.example → .env и заполните значения.")
        return 1

    worker = ParserWorker(settings=settings)

    def on_status(message: str) -> None:
        # logger уже пишет в worker; дублировать не нужно
        _ = message

    worker.on_status = on_status

    logger.info(
        "CLI-режим. Хэштеги: %s | интервал: %s мин | чат: %s",
        ", ".join(display_hashtag(t) for t in settings.search_hashtags),
        settings.check_interval_minutes,
        settings.telegram_chat_id,
    )

    try:
        worker.start()
        while worker.is_running():
            time.sleep(0.4)
        return 0
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C…")
        worker.stop()
        while worker.is_running():
            time.sleep(0.2)
        logger.info("Остановлено.")
        return 0


def main(argv: list[str] | None = None) -> int:
    from instance_lock import acquire_main_lock

    parser = argparse.ArgumentParser(
        description="Threads Posts ARTFrance — Threads hashtags → Telegram"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Запуск без окна, в консоли",
    )
    args = parser.parse_args(argv)

    if not acquire_main_lock():
        logger.error(
            "Уже запущен другой main.py — два процесса убивают Telegram-сессию. "
            "Закрой лишнее окно."
        )
        print("Парсер уже запущен — закрой второе окно.")
        return 2

    if args.cli:
        return run_cli()

    from gui import run_gui

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
