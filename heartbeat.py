"""Пульс процесса парсера: файл, по которому watchdog понимает, жив ли софт."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from models import logger

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_PATH = BASE_DIR / "heartbeat.json"
VERSION_PATH = BASE_DIR / "VERSION"


def app_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def touch(status: str, *, extra: str = "", progress: bool = False) -> None:
    """
    progress=True — реальный прогресс воркера (поиск/фильтр).
    GUI-пульс не должен обновлять progress_ts, иначе watchdog не видит зависание.
    """
    prev = read() or {}
    now = time.time()
    progress_ts = float(prev.get("progress_ts") or 0)
    if progress or status in {"starting", "sleeping", "stopped", "error", "idle"}:
        progress_ts = now
    payload = {
        "ts": now,
        "status": status,
        "extra": extra,
        "version": app_version(),
        "progress_ts": progress_ts,
    }
    try:
        HEARTBEAT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("heartbeat: %s", exc)


def read() -> dict | None:
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def age_seconds() -> float | None:
    data = read()
    if not data:
        return None
    try:
        return max(0.0, time.time() - float(data.get("ts") or 0))
    except (TypeError, ValueError):
        return None


def progress_age_seconds() -> float | None:
    data = read()
    if not data:
        return None
    try:
        pts = float(data.get("progress_ts") or data.get("ts") or 0)
        if pts <= 0:
            return None
        return max(0.0, time.time() - pts)
    except (TypeError, ValueError):
        return None


def notify_local(title: str, body: str) -> None:
    """Windows balloon, если Telegram тоже мёртв."""
    title = title.replace("'", "''")[:80]
    body = body.replace("'", "''")[:240]
    script = (
        "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
        "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Drawing');"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Error;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(10000, '{title}', '{body}', 'Error');"
        "Start-Sleep -Seconds 12; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("local notify: %s", exc)
