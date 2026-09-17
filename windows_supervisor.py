"""Windows: держит GUI + watchdog, перезапускает парсер если он упал."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from playwright_env import enable_udp_for_app, fix_playwright_browsers_path

fix_playwright_browsers_path()
enable_udp_for_app()

from heartbeat import app_version, notify_local, read
from models import logger, redact_secrets

PY = BASE_DIR / ".venv" / "Scripts" / "python.exe"
MAIN = BASE_DIR / "main.py"
WATCHDOG = BASE_DIR / "watchdog.py"
LOCK = BASE_DIR / "watchdog.lock"
RESTART_SEC = 18
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _watchdog_alive() -> bool:
    if not LOCK.exists():
        return False
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _child_env() -> dict[str, str]:
    fix_playwright_browsers_path()
    env = dict(os.environ)
    raw = env.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if "cursor-sandbox-cache" in raw.replace("\\", "/").casefold():
        env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    return env


def _start_watchdog() -> None:
    if _watchdog_alive():
        return
    proc = subprocess.Popen(
        [str(PY), str(WATCHDOG)],
        cwd=str(BASE_DIR),
        env=_child_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | DETACHED,
    )
    LOCK.write_text(str(proc.pid), encoding="utf-8")
    logger.info("watchdog pid %s", proc.pid)


def _alert_crash(reason: str) -> None:
    text = (
        f"<b>Threads Posts ARTFrance</b>\n"
        f"⛔ Окно парсера упало, перезапускаю через {RESTART_SEC}с\n"
        f"версия {app_version()}\n"
        f"{reason}"
    )
    try:
        from config import load_settings
        from telegram_sender import TelegramSender

        TelegramSender(load_settings()).send_alert(text)
    except Exception as exc:
        logger.error("supervisor alert: %s", redact_secrets(exc))
        notify_local("Парсер упал", reason[:200])


def _clean_exit() -> bool:
    """Только свежий STOP из GUI. Старый heartbeat 'stopped' не глушит start.bat."""
    data = read() or {}
    if str(data.get("status") or "") != "stopped":
        return False
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return ts > 1_000_000_000 and (time.time() - ts) < 60


def main() -> int:
    if not PY.exists():
        print("Нет .venv — сначала install.bat")
        return 1
    from instance_lock import acquire_supervisor_lock, kill_duplicate_parsers

    if not acquire_supervisor_lock():
        logger.error(
            "Парсер уже запущен (другой start.bat). Закрой лишнее окно."
        )
        print("Парсер уже запущен — оставь одно окно.")
        return 2

    # Только лишние main/watchdog. НЕ трогаем _python — это base для .venv!
    killed = kill_duplicate_parsers(
        include_watchdog=True, include_supervisor=False
    )
    if killed:
        logger.info("закрыл лишние main/watchdog: %s", ",".join(map(str, killed)))

    logger.info("Windows supervisor · %s", app_version())
    logger.info(
        "base python: %s (это норма, не второй парсер)",
        Path(sys.base_prefix).name,
    )
    _start_watchdog()
    crashes = 0
    while True:
        print("Windows supervisor: запускаю GUI…", flush=True)
        proc = subprocess.Popen(
            [str(PY), "-X", "utf8", "-u", str(MAIN)], cwd=str(BASE_DIR), env=_child_env()
        )
        code = proc.wait()
        if _clean_exit() or code == 0:
            logger.info("парсер закрыт нормально (код %s)", code)
            return 0
        crashes += 1
        reason = f"exit_code={code} · падение #{crashes}"
        logger.error("%s", reason)
        _alert_crash(reason)
        time.sleep(RESTART_SEC)
        _start_watchdog()


if __name__ == "__main__":
    raise SystemExit(main())
