"""Playwright в Cursor часто смотрит в sandbox-кэш, где нет Chromium."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import tg_ssl  # noqa: F401  — libcrypto как ssl.dll для Telethon, до импорта telethon

# WARP MASQUE = HTTP/3 по UDP. --disable-quic ломает туннель и Chrome.
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--enable-quic",
]


def enable_udp_for_app() -> None:
    """Исходящий и входящий UDP для интерпретатора — WARP/QUIC иначе режет Windows Firewall."""
    if os.name != "nt":
        return
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    exe = sys.executable
    name = "ARTFrance-UDP"
    try:
        subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=out",
                "action=allow",
                "protocol=UDP",
                f"program={exe}",
                "enable=yes",
            ],
            timeout=8,
            capture_output=True,
            creationflags=flags,
        )
        subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}-in",
                "dir=in",
                "action=allow",
                "protocol=UDP",
                f"program={exe}",
                "enable=yes",
            ],
            timeout=8,
            capture_output=True,
            creationflags=flags,
        )
    except Exception:
        pass


def fix_playwright_browsers_path() -> None:
    raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    bad = "cursor-sandbox-cache" in raw.replace("\\", "/").casefold()
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    stable = home / "ms-playwright"
    if bad or not raw:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        if stable.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(stable)
