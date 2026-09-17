"""Один экземпляр парсера — Global Mutex + file lock + зачистка дублей."""

from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCK_PATH = BASE_DIR / "app.lock"
SUPERVISOR_LOCK = BASE_DIR / "supervisor.lock"
SESSION_LOCK_PATH = BASE_DIR / "tg_user.session.lock"
MAIN_LOCK_PATH = BASE_DIR / "main.lock"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080

_handle = None
_sup_handle = None
_bot_handle = None
_session_handle = None
_main_handle = None
_sup_file = None
_session_file = None
_main_file = None

_CreateMutexW = _kernel32.CreateMutexW
_CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_CreateMutexW.restype = wintypes.HANDLE
_WaitForSingleObject = _kernel32.WaitForSingleObject
_WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_WaitForSingleObject.restype = wintypes.DWORD
_ReleaseMutex = _kernel32.ReleaseMutex
_ReleaseMutex.argtypes = [wintypes.HANDLE]
_ReleaseMutex.restype = wintypes.BOOL
_CloseHandle = _kernel32.CloseHandle
ERROR_ALREADY_EXISTS = 183


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    # #region agent log
    try:
        from models import agent_dbg

        agent_dbg(hypothesis_id, location, message, data)
    except Exception:
        pass
    # #endregion


def _acquire_owned_mutex(name: str):
    """Singleton: свой mutex. Брошенный после краша забираем; живой чужой — отказ."""
    ctypes.set_last_error(0)
    handle = _CreateMutexW(None, True, name)
    err = ctypes.get_last_error()
    if not handle:
        return None
    if err == ERROR_ALREADY_EXISTS:
        # WAIT_ABANDONED = прошлый процесс умер, mutex свободен
        res = _WaitForSingleObject(handle, 0)
        if res in (WAIT_OBJECT_0, WAIT_ABANDONED):
            return handle
        _CloseHandle(handle)
        return None
    return handle


def _acquire_mutex(name: str, *, wait_ms: int = 0):
    """Ждём чужой mutex (Telethon session). Singleton'ы — через _acquire_owned_mutex."""
    ctypes.set_last_error(0)
    handle = _CreateMutexW(None, False, name)
    if not handle:
        return None
    res = _WaitForSingleObject(handle, int(wait_ms))
    if res in (WAIT_OBJECT_0, WAIT_ABANDONED):
        return handle
    _CloseHandle(handle)
    return None


