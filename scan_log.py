"""Единый журнал разбора: какие посты смотрели и что дали запросы."""

from __future__ import annotations

import time

from config import BASE_DIR

PATH = BASE_DIR / "scan_log.txt"
PREV_PATH = BASE_DIR / "scan_log.prev.txt"
MAX_BYTES = 8_000_000
# Один и тот же пост висит в выдаче много циклов — не пишем его чаще, чем раз в час
REPEAT_TTL_SEC = 3600
_recent: dict[str, float] = {}


def _rotate_if_needed() -> None:
    if not PATH.exists() or PATH.stat().st_size <= MAX_BYTES:
        return
    try:
        PREV_PATH.unlink(missing_ok=True)
        PATH.replace(PREV_PATH)
    except OSError:
        PATH.unlink(missing_ok=True)


def write(line: str) -> None:
    try:
        _rotate_if_needed()
        with PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass


def write_once(key: str, line: str) -> bool:
    """Пишет строку, если такой же не было за последний час. True — записали."""
    now = time.time()
    if len(_recent) > 5000:
        for k, ts in list(_recent.items()):
            if now - ts > REPEAT_TTL_SEC:
                _recent.pop(k, None)
    last = _recent.get(key)
    if last is not None and now - last < REPEAT_TTL_SEC:
        return False
    _recent[key] = now
    write(line)
    return True
