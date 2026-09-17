"""Модель поста и вспомогательные функции."""

from __future__ import annotations

import html
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime

_BOT_TOKEN_RE = re.compile(r"(?:/bot)?\d{6,12}:[A-Za-z0-9_-]{20,}")


def redact_secrets(text: object) -> str:
    """Прячет токен бота в логах."""
    return _BOT_TOKEN_RE.sub("***TOKEN***", str(text))


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("threads2tg")
logger.addFilter(_RedactFilter())
# Служебный шум Telethon (ssl.dll / cryptg) не должен пугать в консоли
logging.getLogger("telethon.crypto").setLevel(logging.WARNING)


@dataclass
class ThreadPost:
    post_id: str
    code: str
    username: str
    text: str
    url: str
    image_url: str | None
    hashtag: str
    comments_count: int = 0
    # unix sec; 0 = неизвестно (DOM / старый путь)
    posted_at: float = 0.0
    # threads | telegram
    source: str = "threads"
    author_tg_id: int | None = None

    @property
    def author_url(self) -> str:
        if (self.source or "").casefold() == "telegram":
            if self.username and not self.username.startswith("id"):
                return f"https://t.me/{self.username.lstrip('@')}"
            if self.author_tg_id:
                return f"tg://user?id={int(self.author_tg_id)}"
            return self.url or "https://t.me/"
        return f"https://www.threads.net/@{self.username}"

    @property
    def is_telegram(self) -> bool:
        if (self.source or "").casefold() == "telegram":
            return True
        tag = (self.hashtag or "").casefold()
        url = self.url or ""
        return tag == "telegram" or url.startswith("https://t.me/")


def agent_dbg(*_a, **_k) -> None:
    return


def human_delay(min_sec: float = 1.2, max_sec: float = 3.5) -> None:
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def escape_html(text: str) -> str:
    return html.escape(text or "", quote=False)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def now_stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")