def _acquire_file_lock(path: Path):
    """Эксклюзивный msvcrt lock — второй процесс не пройдёт даже без mutex."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            f.close()
        except OSError:
            pass
        return None
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()).encode("ascii"))
        f.flush()
    except OSError:
        pass
    return f


def _release_file_lock(f) -> None:
    if f is None:
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        f.close()
    except OSError:
        pass


def _is_our_script(low: str, script: str) -> bool:
    """True для '...\\main.py', не для случайной подстроки вроде af_boot_main.py."""
    needle = script.casefold()
    return f"\\{needle}" in low or f"/{needle}" in low or f" {needle}" in low


def kill_duplicate_parsers(
    *,
    include_watchdog: bool = True,
    include_supervisor: bool = False,
) -> list[int]:
    """
    Убивает ЧУЖИЕ python-процессы этого проекта.
    По умолчанию НЕ трогает windows_supervisor — иначе два старта
    убивают друг друга (exit 9) до захвата mutex.
    """
    my_pid = os.getpid()
    base = str(BASE_DIR).casefold().replace("/", "\\")
    markers: tuple[str, ...] = ("main.py",)
    if include_watchdog:
        markers = markers + ("watchdog.py",)
    if include_supervisor:
        markers = markers + (
            "windows_supervisor.py",
        )
        # НЕ добавляем "import windows_supervisor" — иначе -c debug/агент
        # убивает сам себя через тот же маркер.
    killed: list[int] = []
    try:
        ps = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (ps.stdout or "").strip()
        if not raw:
            return killed
        import json

        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
    except Exception:
        return killed

    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid == my_pid:
            continue
        cmd = str(row.get("CommandLine") or "")
        low = cmd.casefold().replace("/", "\\")
        # _python = base_prefix для .venv. Это НЕ второй парсер.
        if "\\_python\\" in low:
            continue
        if not any(_is_our_script(low, m) for m in markers):
            continue
        # не трогаем чужие проекты без нашего пути, если путь читаем
        if base not in low and "парсер2" not in low:
            if not _is_our_script(low, "main.py") and not _is_our_script(low, "watchdog.py"):
                if not include_supervisor:
                    continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            killed.append(pid)
        except Exception:
            try:
                os.kill(pid, 9)
                killed.append(pid)
            except OSError:
                pass
    if killed:
        time.sleep(1.2)
    return killed


def count_parser_mains() -> int:
    """Сколько чужих main.py этого проекта сейчас живо."""
    base = str(BASE_DIR).casefold().replace("/", "\\")
    n = 0
    try:
        ps = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (ps.stdout or "").strip()
        if not raw:
            return 0
        import json

        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
        my = os.getpid()
        for row in rows:
            try:
                pid = int(row.get("ProcessId") or 0)
            except (TypeError, ValueError):
                continue
            if not pid or pid == my:
                continue
            cmd = str(row.get("CommandLine") or "").casefold().replace("/", "\\")
            if "main.py" not in cmd:
                continue
            n += 1
    except Exception:
        return n
    return n


def acquire_gui_lock() -> bool:
    global _handle
    if _handle is not None:
        return True
    handle = _acquire_owned_mutex("Global\\ARTFranceThreadsParserGUI")
    if handle is None:
        _dbg("I", "instance_lock.py:acquire_gui_lock", "lock_fail", {"kind": "gui"})
        return False
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass
    _handle = handle
    return True


def acquire_supervisor_lock() -> bool:
    """Один start.bat / windows_supervisor. Сначала mutex, потом чистка дублей."""
    global _sup_handle, _sup_file
    if _sup_handle is not None and _sup_file is not None:
        return True
    handle = _acquire_owned_mutex("Global\\ARTFranceThreadsParserSupervisor")
    if handle is None:
        _dbg("I", "instance_lock.py:acquire_supervisor_lock", "lock_fail", {"kind": "supervisor"})
        return False
    f = _acquire_file_lock(SUPERVISOR_LOCK)
    if f is None:
        time.sleep(1.0)
        f = _acquire_file_lock(SUPERVISOR_LOCK)
    if f is None:
        try:
            _ReleaseMutex(handle)
            _CloseHandle(handle)
        except Exception:
            pass
        _dbg("I", "instance_lock.py:acquire_supervisor_lock", "lock_fail", {"kind": "supervisor_file"})
        return False
    _sup_handle = handle
    _sup_file = f
    _dbg(
        "I",
        "instance_lock.py:acquire_supervisor_lock",
        "lock_ok",
        {"kind": "supervisor", "pid": os.getpid()},
    )
    return True


def acquire_main_lock() -> bool:
    """Один main.py — даже если проскочили два supervisor."""
    global _main_handle, _main_file
    if _main_handle is not None and _main_file is not None:
        return True
    handle = _acquire_owned_mutex("Global\\ARTFranceThreadsParserMain")
    if handle is None:
        _dbg("I", "instance_lock.py:acquire_main_lock", "lock_fail", {"kind": "main"})
        return False
    f = _acquire_file_lock(MAIN_LOCK_PATH)
    if f is None:
        time.sleep(1.0)
        f = _acquire_file_lock(MAIN_LOCK_PATH)
    if f is None:
        try:
            _ReleaseMutex(handle)
            _CloseHandle(handle)
        except Exception:
            pass
        _dbg("I", "instance_lock.py:acquire_main_lock", "lock_fail", {"kind": "main_file"})
        return False
    _main_handle = handle
    _main_file = f
    _dbg(
        "I",
        "instance_lock.py:acquire_main_lock",
        "lock_ok",
        {"kind": "main", "pid": os.getpid()},
    )
    return True


def acquire_bot_poll_lock() -> bool:
    global _bot_handle
    if _bot_handle is not None:
        return True
    handle = _acquire_owned_mutex("Global\\ARTFranceThreadsParserBotPoll")
    if handle is None:
        return False
    _bot_handle = handle
    return True


def acquire_telethon_session_lock(*, wait_ms: int = 8000) -> bool:
    """Один Telethon на tg_user.session."""
    global _session_handle, _session_file
    if _session_handle is not None and _session_file is not None:
        return True
    handle = _acquire_mutex(
        "Global\\ARTFranceThreadsParserTelethonSession", wait_ms=wait_ms
    )
    if handle is None:
        return False
    f = _acquire_file_lock(SESSION_LOCK_PATH)
    if f is None:
        try:
            _ReleaseMutex(handle)
            _CloseHandle(handle)
        except Exception:
            pass
        return False
    _session_handle = handle
    _session_file = f
    return True


def release_telethon_session_lock() -> None:
    global _session_handle, _session_file
    handle = _session_handle
    f = _session_file
    _session_handle = None
    _session_file = None
    _release_file_lock(f)
    if not handle:
        return
    try:
        _ReleaseMutex(handle)
    except Exception:
        pass
    try:
        _CloseHandle(handle)
    except Exception:
        pass
