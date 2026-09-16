"""Windows: Telethon зовёт ssl.dll за AES. В OpenSSL 3 AES лежит в libcrypto."""

from __future__ import annotations

import ctypes.util
import os
import sys
from pathlib import Path


def _crypto_dll() -> Path | None:
    dlls = Path(sys.base_prefix) / "DLLs"
    for name in ("libcrypto-3.dll", "libcrypto-1_1.dll"):
        path = dlls / name
        if path.exists():
            return path
    return None


def fix_telethon_ssl() -> None:
    dlls = Path(sys.base_prefix) / "DLLs"
    if dlls.is_dir():
        try:
            os.add_dll_directory(str(dlls))
        except (OSError, AttributeError):
            pass
        path = os.environ.get("PATH", "")
        prefix = str(dlls)
        if prefix not in path.split(os.pathsep):
            os.environ["PATH"] = prefix + os.pathsep + path

        ssl3 = dlls / "libssl-3.dll"
        alias = dlls / "ssl.dll"
        # Кривая копия libssl (без AES) ломает MTProto — «связь есть, шифрование нет».
        if alias.exists() and ssl3.exists() and alias.stat().st_size == ssl3.stat().st_size:
            try:
                alias.unlink()
            except OSError:
                pass

    crypto = _crypto_dll()
    if crypto is None:
        return

    orig = ctypes.util.find_library

    def find_library(name):  # noqa: ANN001
        if str(name or "").casefold() in {"ssl", "libssl"}:
            return str(crypto)
        return orig(name)

    ctypes.util.find_library = find_library  # type: ignore[method-assign]


fix_telethon_ssl()
