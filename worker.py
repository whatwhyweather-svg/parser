"""Фоновый воркер парсинга (для GUI и CLI)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from config import Settings
from database import PostDatabase
from deepseek_filter import DeepSeekFilter
from filters import looks_like_clear_hire, post_passes_filters, scrub_post_text
from hashtags import display_hashtag, normalize_hashtags
from models import ThreadPost, escape_html, logger
from scraper import ThreadsScraper
from telegram_sender import TelegramSender, lead_keyboard
from heartbeat import touch as heartbeat_touch
from scan_log import write as scan_log
from scan_log import write_once as scan_log_once

StatusCallback = Callable[[str], None]
StatsCallback = Callable[["WorkerStats"], None]

POST_BOOTSTRAP_PAUSE_SEC = 3


@dataclass
class WorkerStats:
    status: str = "idle"
    cycle: int = 0
    found: int = 0
    sent: int = 0
    skipped: int = 0
    last_hashtag: str = "—"
    message: str = ""
    ready_to_publish: bool = False


@dataclass
class ParserWorker:
    settings: Settings
    hashtags: list[str] = field(default_factory=list)
    interval_minutes: int = 2
    parse_posts: int = 5
    on_status: StatusCallback | None = None
    on_stats: StatsCallback | None = None

    def __post_init__(self) -> None:
        if not self.hashtags:
            self.hashtags = list(self.settings.search_hashtags)
        self.interval_minutes = (
            self.interval_minutes or self.settings.check_interval_minutes
        )
        default_parse = int(getattr(self.settings, "parse_posts", 5) or 5)
        self.parse_posts = max(1, min(200, int(self.parse_posts or default_parse)))
        self.stats = WorkerStats()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._db: PostDatabase | None = None
        self._pending_new_tags: list[str] = []
        # Счётчики для АФК-надзора: молчащий DeepSeek и слепой скрейпер
        self._ai_fail_streak = 0
        self._blind_cycles = 0
        self._rate_limit_hits = 0
        self._ai: DeepSeekFilter | None = None
        if getattr(self.settings, "deepseek_enabled", False) and getattr(
            self.settings, "deepseek_api_key", ""
        ):
            self._ai = DeepSeekFilter(
                self.settings.deepseek_api_key,
                base_url=getattr(
                    self.settings, "deepseek_base_url", "https://api.deepseek.com"
                ),
                model=getattr(self.settings, "deepseek_model", "deepseek-chat"),
            )

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, status: str | None = None, message: str = "") -> None:
        if status:
            self.stats.status = status
        if message:
            self.stats.message = message
            logger.info("[THREADS] %s", message)
        # Любой emit от воркера = живой прогресс (не GUI-пульс)
        heartbeat_touch(
            self.stats.status or status or "running",
            extra=message or getattr(self.stats, "message", "") or "",
            progress=True,
        )
        if self.on_status and message:
            self.on_status(message)
        if self.on_stats:
            self.on_stats(self.stats)

    def start(
        self,
        hashtags: list[str] | None = None,
        interval_minutes: int | None = None,
        parse_posts: int | None = None,
    ) -> None:
        if self.is_running():
            return
        tags = normalize_hashtags(
            hashtags if hashtags is not None else self.hashtags
        )
        with self._lock:
            self.hashtags = tags
            self._pending_new_tags.clear()
        if interval_minutes is not None:
            self.interval_minutes = max(1, int(interval_minutes))
        if parse_posts is not None:
            self.parse_posts = max(1, min(200, int(parse_posts)))
        self._stop.clear()
        self._wake.clear()
        self.stats = WorkerStats(status="starting")
        self._thread = threading.Thread(
            target=self._run, name="parser-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._emit(status="stopping", message="Остановка после текущего шага…")

    def update_hashtags(self, hashtags: list[str]) -> tuple[list[str], list[str]]:
        new_tags = normalize_hashtags(hashtags)
        with self._lock:
            old = list(self.hashtags)
            old_keys = {t.casefold() for t in old}
            new_keys = {t.casefold() for t in new_tags}
            added = [t for t in new_tags if t.casefold() not in old_keys]
            removed = [t for t in old if t.casefold() not in new_keys]
            self.hashtags = new_tags
            self._pending_new_tags.extend(added)
        if added or removed:
            self._wake.set()
            parts = []
            if added:
                parts.append(
                    "добавлены: " + ", ".join(display_hashtag(t) for t in added)
                )
            if removed:
                parts.append(
                    "убраны: " + ", ".join(display_hashtag(t) for t in removed)
                )
            self._emit(message="Теги обновлены · " + " · ".join(parts))
        return added, removed

    def merge_subscriber_tags(self) -> list[str]:
        """GUI-теги + теги подписчиков из Telegram."""
        db = self._db or PostDatabase(self.settings.database_path)
        sub = db.all_subscribed_tags()
        with self._lock:
            base = list(self.hashtags)
        merged = normalize_hashtags(base + sub)
        with self._lock:
            old_keys = {t.casefold() for t in self.hashtags}
            added = [t for t in merged if t.casefold() not in old_keys]
            self.hashtags = merged
            self._pending_new_tags.extend(added)
        return merged

    def _current_hashtags(self) -> list[str]:
        with self._lock:
            return list(self.hashtags)

    def _pop_pending_new_tags(self) -> list[str]:
        with self._lock:
            pending = list(self._pending_new_tags)
            self._pending_new_tags.clear()
            return pending

    def _pending_new_tags_peek(self) -> bool:
        with self._lock:
            return bool(self._pending_new_tags)

    def _interruptible_sleep(self, seconds: float) -> None:
        self._wake.clear()
        end = time.time() + max(0.0, seconds)
        last_beat = 0.0
        while time.time() < end:
            if self._stop.is_set():
                return
            if self._wake.is_set():
                self._wake.clear()
                return
            now = time.time()
            if now - last_beat >= 20:
                remaining_min = max(1, int((end - now) / 60))
                heartbeat_touch(
                    self.stats.status or "sleeping",
                    extra=self.stats.message or f"пауза ещё ~{remaining_min} мин",
                    progress=True,
                )
                last_beat = now
            remaining = end - time.time()
            self._wake.wait(timeout=min(1.0, max(0.05, remaining)))

    def _deliver(
        self,
        sender: TelegramSender,
        db: PostDatabase,
        post: ThreadPost,
        tag: str,
    ) -> bool:
        """ЛС только тем из белого списка, кто нажал /start."""
        _ = tag
        allowed = set(getattr(self.settings, "allowed_tg_ids", ()) or ())
        allowed |= set(getattr(self.settings, "allowed_tg_ids", ()) or ())
        for key in ("alert_tg_id", "alert_tg_id"):
            try:
                extra = int(getattr(self.settings, key, 0) or 0)
            except (TypeError, ValueError):
                extra = 0
            if extra:
                allowed.add(extra)
        users = db.all_started_users()
        if allowed:
            users = [u for u in users if int(u.get("user_id", 0)) in allowed]
        else:
            # Пустой белый список = никому нельзя
            self._emit(
                message="⚠ Белый список пуст — добавь Telegram ID в программе"
            )
            return False
        if not users:
            self._emit(
                message=(
                    "⚠ Нет получателей: /start у разрешённых ID "
                    "или проверь белый список"
                )
            )
            return False

        dm_ok = 0
        already = 0
        for user in users:
            if db.user_already_got(user["user_id"], post.post_id):
                already += 1
                continue
            lead_id = db.save_lead(post)
            markup = lead_keyboard(lead_id) if lead_id else None
            if sender.send_post(post, chat_id=user["chat_id"], reply_markup=markup):
                db.mark_sent_to_user(user["user_id"], post.post_id)
                dm_ok += 1
                who = user.get("username") or str(user["user_id"])
                self._emit(
                    message=(
                        f"  · ЛС → @{who}"
                        if user.get("username")
                        else f"  · ЛС → {who}"
                    )
                )
            else:
                who = user.get("username") or str(user["user_id"])
                self._emit(message=f"  · не удалось ЛС → {who}")
            time.sleep(0.35)

        # Уже у всех в ЛС (после сброса только sent_posts) — не крутим dm_fail
        if dm_ok or already >= len(users):
            db.add(post.post_id, post.hashtag, post.url)
            if dm_ok:
                db.log_pipeline(post.hashtag or "", "sent", 1, post.post_id)
            return True
        db.log_pipeline(post.hashtag or "", "dm_fail", 1, post.post_id)
        return False

    def _seen(self, tag: str, post: ThreadPost, verdict: str) -> None:
        """Одна строка на пост: кого смотрел и что решил."""
        flat = " ".join((post.text or "").split())
        head = f"{display_hashtag(tag)} @{post.username} · {verdict}"
        url = getattr(post, "url", "") or ""
        # Тот же пост висит в Recent много циклов — в лог только раз в час
        key = f"{post.post_id}|{tag}|{verdict}"
        if not scan_log_once(key, f"{head} · «{flat[:240]}» · {url}"):
            return
        self._emit(message=f"  {head} · «{flat[:80]}»")

    def _filter_posts(
        self, posts: list[ThreadPost], tag: str
    ) -> list[ThreadPost]:
        require_ru = bool(getattr(self.settings, "require_russian", True))
        max_comments = int(getattr(self.settings, "max_comments", 0) or 0)
        if max_comments <= 0:
            max_comments = 40
        max_age_h = int(getattr(self.settings, "max_post_age_hours", 168) or 0)
        use_ai = self._ai is not None and self._ai.enabled
        # Дешёвый префильтр даже при DeepSeek: не кормим модель оффтопом.
        require_intent = bool(getattr(self.settings, "require_seo_intent", True))
        now = time.time()

        out: list[ThreadPost] = []
        reasons: dict[str, int] = {}
        fetched = len(posts)
        for post in posts:
            raw = post.text or ""
            text = scrub_post_text(raw)
            post = replace(post, text=text or raw)
            if self._db and post.post_id and self._db.exists(post.post_id):
                reasons["уже в базе"] = reasons.get("уже в базе", 0) + 1
                self._seen(tag, post, "✗ уже в базе")
                continue
            comments = int(getattr(post, "comments_count", 0) or 0)
            clear_hire = looks_like_clear_hire(text or raw)
            # Не режем длинные ветки — иначе теряем найм в топе Recent
            comment_limit = max(max_comments, 150) if clear_hire else max(max_comments, 100)
            if comments > comment_limit:
                key = f"комментариев > {comment_limit} ({comments})"
                reasons[key] = reasons.get(key, 0) + 1
                self._seen(tag, post, f"✗ {key}")
                continue
            posted_at = float(getattr(post, "posted_at", 0) or 0)
            if max_age_h > 0 and posted_at > 0:
                age_h = (now - posted_at) / 3600.0
                if age_h > max_age_h:
                    key = f"старше {max_age_h}ч ({int(age_h)}ч)"
                    reasons[key] = reasons.get(key, 0) + 1
                    self._seen(tag, post, f"✗ {key}")
                    continue
            ok, reason = post_passes_filters(
                raw,
                tag,
                require_russian=require_ru,
                require_seo_intent=require_intent,
            )
            if not ok:
                reasons[reason or "фильтр"] = reasons.get(reason or "фильтр", 0) + 1
                self._seen(tag, post, f"✗ {reason or 'фильтр'}")
                continue

            # Только явный найм SEO без DeepSeek. Всё остальное — через AI (precision).
            if use_ai and not clear_hire:
                ai_ok, ai_why = self._ai.is_good_lead(text or raw, tag)
                if "недоступен" in ai_why:
                    self._ai_fail_streak += 1
                else:
                    self._ai_fail_streak = 0
                if not ai_ok:
                    key = f"DeepSeek: {ai_why}"
                    reasons[key] = reasons.get(key, 0) + 1
                    self._seen(tag, post, f"✗ {key}")
                    continue
                self._seen(tag, post, f"✓ ЛИД · DeepSeek: {ai_why}")
            elif clear_hire:
                self._seen(tag, post, "✓ ЛИД · явный найм (без DeepSeek)")
            else:
                self._seen(tag, post, "✓ ЛИД · правила")

            out.append(post)
        if self._db:
            self._db.log_pipeline(tag, "fetched", fetched)
            for reason, n in reasons.items():
                self._db.log_pipeline(tag, "drop", n, reason)
            self._db.log_pipeline(tag, "passed", len(out))
        for reason, n in reasons.items():
            self._emit(
                message=f"{display_hashtag(tag)}: отброшено {n} · {reason}"
            )
        self._emit(
            message=(
                f"{display_hashtag(tag)}: снял {fetched} → "
                f"фильтр {len(out)}"
                + (" (DeepSeek)" if use_ai else " (правила)")
            )
        )
        return out

    def _run(self) -> None:
        db = PostDatabase(self.settings.database_path)
        self._db = db
        sender: TelegramSender | None = None
        fatal: str | None = None
        try:
            sender = TelegramSender(self.settings)
            bot_name = sender.verify_access()
            self._emit(
                message=(
                    f"✓ Telegram · бот {bot_name} · рассылка в ЛС "
                    "всем, кто нажал /start"
                )
            )
        except Exception as exc:
            fatal = str(exc)
            self._emit(status="error", message=fatal)
            self._alert_admin(sender, f"⛔ Старт не удался\n{fatal}")
            self.stats.status = "idle"
            if self.on_stats:
                self.on_stats(self.stats)
            return

        self.merge_subscriber_tags()
        tags_now = self._current_hashtags()
        if not tags_now:
            fatal = "Нет тегов. Добавь в GUI или в боте: /add SEO"
            self._emit(status="error", message=fatal)
            self._alert_admin(sender, f"⛔ Парсер остановлен\n{fatal}")
            self.stats.status = "idle"
            if self.on_stats:
                self.on_stats(self.stats)
            return

        self._emit(
            status="running",
            message=(
                f"▶ Старт · теги: {', '.join(display_hashtag(t) for t in tags_now)} · "
                f"свежие до {self.parse_posts} · каждые {self.interval_minutes} мин · "
                f"цель ≥{int(getattr(self.settings, 'lead_target_per_hour', 4) or 4)}/час "
                f"({int(getattr(self.settings, 'lead_target_per_day', 96) or 96)}/день) · "
                f"фильтр: RU + интент + "
                + (
                    f"DeepSeek под тег ({getattr(self.settings, 'deepseek_model', '?')})"
                    if self._ai and self._ai.enabled
                    else "запрос подрядчика"
                )
            ),
        )

        # headless из settings (GUI пишет в .env)
        try:
            with ThreadsScraper(self.settings) as scraper:
                scraper.set_parse_posts(self.parse_posts)
                # Сразу watch: без долгого «первого прохода» / bootstrap
                for tag in tags_now:
                    db.mark_tag_bootstrapped(tag)
                self._emit(
                    message=(
                        f"Быстрый режим · свежие ×{self.parse_posts} · "
                        f"без первого прохода · DeepSeek только если не явный найм"
                    )
                )

                while not self._stop.is_set():
                    self.merge_subscriber_tags()
                    pending = self._pop_pending_new_tags()
                    if pending:
                        db.reset_tags_bootstrap(pending)
                        cleared = db.clear_keywords_posts(pending)
                        self._emit(
                            message=(
                                "Новые теги — шлю их топ: "
                                + ", ".join(display_hashtag(t) for t in pending)
                                + f" (сброс: {cleared})"
                            )
                        )

                    self.stats.cycle += 1
                    self._emit(
                        status="scanning",
                        message=f"——— Цикл #{self.stats.cycle} ———",
                    )
                    just_bootstrapped = self._process_cycle(scraper, db, sender)
                    self._report_health(db, sender)
                    if self._stop.is_set():
                        break

                    if self._pending_new_tags_peek():
                        self._emit(message="Есть новые теги — сразу следующий цикл")
                        continue

                    # Threads ответил 429 — отходим надолго, иначе блок продлится
                    if getattr(scraper, "rate_limited_at", 0):
                        scraper.rate_limited_at = 0.0
                        self._rate_limit_hits += 1
                        pause_min = min(60, 15 * self._rate_limit_hits)
                        if self._throttle(db, "alert_429_ts", 1800):
                            self._alert_admin(
                                sender,
                                f"⚠ Threads лимитирует поиск (429). "
                                f"Пауза {pause_min} мин, потом продолжу сам.",
                            )
                        self._emit(
                            status="sleeping",
                            message=(
                                f"Threads лимитирует поиск (429) — пауза "
                                f"{pause_min} мин"
                            ),
                        )
                        self._interruptible_sleep(pause_min * 60)
                        continue
                    self._rate_limit_hits = 0

                    if just_bootstrapped:
                        self.stats.ready_to_publish = True
                        self._emit(
                            status="sleeping",
                            message=(
                                f"✓ Первый проход по свежим обработан. "
                                "Дальше только новые сверху выдачи…"
                            ),
                        )
                        self._interruptible_sleep(POST_BOOTSTRAP_PAUSE_SEC)
                    else:
                        # Не чаще, чем задан интервал: частые поиски = 429
                        wait_sec = max(60, self.interval_minutes * 60)
                        self._emit(
                            status="sleeping",
                            message=(
                                f"…пауза {wait_sec // 60} мин, потом новый проход"
                            ),
                        )
                        self._interruptible_sleep(wait_sec)
        except Exception as exc:
            logger.exception("Критическая ошибка воркера")
            fatal = f"Критическая ошибка: {exc}"
            self._emit(status="error", message=fatal)
            self._alert_admin(sender, f"⛔ Парсер упал\n{fatal}")
        finally:
            self.stats.status = "idle"
            self.stats.ready_to_publish = False
            stopped_by_user = self._stop.is_set() and not fatal
            self._emit(status="idle", message="Остановлено")
            if stopped_by_user:
                heartbeat_touch("stopped")
                self._alert_admin(
                    sender,
                    "⏹ Парсер остановлен (STOP / закрытие)",
                )

    def _alert_admin(self, sender: TelegramSender | None, text: str) -> None:
        """Уведомление на ALERT_TG_ID + ALERT_TG_EXTRA (@link_to_my_profile и др.)."""
        try:
            bot = sender or TelegramSender(self.settings)
            app = getattr(self.settings, "app_name", "Threads Posts ARTFrance")
            safe = escape_html(text)
            bot.send_alert(
                f"<b>{escape_html(app)}</b>\n{safe}",
                db=self._db,
            )
        except Exception as exc:
            logger.error("Алерт админу не ушёл: %s", exc)

    def _status_recipients(self, db: PostDatabase) -> list[int]:
        """Все, кто /start + в белом списке (как получатели лидов)."""
        allowed = set(getattr(self.settings, "allowed_tg_ids", ()) or ())
        try:
            extra = int(getattr(self.settings, "alert_tg_id", 0) or 0)
        except (TypeError, ValueError):
            extra = 0
        if extra:
            allowed.add(extra)
        users = db.all_started_users()
        out: list[int] = []
        seen: set[int] = set()
        for u in users:
            if not u.get("active", True):
                continue
            uid = int(u.get("user_id") or 0)
            cid = int(u.get("chat_id") or uid or 0)
            if not cid:
                continue
            if allowed and uid not in allowed:
                continue
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
        return out

    def _broadcast_users(
        self, sender: TelegramSender | None, db: PostDatabase, text: str, *, label: str
    ) -> None:
        try:
            bot = sender or TelegramSender(self.settings)
            app = getattr(self.settings, "app_name", "Threads Posts ARTFrance")
            body = f"<b>{escape_html(app)}</b>\n{text}"
            ids = self._status_recipients(db)
            if not ids:
                self._emit(message="⚠ Статус: нет получателей (/start + белый список)")
                return
            n = bot.send_broadcast(body, ids, label=label)
            self._emit(message=f"Статистика ушла {n}/{len(ids)} пользователям")
        except Exception as exc:
            logger.error("Рассылка статуса: %s", exc)

    def _maybe_hourly_status(
        self, db: PostDatabase, sender: TelegramSender | None
    ) -> None:
        interval = int(getattr(self.settings, "status_interval_minutes", 180) or 180)
        interval = max(30, interval)
        window_h = max(1.0, interval / 60.0)
        last = db.get_meta("hourly_status_ts") or "0"
        try:
            last_ts = float(last)
        except ValueError:
            last_ts = 0.0
        now = time.time()
        if last_ts and now - last_ts < interval * 60:
            return
        if not last_ts and self.stats.cycle < 1:
            return
        db.set_meta("hourly_status_ts", str(now))
        text = self._build_status_text(db, window_hours=window_h)
        self._emit(message=f"Отправляю статус за {int(window_h)}ч всем пользователям…")
        self._broadcast_users(sender, db, text, label="status3h")

    def _build_status_text(self, db: PostDatabase, *, window_hours: float = 3.0) -> str:
        hour_target = int(getattr(self.settings, "lead_target_per_hour", 4) or 4)
        day_target = int(getattr(self.settings, "lead_target_per_day", 96) or 96)
        window_h = max(1.0, float(window_hours))
        window_sent = db.sent_last_hours(window_h)
        day_sent = db.sent_today()
        funnel_w = db.funnel_last_hours(window_h)
        funnel_d = db.funnel_today()
        tags = ", ".join(display_hashtag(t) for t in self._current_hashtags()) or "—"
        top_drop = db.top_drop_reason_today() or "—"
        # цель на окно ≈ почасовая × часов
        window_target = max(1, int(round(hour_target * window_h)))
        pace_ok = window_sent >= window_target
        pace = "норма" if pace_ok else "ниже цели"
        wh = int(window_h) if abs(window_h - int(window_h)) < 0.05 else round(window_h, 1)
        return (
            f"⏱ <b>Статус за {wh} ч</b>\n"
            f"Статус воркера: {escape_html(self.stats.status)} · цикл #{self.stats.cycle}\n"
            f"Теги: {escape_html(tags)}\n\n"
            f"Заявки за {wh} ч: <b>{window_sent}/{window_target}</b> ({pace})\n"
            f"Заявки за день: <b>{day_sent}/{day_target}</b>\n\n"
            f"Воронка {wh}ч: снято {int(funnel_w.get('fetched') or 0)} · "
            f"отсев {int(funnel_w.get('drop') or 0)} · "
            f"прошло {int(funnel_w.get('passed') or 0)} · "
            f"ЛС {int(funnel_w.get('sent') or 0)}\n"
            f"Воронка день: снято {int(funnel_d.get('fetched') or 0)} · "
            f"отсев {int(funnel_d.get('drop') or 0)} · "
            f"прошло {int(funnel_d.get('passed') or 0)} · "
            f"ЛС {int(funnel_d.get('sent') or 0)}\n"
            f"Частый отсев: {escape_html(top_drop)}\n"
            f"Всего найдено/отправлено в сессии: "
            f"{self.stats.found}/{self.stats.sent}"
        )

    def _maybe_daily_report(
        self, db: PostDatabase, sender: TelegramSender | None
    ) -> None:
        """Раз в сутки утром — отчёт за вчера всем пользователям."""
        from datetime import datetime, timedelta

        report_hour = int(getattr(self.settings, "daily_report_hour", 9) or 9)
        now_local = datetime.now()
        if now_local.hour < report_hour:
            return
        yesterday = (now_local.date() - timedelta(days=1)).isoformat()
        last = (db.get_meta("daily_report_date") or "").strip()
        if last == yesterday:
            return
        day_target = int(getattr(self.settings, "lead_target_per_day", 96) or 96)
        sent = db.sent_on_local_date(yesterday)
        funnel = db.funnel_on_local_date(yesterday)
        top_drop = db.top_drop_reason_on_local_date(yesterday) or "—"
        tags = ", ".join(display_hashtag(t) for t in self._current_hashtags()) or "—"
        text = (
            f"📅 <b>Отчёт за {escape_html(yesterday)}</b>\n"
            f"Теги: {escape_html(tags)}\n\n"
            f"Заявки в ЛС: <b>{sent}</b> (цель дня {day_target})\n"
            f"Снято: {int(funnel.get('fetched') or 0)}\n"
            f"Отсев: {int(funnel.get('drop') or 0)}\n"
            f"Прошло фильтр: {int(funnel.get('passed') or 0)}\n"
            f"Ошибки ЛС: {int(funnel.get('dm_fail') or 0)}\n"
            f"Частый отсев: {escape_html(top_drop)}"
        )
        db.set_meta("daily_report_date", yesterday)
        self._emit(message=f"Утренний отчёт за {yesterday}…")
        self._broadcast_users(sender, db, text, label="dailyReport")

    def _throttle(self, db: PostDatabase, key: str, period_sec: float) -> bool:
        """True — можно алертить (не чаще раза в period_sec)."""
        try:
            last = float(db.get_meta(key) or 0)
        except (TypeError, ValueError):
            last = 0.0
        now = time.time()
        if now - last < period_sec:
            return False
        db.set_meta(key, str(now))
        return True

    def _alert_afk_breakage(
        self, db: PostDatabase, sender: TelegramSender | None
    ) -> None:
        """Две тихие поломки, которые в АФК выглядят как «лидов просто нет»."""
        if self._ai_fail_streak >= 5:
            if self._throttle(db, "alert_ai_down_ts", 1800):
                self._alert_admin(
                    sender,
                    "⚠ DeepSeek не отвечает — режим «только лиды» рубит все "
                    "посты, кроме явного найма. Проверь ключ/баланс API.",
                )
            self._emit(
                message=(
                    "⚠ DeepSeek не отвечает — проходит только явный найм "
                    f"(сбоев подряд: {self._ai_fail_streak})"
                )
            )
        if self._blind_cycles >= 3:
            if self._throttle(db, "alert_blind_ts", 1800):
                self._alert_admin(
                    sender,
                    f"⚠ {self._blind_cycles} цикла подряд с Threads снято 0 постов.\n"
                    "Похоже, слетела сессия Threads — нужен вход заново.",
                )
            self._emit(
                message=(
                    f"⚠ {self._blind_cycles} цикла подряд по 0 постов — "
                    "проверь сессию Threads"
                )
            )

    def _report_health(
        self, db: PostDatabase, sender: TelegramSender | None
    ) -> None:
        day_target = int(getattr(self.settings, "lead_target_per_day", 96) or 96)
        hour_target = int(getattr(self.settings, "lead_target_per_hour", 4) or 4)
        today = db.sent_today()
        hour = db.sent_last_hours(1.0)
        funnel = db.funnel_today()
        fetched = int(funnel.get("fetched") or 0)
        dropped = int(funnel.get("drop") or 0)
        passed = int(funnel.get("passed") or 0)
        self._emit(
            message=(
                f"Сводка: час {hour}/{hour_target} · день {today}/{day_target} · "
                f"снято {fetched} · отброшено {dropped} · прошло {passed}"
            )
        )
        self._maybe_hourly_status(db, sender)
        self._maybe_daily_report(db, sender)
        self._alert_afk_breakage(db, sender)

        last = db.get_meta("health_alert_ts") or "0"
        try:
            last_ts = float(last)
        except ValueError:
            last_ts = 0.0
        now = time.time()
        if now - last_ts < 2 * 3600:
            return
        if self.stats.cycle < 2:
            return
        if hour < hour_target and self.stats.cycle >= 3:
            db.set_meta("health_alert_ts", str(now))
            top_drop = db.top_drop_reason_today() or "фильтр"
            self._alert_admin(
                sender,
                f"⚠ За час {hour}/{hour_target} заявок "
                f"(цель ≥{hour_target}/час).\n"
                f"День: {today}/{day_target}. Частый отсев: {top_drop}",
            )
            return
        if fetched == 0 and self.stats.cycle >= 2:
            db.set_meta("health_alert_ts", str(now))
            self._alert_admin(
                sender,
                f"⚠ Парсер крутится, но с Threads снято 0 постов за сегодня.\n"
                f"Проверь вход в Threads и START. Цель {hour_target}/час.",
            )
            return
        if today == 0 and fetched > 0:
            db.set_meta("health_alert_ts", str(now))
            top_drop = db.top_drop_reason_today() or "фильтр"
            self._alert_admin(
                sender,
                f"⚠ Сегодня 0 заявок при {fetched} снятых постах "
                f"(цель {hour_target}/час).\nЧаще всего отсев: {top_drop}",
            )

    def _process_cycle(
        self,
        scraper: ThreadsScraper,
        db: PostDatabase,
        sender: TelegramSender,
    ) -> bool:
        any_bootstrap = False
        seen_this_cycle: set[str] = set()
        cycle_fetched = 0

        for tag in self._current_hashtags():
            if self._stop.is_set():
                return any_bootstrap
            if getattr(scraper, "rate_limited_at", 0):
                self._emit(message="429 от Threads — остальные теги в этом цикле пропускаю")
                break

            tag_ready = db.is_tag_bootstrapped(tag)
            self.stats.last_hashtag = display_hashtag(tag)
            if self.on_stats:
                self.on_stats(self.stats)

            limit = max(1, int(self.parse_posts))
            # Меньше снимаем — быстрее цикл; качество даёт hire-поиск, не объём #seo
            fetch_n = max(80, min(220, limit + 120))
            if not tag_ready:
                self._emit(
                    message=(
                        f"{display_hashtag(tag)}: первый проход — "
                        f"сниму {fetch_n} свежих, в ЛС до {limit}"
                    )
                )
                any_bootstrap = True

            try:
                posts = scraper.search_hashtag(
                    tag,
                    limit=fetch_n,
                    watch=True,
                    on_progress=lambda msg: self._emit(message=msg),
                )
            except Exception as exc:
                self._emit(
                    message=f"{display_hashtag(tag)}: ошибка поиска — {exc}"
                )
                continue

            cycle_fetched += len(posts)
            if scraper.my_username:
                me = scraper.my_username.casefold()
                posts = [p for p in posts if p.username.casefold() != me]

            posts = self._filter_posts(posts, tag)

            uniq: list[ThreadPost] = []
            seen_ids: set[str] = set()
            for p in posts:
                if p.post_id in seen_ids:
                    continue
                seen_ids.add(p.post_id)
                uniq.append(p)
            posts = uniq
            self.stats.found += len(posts)

            if not posts:
                self._emit(
                    message=(
                        f"{display_hashtag(tag)}: подходящих постов 0 "
                        "(после фильтров)"
                    )
                )
                if not tag_ready:
                    db.mark_tag_bootstrapped(tag)
                continue

            if not tag_ready:
                sent_n = 0
                remembered = 0
                for post in posts:
                    if self._stop.is_set():
                        return any_bootstrap
                    if post.post_id in seen_this_cycle:
                        continue
                    seen_this_cycle.add(post.post_id)
                    if db.exists(post.post_id):
                        continue
                    if sent_n >= limit:
                        db.add(post.post_id, post.hashtag, post.url)
                        remembered += 1
                        continue
                    ok = self._deliver(sender, db, post, tag)
                    if ok:
                        self.stats.sent += 1
                        sent_n += 1
                        self._emit(
                            message=(
                                f"→ [{sent_n}/{limit}] @{post.username} · "
                                f"{display_hashtag(tag)}"
                            )
                        )
                    else:
                        seen_this_cycle.discard(post.post_id)
                        self._emit(
                            message=f"✗ не отправилось @{post.username}"
                        )
                    time.sleep(0.25)
                    if self.on_stats:
                        self.on_stats(self.stats)

                db.mark_tag_bootstrapped(tag)
                self._emit(
                    message=(
                        f"{display_hashtag(tag)}: готово — в ЛС ушло {sent_n}/{limit}, "
                        f"запомнено {remembered}. Жду только новые."
                    )
                )
                continue

            send_cap = max(limit, 40)
            posts = posts[:send_cap]
            new_count = 0
            skipped = 0
            for post in posts:
                if self._stop.is_set():
                    return any_bootstrap
                if post.post_id in seen_this_cycle or db.exists(post.post_id):
                    self.stats.skipped += 1
                    skipped += 1
                    continue
                seen_this_cycle.add(post.post_id)
                ok = self._deliver(sender, db, post, tag)
                if ok:
                    self.stats.sent += 1
                    new_count += 1
                    self._emit(
                        message=f"NEW → @{post.username} · {display_hashtag(tag)}"
                    )
                else:
                    seen_this_cycle.discard(post.post_id)
                    self._emit(message=f"✗ NEW fail @{post.username}")
                time.sleep(0.25)
                if self.on_stats:
                    self.on_stats(self.stats)

            if new_count == 0:
                self._emit(
                    message=(
                        f"{display_hashtag(tag)}: новых нет "
                        f"(старых {skipped}) — жду"
                    )
                )
            else:
                self._emit(
                    message=f"{display_hashtag(tag)}: новых отправлено {new_count}"
                )

        if cycle_fetched == 0:
            self._blind_cycles += 1
        else:
            self._blind_cycles = 0
        return any_bootstrap
