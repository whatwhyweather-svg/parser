"""SQLite: дубликаты, bootstrap, пользователи Telegram и их теги."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hashtags import normalize_hashtag


class PostDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_posts (
                    post_id TEXT PRIMARY KEY,
                    keyword TEXT,
                    url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tg_users (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(tg_users)").fetchall()
            }
            if "display_name" not in cols:
                conn.execute(
                    "ALTER TABLE tg_users ADD COLUMN display_name TEXT DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_tags (
                    user_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (user_id, tag),
                    FOREIGN KEY (user_id) REFERENCES tg_users(user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_to_user (
                    user_id INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, post_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    post_text TEXT DEFAULT '',
                    hashtag TEXT DEFAULT '',
                    draft TEXT DEFAULT '',
                    status TEXT DEFAULT 'new'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tag TEXT DEFAULT '',
                    stage TEXT NOT NULL,
                    n INTEGER NOT NULL DEFAULT 1,
                    detail TEXT DEFAULT ''
                )
                """
            )

    def _tag_key(self, hashtag: str) -> str:
        return f"bootstrapped_tag:{normalize_hashtag(hashtag).casefold()}"

    def is_tag_bootstrapped(self, hashtag: str) -> bool:
        key = self._tag_key(hashtag)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
        return bool(row and row["value"] == "1")

    def mark_tag_bootstrapped(self, hashtag: str) -> None:
        key = self._tag_key(hashtag)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key,),
            )

    def reset_tag_bootstrap(self, hashtag: str) -> None:
        key = self._tag_key(hashtag)
        with self._connect() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def reset_tags_bootstrap(self, hashtags: list[str]) -> None:
        for tag in hashtags:
            self.reset_tag_bootstrap(tag)

    def clear_keyword_posts(self, hashtag: str) -> int:
        """Сброс тега: и sent_posts, и sent_to_user — иначе ЛС «уже было» → вечный dm_fail."""
        tag = normalize_hashtag(hashtag)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT post_id FROM sent_posts WHERE lower(keyword) = lower(?)",
                (tag,),
            ).fetchall()
            post_ids = [r["post_id"] for r in rows]
            cur = conn.execute(
                "DELETE FROM sent_posts WHERE lower(keyword) = lower(?)",
                (tag,),
            )
            if post_ids:
                conn.executemany(
                    "DELETE FROM sent_to_user WHERE post_id = ?",
                    [(pid,) for pid in post_ids],
                )
            return int(cur.rowcount or 0)

    def clear_keywords_posts(self, hashtags: list[str]) -> int:
        total = 0
        for tag in hashtags:
            total += self.clear_keyword_posts(tag)
        return total

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    def exists(self, post_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_posts WHERE post_id = ? LIMIT 1",
                (post_id,),
            ).fetchone()
            if row is not None:
                return True
            # После частичного сброса: пост уже уходил в ЛС, в sent_posts его нет
            row = conn.execute(
                "SELECT 1 FROM sent_to_user WHERE post_id = ? LIMIT 1",
                (post_id,),
            ).fetchone()
        return row is not None

    def add(self, post_id: str, keyword: str, url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_posts(post_id, keyword, url)
                VALUES (?, ?, ?)
                """,
                (post_id, keyword, url),
            )

    # ── Telegram users ──

    def upsert_user(
        self,
        user_id: int,
        chat_id: int,
        username: str = "",
        *,
        display_name: str = "",
        active: bool = True,
    ) -> bool:
        """True = новый пользователь (первый раз в базе)."""
        with self._connect() as conn:
            existed = conn.execute(
                "SELECT 1 FROM tg_users WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO tg_users(user_id, chat_id, username, display_name, active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    username = excluded.username,
                    display_name = CASE
                        WHEN excluded.display_name != '' THEN excluded.display_name
                        ELSE tg_users.display_name
                    END,
                    active = excluded.active
                """,
                (
                    user_id,
                    chat_id,
                    username or "",
                    display_name or "",
                    1 if active else 0,
                ),
            )
        return existed is None

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tg_users SET active = ? WHERE user_id = ?",
                (1 if active else 0, user_id),
            )

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tg_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "user_id": row["user_id"],
            "chat_id": row["chat_id"],
            "username": row["username"] or "",
            "display_name": row["display_name"] if "display_name" in row.keys() else "",
            "active": bool(row["active"]),
        }

    def add_user_tag(self, user_id: int, tag: str) -> None:
        tag = normalize_hashtag(tag)
        if not tag:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_tags(user_id, tag) VALUES (?, ?)",
                (user_id, tag),
            )

    def remove_user_tag(self, user_id: int, tag: str) -> bool:
        tag = normalize_hashtag(tag)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_tags WHERE user_id = ? AND lower(tag) = lower(?)",
                (user_id, tag),
            )
            return (cur.rowcount or 0) > 0

    def get_user_tags(self, user_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tag FROM user_tags WHERE user_id = ? ORDER BY tag",
                (user_id,),
            ).fetchall()
        return [r["tag"] for r in rows]

    def all_subscribed_tags(self) -> list[str]:
        """Все теги всех активных пользователей (для парсинга)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ut.tag
                FROM user_tags ut
                JOIN tg_users u ON u.user_id = ut.user_id
                WHERE u.active = 1
                ORDER BY ut.tag
                """
            ).fetchall()
        out: list[str] = []
        seen: set[str] = set()
        for r in rows:
            key = r["tag"].casefold()
            if key not in seen:
                seen.add(key)
                out.append(r["tag"])
        return out

    def all_started_users(self) -> list[dict[str, Any]]:
        """Все, кто когда-либо написал боту — видны в GUI для разрешения."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, chat_id, username, display_name, active, created_at
                FROM tg_users
                ORDER BY created_at DESC, user_id DESC
                """
            ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "chat_id": r["chat_id"],
                "username": r["username"] or "",
                "display_name": (r["display_name"] or "") if "display_name" in r.keys() else "",
                "active": bool(r["active"]),
                "created_at": r["created_at"] if "created_at" in r.keys() else "",
            }
            for r in rows
        ]

    def users_for_tag(self, tag: str) -> list[dict[str, Any]]:
        """Подписчики конкретного тега (если теги персональные)."""
        tag_n = normalize_hashtag(tag).casefold()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT u.user_id, u.chat_id, u.username, u.active
                FROM tg_users u
                JOIN user_tags ut ON ut.user_id = u.user_id
                WHERE lower(ut.tag) = ?
                """,
                (tag_n,),
            ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "chat_id": r["chat_id"],
                "username": r["username"] or "",
                "active": bool(r["active"]),
            }
            for r in rows
        ]

    def user_already_got(self, user_id: int, post_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_to_user WHERE user_id = ? AND post_id = ?",
                (user_id, post_id),
            ).fetchone()
        return row is not None

    def mark_sent_to_user(self, user_id: int, post_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_to_user(user_id, post_id) VALUES (?, ?)",
                (user_id, post_id),
            )

    def delete_user(self, user_id: int) -> bool:
        """Удалить пользователя и его теги/историю рассылки из базы."""
        with self._connect() as conn:
            conn.execute("DELETE FROM user_tags WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM sent_to_user WHERE user_id = ?", (user_id,))
            cur = conn.execute("DELETE FROM tg_users WHERE user_id = ?", (user_id,))
            return (cur.rowcount or 0) > 0

    def save_lead(self, post) -> int:
        """Один lead_id на post_id — не плодим дубликаты при рассылке нескольким юзерам."""
        post_id = getattr(post, "post_id", "") or ""
        with self._connect() as conn:
            if post_id:
                row = conn.execute(
                    "SELECT id FROM lead_replies WHERE post_id = ? ORDER BY id DESC LIMIT 1",
                    (post_id,),
                ).fetchone()
                if row:
                    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
            cur = conn.execute(
                """
                INSERT INTO lead_replies(post_id, url, username, post_text, hashtag, status)
                VALUES (?, ?, ?, ?, ?, 'new')
                """,
                (
                    post_id,
                    getattr(post, "url", "") or "",
                    getattr(post, "username", "") or "",
                    getattr(post, "text", "") or "",
                    getattr(post, "hashtag", "") or "",
                ),
            )
            return int(cur.lastrowid or 0)

    def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lead_replies WHERE id = ?",
                (int(lead_id),),
            ).fetchone()
        if not row:
            return None
        return {k: row[k] for k in row.keys()}

    def set_lead_draft(self, lead_id: int, draft: str, *, status: str = "draft") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE lead_replies SET draft = ?, status = ? WHERE id = ?",
                (draft or "", status, int(lead_id)),
            )

    def log_pipeline(
        self, tag: str, stage: str, n: int = 1, detail: str = ""
    ) -> None:
        if n <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_events(tag, stage, n, detail)
                VALUES (?, ?, ?, ?)
                """,
                (tag or "", stage, int(n), (detail or "")[:240]),
            )

    def sent_today(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE stage = 'sent'
                  AND date(ts, 'localtime') = date('now', 'localtime')
                """
            ).fetchone()
        if row and int(row["c"] or 0):
            return int(row["c"])
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM sent_posts
                WHERE date(created_at, 'localtime') = date('now', 'localtime')
                """
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def funnel_today(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT stage, COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE date(ts, 'localtime') = date('now', 'localtime')
                GROUP BY stage
                """
            ).fetchall()
        return {str(r["stage"]): int(r["c"] or 0) for r in rows}

    def sent_last_hours(self, hours: float = 1.0) -> int:
        hours = max(0.1, float(hours))
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE stage = 'sent'
                  AND ts >= datetime('now', ?)
                """,
                (f"-{hours} hours",),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def funnel_last_hours(self, hours: float = 1.0) -> dict[str, int]:
        hours = max(0.1, float(hours))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT stage, COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE ts >= datetime('now', ?)
                GROUP BY stage
                """,
                (f"-{hours} hours",),
            ).fetchall()
        return {str(r["stage"]): int(r["c"] or 0) for r in rows}

    def sent_on_local_date(self, day: str) -> int:
        """day = 'YYYY-MM-DD' в локальной зоне SQLite."""
        day = (day or "").strip()
        if not day:
            return 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE stage = 'sent'
                  AND date(ts, 'localtime') = ?
                """,
                (day,),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def funnel_on_local_date(self, day: str) -> dict[str, int]:
        day = (day or "").strip()
        if not day:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT stage, COALESCE(SUM(n), 0) AS c
                FROM pipeline_events
                WHERE date(ts, 'localtime') = ?
                GROUP BY stage
                """,
                (day,),
            ).fetchall()
        return {str(r["stage"]): int(r["c"] or 0) for r in rows}

    def top_drop_reason_on_local_date(self, day: str) -> str:
        day = (day or "").strip()
        if not day:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT detail, SUM(n) AS c
                FROM pipeline_events
                WHERE stage = 'drop'
                  AND date(ts, 'localtime') = ?
                  AND detail IS NOT NULL
                  AND detail != ''
                GROUP BY detail
                ORDER BY c DESC
                LIMIT 1
                """,
                (day,),
            ).fetchone()
        if not row:
            return ""
        return str(row["detail"] or "")[:120]

    def find_user_by_username(self, username: str) -> dict[str, Any] | None:
        uname = (username or "").strip().lstrip("@").casefold()
        if not uname:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, chat_id, username, display_name, active
                FROM tg_users
                WHERE lower(username) = ?
                LIMIT 1
                """,
                (uname,),
            ).fetchone()
        if not row:
            return None
        return {
            "user_id": int(row["user_id"]),
            "chat_id": int(row["chat_id"]),
            "username": row["username"] or "",
            "display_name": row["display_name"] or "",
            "active": bool(row["active"]),
        }

    def top_drop_reason_today(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT detail, SUM(n) AS c
                FROM pipeline_events
                WHERE stage = 'drop'
                  AND date(ts, 'localtime') = date('now', 'localtime')
                  AND detail != ''
                GROUP BY detail
                ORDER BY c DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return ""
        return f"{row['detail']} ({int(row['c'] or 0)})"

