"""Парсинг топиков/хэштегов Threads через Playwright + сохранённая сессия."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

from nested_lookup import nested_lookup
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from config import Settings
from hashtags import (
    build_recent_search_url,
    clean_post_text,
    display_hashtag,
    normalize_hashtag,
    post_matches_tag,
    query_fetch_cap,
    search_queries_for_tag,
    text_mentions_tag,
)
from models import ThreadPost, human_delay, logger
from scan_log import write as scan_log
from playwright_env import fix_playwright_browsers_path
from session_store import SESSION_PATH, load_username, save_username, session_exists

SELECTORS = {
    "post_container": "[data-pressable-container='true']",
    "post_link": "a[href*='/post/']",
    "cookie_accept": (
        "button:has-text('Accept all'), button:has-text('Accept All'), "
        "button:has-text('Accept'), button:has-text('Разрешить'), "
        "button:has-text('Allow')"
    ),
}

POST_LINK_RE = re.compile(r"/@(?P<user>[\w.]+)/post/(?P<code>[\w-]+)")
LOGIN_WAIT_SEC = 300  # 5 минут на ручной вход


SSO_CONTINUE_RE = re.compile(
    r"Продолжить с аккаунтом Instagram|Continue with Instagram",
    re.I,
)
ONBOARDING_LATER_RE = re.compile(
    r"^(Не сейчас|Not now|Skip|Пропустить|Maybe later)$",
    re.I,
)


def soft_cookie_banner(page: Page) -> None:
    """Только cookie-баннер — без кликов по углам и без сноса диалогов входа."""
    try:
        btn = page.query_selector(SELECTORS["cookie_accept"])
        if btn and btn.is_visible():
            btn.click(timeout=1000)
            page.wait_for_timeout(400)
    except Exception:
        pass


def looks_like_threads_onboarding(page: Page) -> bool:
    """Instagram уже выбран, Threads просит один клик «Продолжить»."""
    try:
        html = (page.content() or "").lower()
    except Exception:
        return False
    return any(
        m in html
        for m in (
            "попробуйте threads",
            "try threads",
            "продолжить с аккаунтом instagram",
            "continue with instagram account",
        )
    )


def complete_threads_onboarding(page: Page) -> bool:
    """Закрыть модалку SSO: аккаунт Instagram уже есть, нужен клик Continue."""
    clicked = False
    for _ in range(8):
        soft_cookie_banner(page)
        try:
            btn = page.get_by_text(SSO_CONTINUE_RE)
            n = btn.count()
            if n == 0:
                btn = page.locator(
                    "div[role='button'], button, a, [role='link']"
                ).filter(has_text=SSO_CONTINUE_RE)
                n = btn.count()
            if n > 0:
                target = btn.last
                if target.is_visible():
                    target.click(timeout=4000)
                    clicked = True
                    logger.info("Нажал «Продолжить с Instagram» — подтверждаю Threads")
                    page.wait_for_timeout(2200)
                    continue
        except Exception:
            pass
        try:
            later = page.locator("div[role='button'], button").filter(
                has_text=ONBOARDING_LATER_RE
            )
            if later.count() > 0 and later.first.is_visible():
                later.first.click(timeout=2000)
                clicked = True
                logger.info("Закрыл доп. экран Threads (Не сейчас / Skip)")
                page.wait_for_timeout(800)
                continue
        except Exception:
            pass
        if not looks_like_threads_onboarding(page):
            break
        page.wait_for_timeout(400)
    return clicked


def open_recent_tab(page: Page) -> bool:
    """Вкладка Recent / Недавние — иначе виден только популярный топ."""
    patterns = (
        re.compile(r"^(Recent|Latest|Недавние|Свежие|Новые)$", re.I),
        re.compile(r"Recent|Недавние", re.I),
    )
    for pat in patterns:
        try:
            tab = page.get_by_role("tab", name=pat)
            if tab.count() and tab.first.is_visible():
                tab.first.click(timeout=2500)
                page.wait_for_timeout(1400)
                logger.info("Переключил выдачу на Recent")
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(pat)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2500)
                page.wait_for_timeout(1400)
                logger.info("Кликнул «Recent» в поиске")
                return True
        except Exception:
            pass
    return False


def prepare_threads_page(page: Page) -> None:
    soft_cookie_banner(page)
    complete_threads_onboarding(page)
    soft_cookie_banner(page)


def looks_like_login_wall(page: Page) -> bool:
    if looks_like_threads_onboarding(page):
        return False
    try:
        html = (page.content() or "").lower()
    except Exception:
        return True
    markers = (
        "log in to threads",
        "log in with instagram",
        "sign up for threads",
        "войдите в threads",
        "войти через instagram",
    )
    if any(m in html for m in markers):
        try:
            if page.query_selector(SELECTORS["post_container"]):
                return False
        except Exception:
            pass
        return True
    return False


def context_has_auth(context: BrowserContext) -> bool:
    try:
        cookies = context.cookies()
    except Exception:
        return False
    names = {c.get("name") for c in cookies}
    return "sessionid" in names or "ds_user_id" in names


def _best_image_url(candidates: list[dict] | None) -> str | None:
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda c: (c.get("width") or 0) * (c.get("height") or 0),
        reverse=True,
    )
    url = ranked[0].get("url")
    return url if url else None


def _extract_single_image(post: dict) -> str | None:
    """Первая картинка поста (в т.ч. из карусели). Видео без превью → None."""
    if post.get("video_versions"):
        # иногда у видео есть превью
        preview = _best_image_url((post.get("image_versions2") or {}).get("candidates"))
        return preview

    carousel = post.get("carousel_media") or []
    if carousel:
        media = carousel[0]
        if media.get("video_versions"):
            return _best_image_url(
                (media.get("image_versions2") or {}).get("candidates")
            )
        return _best_image_url(
            (media.get("image_versions2") or {}).get("candidates")
        )
    return _best_image_url((post.get("image_versions2") or {}).get("candidates"))


def _is_reply_or_repost(item: dict, post: dict) -> bool:
    info = post.get("text_post_app_info") or {}
    if info.get("is_reply") or info.get("reply_to_author"):
        return True
    if info.get("repost_post") or info.get("repost_info"):
        return True
    if item.get("is_reposted_by_viewer"):
        return True
    return False


def _extract_comments_count(post: dict) -> int:
    """Пробуем вытащить число комментариев из разных полей Threads."""
    candidates = [
        post.get("reply_count"),
        post.get("direct_reply_count"),
        post.get("reply_count_text"),
        post.get("comment_count"),
        post.get("comments_count"),
    ]
    info = post.get("text_post_app_info") or {}
    candidates.extend(
        [
            info.get("reply_count"),
            info.get("direct_reply_count"),
            info.get("comment_count"),
        ]
    )
    for value in candidates:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _extract_posted_at(post: dict) -> float:
    """Время публикации (unix). 0 если нет в payload."""
    for key in ("taken_at", "taken_at_timestamp", "device_timestamp", "created_at"):
        raw = post.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        # иногда приходит в миллисекундах
        if val > 1e12:
            val /= 1000.0
        if val > 1e9:
            return val
    caption = post.get("caption")
    if isinstance(caption, dict):
        for key in ("created_at", "created_at_utc"):
            raw = caption.get(key)
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val > 1e12:
                val /= 1000.0
            if val > 1e9:
                return val
    return 0.0


_UI_AGE_RE = re.compile(
    r"(?iu)(?:^|\n)\s*(\d+)\s*"
    r"(мин\.?|минуту|минуты|минут|minutes?|mins?|min|m|"
    r"ч\.?|час|часа|часов|hours?|hrs?|h|"
    r"д\.?|день|дня|дней|days?|d|"
    r"нед\.?|weeks?|w|"
    r"мес\.?|months?)\b"
)
_UI_REPLY_RE = re.compile(
    r"(?iu)(\d+)\s*(?:ответ|ответа|ответов|repl(?:y|ies)|коммент)"
)


def _guess_posted_at_from_ui(text: str) -> float:
    """Оценка возраста из UI Threads («3д», «5h»)."""
    m = _UI_AGE_RE.search(text or "")
    if not m:
        return 0.0
    n = int(m.group(1))
    unit = m.group(2).casefold()
    if unit.startswith(("мин", "min", "m")) and not unit.startswith("мес"):
        sec = n * 60
    elif unit.startswith(("ч", "h", "hour")):
        sec = n * 3600
    elif unit.startswith(("д", "d", "day", "день", "дня", "дней")):
        sec = n * 86400
    elif unit.startswith(("нед", "w", "week")):
        sec = n * 7 * 86400
    elif unit.startswith(("мес", "month")):
        sec = n * 30 * 86400
    else:
        return 0.0
    return time.time() - sec


def _guess_comments_from_ui(text: str) -> int:
    m = _UI_REPLY_RE.search(text or "")
    if not m:
        return 0
    try:
        return max(0, int(m.group(1)))
    except ValueError:
        return 0


def parse_thread_item(
    item: dict,
    hashtag: str,
    *,
    from_tag_search: bool = False,
    own_username: str | None = None,
) -> ThreadPost | None:
    post = item.get("post") or {}
    if not post or _is_reply_or_repost(item, post):
        return None

    user = post.get("user") or {}
    username = user.get("username")
    code = post.get("code")
    if not username or not code:
        return None

    # Не берём посты СВОЕГО аккаунта — только чужие
    if own_username and username.casefold() == own_username.casefold():
        return None

    post_id = f"{username}:{code}"

    caption = post.get("caption") or {}
    text = (caption.get("text") if isinstance(caption, dict) else None) or ""
    text = clean_post_text(text.strip())

    if not post_matches_tag(
        text,
        post,
        hashtag,
        from_tag_search=from_tag_search,
        own_post=False,
    ):
        return None

    image_url = _extract_single_image(post)
    if not text and not image_url:
        return None

    return ThreadPost(
        post_id=post_id,
        code=code,
        username=username,
        text=text,
        url=f"https://www.threads.com/@{username}/post/{code}",
        image_url=image_url,
        hashtag=normalize_hashtag(hashtag),
        comments_count=_extract_comments_count(post),
        posted_at=_extract_posted_at(post),
    )


def _collect_from_thread_items(
    payload: Any,
    hashtag: str,
    *,
    from_tag_search: bool = False,
    own_username: str | None = None,
) -> tuple[list[ThreadPost], int]:
    posts: list[ThreadPost] = []
    seen: set[str] = set()
    raw_groups = 0
    for group in nested_lookup("thread_items", payload):
        if not isinstance(group, list) or not group:
            continue
        raw_groups += 1
        parsed = parse_thread_item(
            group[0],
            hashtag,
            from_tag_search=from_tag_search,
            own_username=own_username,
        )
        if parsed and parsed.post_id not in seen:
            seen.add(parsed.post_id)
            posts.append(parsed)
    return posts, raw_groups


def _collect_from_dom(
    page: Page,
    hashtag: str,
    *,
    own_username: str | None = None,
    from_tag_search: bool = False,
) -> list[ThreadPost]:
    posts: list[ThreadPost] = []
    seen: set[str] = set()
    tag = normalize_hashtag(hashtag)
    own = (own_username or "").casefold()
    for anchor in page.query_selector_all(SELECTORS["post_link"]):
        href = anchor.get_attribute("href") or ""
        match = POST_LINK_RE.search(href)
        if not match:
            continue
        username = match.group("user")
        code = match.group("code")
        if own and username.casefold() == own:
            continue
        post_id = f"{username}:{code}"
        if post_id in seen:
            continue
        seen.add(post_id)

        text = ""
        try:
            container = anchor.evaluate_handle(
                """el => el.closest("[data-pressable-container='true']") || el.parentElement"""
            )
            text = (container.as_element().inner_text() or "").strip()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if lines and lines[0].lstrip("@").lower() == username.lower():
                lines = lines[1:]
            text = "\n".join(lines[:12]).strip()
        except Exception:
            text = ""

        posted_at = _guess_posted_at_from_ui(text)
        comments_guess = _guess_comments_from_ui(text)
        body = clean_post_text(text)
        if not from_tag_search:
            if not text_mentions_tag(body, tag) and not text_mentions_tag(text, tag):
                continue

        image_url = None
        try:
            container_el = anchor.evaluate_handle(
                """el => el.closest("[data-pressable-container='true']") || el.parentElement"""
            ).as_element()
            imgs = container_el.query_selector_all("img") if container_el else []
            candidates = []
            for img in imgs:
                src = img.get_attribute("src") or ""
                if src.startswith("http") and "profile" not in src and "150x150" not in src:
                    candidates.append(src)
            if candidates:
                image_url = candidates[0]
        except Exception:
            pass

        if not body and not image_url:
            continue

        posts.append(
            ThreadPost(
                post_id=post_id,
                code=code,
                username=username,
                text=body,
                url=f"https://www.threads.com/@{username}/post/{code}",
                image_url=image_url,
                hashtag=tag,
                comments_count=comments_guess,
                posted_at=posted_at,
            )
        )
    return posts


def detect_username(page: Page) -> str | None:
    """Пытается понять, под каким @ мы залогинены."""
    try:
        hrefs = page.eval_on_selector_all(
            'a[href^="/@"]',
            "els => els.map(e => e.getAttribute('href') || '')",
        )
    except Exception:
        hrefs = []
    counts: dict[str, int] = {}
    for href in hrefs or []:
        m = re.match(r"/@([\w.]+)/?$", href or "")
        if not m:
            continue
        user = m.group(1)
        if user.lower() in {"threads", "instagram"}:
            continue
        counts[user] = counts.get(user, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


class ThreadsScraper:
    def __init__(self, settings: Settings, force_login: bool = False) -> None:
        self.settings = settings
        self.force_login = force_login
        self.my_username: str | None = (
            getattr(settings, "threads_username", None) or load_username()
        )
        self.parse_posts = max(
            1, min(200, int(getattr(settings, "parse_posts", 40) or 40))
        )
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # Когда Threads последний раз ответил 429 на поиск
        self.rate_limited_at = 0.0

    def set_parse_posts(self, value: int) -> None:
        self.parse_posts = max(1, min(200, int(value)))

    def _set_username(self, username: str | None) -> None:
        if not username:
            return
        self.my_username = username.strip().lstrip("@")
        save_username(self.my_username)
        logger.info("Аккаунт Threads: @%s", self.my_username)

    def start(self) -> None:
        fix_playwright_browsers_path()
        self._pw = sync_playwright().start()
        need_login = self.force_login or not session_exists()
        # Для входа окно обязательно видимо; иначе — как в настройке
        headless = False if need_login else bool(self.settings.playwright_headless)
        logger.info(
            "Браузер: %s (headless=%s)",
            "скрытый" if headless else "видимый",
            headless,
        )

        from playwright_env import CHROME_ARGS

        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=list(CHROME_ARGS),
        )

        context_kwargs = {
            "user_agent": self.settings.user_agent,
            "viewport": {"width": 1365, "height": 900},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if session_exists() and not self.force_login:
            context_kwargs["storage_state"] = str(SESSION_PATH)
            logger.info("Загружаю сохранённую сессию: %s", SESSION_PATH.name)

        self._context = self._browser.new_context(**context_kwargs)
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        # Доп. cookies из .env — только если уже есть сессия (при входе мешают)
        if self.settings.threads_cookies and not need_login:
            try:
                self._context.add_cookies(self.settings.threads_cookies)
            except Exception as exc:
                logger.warning("Cookies из .env не применились: %s", exc)

        if need_login:
            self._interactive_login()
        else:
            self._verify_saved_session()

    def _save_session(self) -> None:
        assert self._context is not None
        self._context.storage_state(path=str(SESSION_PATH))
        logger.info(
            "Сессия сохранена в %s — при следующем запуске вход не нужен. "
            "Чтобы перенести на другой ПК, скопируй этот файл вместе с программой.",
            SESSION_PATH,
        )

    def _interactive_login(self) -> None:
        assert self._context is not None
        page = self._context.new_page()
        try:
            logger.info("=" * 60)
            logger.info("ВХОД В THREADS")
            logger.info("1) Сначала откроется Instagram — войди логином/паролем")
            logger.info("2) Потом откроется Threads (сессия подтянется сама)")
            logger.info("3) Дождись ленты — программа сохранит сессию (до %s мин)", LOGIN_WAIT_SEC // 60)
            logger.info("=" * 60)

            # threads.com/login часто показывает только QR «скачай приложение».
            # Надёжнее: войти через Instagram web, затем открыть Threads.
            page.goto(
                "https://www.instagram.com/accounts/login/",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            soft_cookie_banner(page)

            deadline = time.time() + LOGIN_WAIT_SEC
            opened_threads = False
            while time.time() < deadline:
                if context_has_auth(self._context) and not opened_threads:
                    opened_threads = True
                    logger.info("Instagram-сессия есть — открываю Threads…")
                    try:
                        page.goto(
                            "https://www.threads.com/",
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    except Exception as exc:
                        logger.warning("Не удалось открыть Threads: %s", exc)
                    prepare_threads_page(page)
                    human_delay(1.0, 2.0)

                url = (page.url or "").lower()
                on_threads = "threads." in url
                if on_threads:
                    prepare_threads_page(page)
                if (
                    context_has_auth(self._context)
                    and on_threads
                    and not looks_like_login_wall(page)
                    and not looks_like_threads_onboarding(page)
                ):
                    self._set_username(detect_username(page) or self.my_username)
                    self._save_session()
                    logger.info("Готово: аккаунт сохранён (@%s).", self.my_username or "?")
                    return

                page.wait_for_timeout(2000)

            # Последняя попытка сохранить, если cookies уже есть
            if context_has_auth(self._context):
                try:
                    page.goto(
                        "https://www.threads.com/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    prepare_threads_page(page)
                    self._set_username(detect_username(page) or self.my_username)
                except Exception:
                    pass
                self._save_session()
                logger.info("Сессия сохранена по cookies (таймаут ожидания ленты).")
                return

            raise RuntimeError(
                "Вход не выполнен за отведённое время. Нажми «Войти в Threads» ещё раз."
            )
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _verify_saved_session(self) -> None:
        assert self._context is not None
        page = self._context.new_page()
        try:
            page.goto("https://www.threads.net/", wait_until="domcontentloaded", timeout=60_000)
            prepare_threads_page(page)
            human_delay(1.5, 2.5)
            if looks_like_login_wall(page) or not context_has_auth(self._context):
                logger.warning("Сохранённая сессия устарела — нужен повторный вход.")
                page.close()
                self._interactive_login()
                return
            # Обновляем файл сессии свежими cookies
            self._set_username(detect_username(page) or self.my_username)
            self._save_session()
            logger.info("Сессия Threads активна (@%s).", self.my_username or "?")
        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass

    def stop(self) -> None:
        # Перед закрытием ещё раз сохраняем cookies
        if self._context:
            try:
                if context_has_auth(self._context):
                    self._context.storage_state(path=str(SESSION_PATH))
            except Exception:
                pass
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._context = None
        self._browser = None
        self._pw = None

    def __enter__(self) -> "ThreadsScraper":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def search_hashtag(
        self,
        hashtag: str,
        *,
        limit: int | None = None,
        watch: bool = False,
        on_progress=None,
    ) -> list[ThreadPost]:
        if not self._context:
            raise RuntimeError("Скрейпер не запущен.")

        tag = normalize_hashtag(hashtag)
        target = max(1, min(250, int(limit if limit is not None else self.parse_posts)))
        page = self._context.new_page()
        # Быстрый цикл: меньше страниц, hire-запросы первые
        deadline = time.time() + (150 if watch else 200)
        try:
            merged: dict[str, ThreadPost] = {}
            order: list[str] = []
            queries = search_queries_for_tag(tag)
            for qi, (label, query) in enumerate(queries):
                if self.rate_limited_at:
                    if on_progress:
                        on_progress(f"#{tag}: Threads лимитирует поиск (429) — пауза")
                    break
                if qi:
                    # Пауза между поисками: без неё Threads выдаёт 429
                    page.wait_for_timeout(random.randint(2500, 5000))
                if time.time() >= deadline:
                    if on_progress:
                        on_progress(f"#{tag}: дедлайн, собрано {len(merged)}")
                    break
                if len(merged) >= target:
                    break
                if on_progress:
                    on_progress(
                        f"#{tag}: Recent {qi + 1}/{len(queries)} «{label}» → {len(merged)}/{target}"
                    )
                url = build_recent_search_url(query)
                cap = query_fetch_cap(query)
                want = min(cap, max(0, target - len(merged)))
                if want <= 0:
                    break
                # Hire-фразы — сразу целимся в лимит
                if qi < 3 or not query.strip().startswith("#"):
                    want = min(cap, max(want, min(25, target - len(merged))))
                try:
                    # Тематику задаёт сам запрос: «ищу сеошника» без слова SEO
                    # раньше молча выбрасывалось здесь, не доходя до фильтра.
                    chunk = self._search_on_page(
                        page,
                        tag,
                        url,
                        label,
                        from_tag_search=True,
                        target_count=want,
                        watch=watch,
                    )
                except Exception as exc:
                    logger.warning("Recent «%s» сорвался: %s", label, str(exc)[:160])
                    chunk = []
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        page = self._context.new_page()
                    except Exception:
                        break
                for post in chunk:
                    if post.post_id not in merged:
                        post.hashtag = tag
                        merged[post.post_id] = post
                        order.append(post.post_id)
                logger.info(
                    "Recent «%s»: +%s, всего %s/%s",
                    label,
                    len(chunk),
                    len(merged),
                    target,
                )
                scan_log(
                    f"ЗАПРОС {display_hashtag(tag)} «{query}» → +{len(chunk)} "
                    f"(всего {len(merged)}/{target})"
                )

            result = [merged[pid] for pid in order][:target]
            logger.info(
                "Постов по %s: %s (лимит=%s, max-recent)",
                display_hashtag(tag),
                len(result),
                target,
            )
            return result

        finally:
            try:
                page.close()
            except Exception:
                pass

    def _search_on_page(
        self,
        page: Page,
        tag: str,
        url: str,
        label: str,
        *,
        from_tag_search: bool = False,
        target_count: int = 5,
        watch: bool = False,
    ) -> list[ThreadPost]:
        """Верх выдачи Recent. watch=быстрее (меньше пауз и скроллов)."""
        by_id: dict[str, ThreadPost] = {}
        raw_groups_total = 0
        own = self.my_username
        fast = bool(watch)
        pause_short = 50 if fast else 180
        pause_mid = 80 if fast else 280
        # Быстрее: меньше скроллов — как ручной топ Recent
        max_scrolls = max(4, min(10, target_count // 5 + 3))
        url_deadline = time.time() + (28 if fast else 40)

        def on_response(response) -> None:
            nonlocal raw_groups_total
            try:
                req_url = response.url
                if "graphql" not in req_url and "/api/" not in req_url:
                    return
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype and "javascript" not in ctype:
                    return
                data = response.json()
            except Exception:
                return
            posts, raw_n = _collect_from_thread_items(
                data,
                tag,
                from_tag_search=from_tag_search,
                own_username=own,
            )
            raw_groups_total += raw_n
            for post in posts:
                by_id.setdefault(post.post_id, post)

        def ordered_top() -> list[ThreadPost]:
            ordered: list[ThreadPost] = []
            seen: set[str] = set()
            try:
                for post in _collect_from_dom(
                    page, tag, own_username=own, from_tag_search=from_tag_search
                ):
                    full = by_id.get(post.post_id, post)
                    if full.post_id in seen:
                        continue
                    seen.add(full.post_id)
                    ordered.append(full)
                    if len(ordered) >= target_count:
                        break
            except Exception as exc:
                logger.error("Ошибка DOM: %s", exc)
            if len(ordered) < target_count:
                for post in by_id.values():
                    if post.post_id in seen:
                        continue
                    seen.add(post.post_id)
                    ordered.append(post)
                    if len(ordered) >= target_count:
                        break
            return ordered[:target_count]

        page.on("response", on_response)
        try:
            logger.info("Открываю ТОП (%s): %s", label, url)
            last_goto: Exception | None = None
            for attempt, wait_until in enumerate(
                ("commit", "domcontentloaded"), start=1
            ):
                try:
                    resp = page.goto(url, wait_until=wait_until, timeout=18_000)
                    if resp is not None and resp.status == 429:
                        # Threads зарейтлимитил поиск — дальше долбить бессмысленно
                        self.rate_limited_at = time.time()
                        logger.warning("HTTP 429 на «%s» — поиск лимитирован", label)
                        return []
                    last_goto = None
                    break
                except Exception as exc:
                    last_goto = exc
                    msg = str(exc)
                    logger.warning(
                        "goto сбой %s/%s (%s): %s",
                        attempt,
                        3,
                        label,
                        msg[:160],
                    )
                    if "QUIC" in msg or "net::ERR_" in msg:
                        try:
                            page.wait_for_timeout(1200 * attempt)
                        except Exception:
                            pass
                        continue
                    if attempt < 3:
                        try:
                            page.wait_for_timeout(800)
                        except Exception:
                            pass
                        continue
                    raise
            if last_goto is not None:
                raise last_goto

            soft_cookie_banner(page)
            # URL уже с filter=recent — не кликаем вкладку (экономия 2–4с)
            if "filter=recent" not in (url or "").casefold():
                try:
                    open_recent_tab(page)
                except Exception:
                    pass
            page.wait_for_timeout(pause_mid)
            logger.info("URL: %s", page.url)

            if looks_like_login_wall(page):
                logger.warning("Экран входа на «%s»", label)
                return []

            containers = 0
            try:
                page.wait_for_selector(
                    SELECTORS["post_container"],
                    timeout=6_000 if fast else 12_000,
                )
                containers = len(page.query_selector_all(SELECTORS["post_container"]))
            except Exception:
                logger.warning("Карточки не появились (%s)", label)

            page.wait_for_timeout(pause_short)

            try:
                html = page.content()
                embedded, raw_n = self._parse_embedded_json(
                    html, tag, from_tag_search=from_tag_search
                )
                raw_groups_total += raw_n
                for post in embedded:
                    by_id.setdefault(post.post_id, post)
            except Exception as exc:
                logger.error("Ошибка JSON: %s", exc)

            result = ordered_top()
            scrolls = 0
            stall = 0
            prev_n = len(result)
            while len(result) < target_count and scrolls < max_scrolls:
                if time.time() >= url_deadline:
                    logger.info("URL-дедлайн «%s» — %s постов", label, len(result))
                    break
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    page.mouse.wheel(0, random.randint(900, 1600))
                page.wait_for_timeout(220 if fast else 350)
                scrolls += 1
                try:
                    html = page.content()
                    embedded, raw_n = self._parse_embedded_json(
                        html, tag, from_tag_search=from_tag_search
                    )
                    raw_groups_total += raw_n
                    for post in embedded:
                        by_id.setdefault(post.post_id, post)
                except Exception:
                    pass
                result = ordered_top()
                if len(result) <= prev_n:
                    stall += 1
                    if stall >= 3:
                        break
                else:
                    stall = 0
                    prev_n = len(result)

            logger.info(
                "Свежие «%s»: %s/%s, DOM≈%s, raw≈%s, scroll=%s",
                label,
                len(result),
                target_count,
                containers,
                raw_groups_total,
                scrolls,
            )
            return result[:target_count]
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    def _parse_embedded_json(
        self,
        html: str,
        hashtag: str,
        *,
        from_tag_search: bool = False,
    ) -> tuple[list[ThreadPost], int]:
        posts: list[ThreadPost] = []
        seen: set[str] = set()
        raw_total = 0
        pattern = re.compile(
            r'<script[^>]*type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(html):
            raw = match.group(1).strip()
            if "thread_items" not in raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            chunk, raw_n = _collect_from_thread_items(
                data,
                hashtag,
                from_tag_search=from_tag_search,
                own_username=self.my_username,
            )
            raw_total += raw_n
            for post in chunk:
                if post.post_id not in seen:
                    seen.add(post.post_id)
                    posts.append(post)
        return posts, raw_total


def run_login_only(settings: Settings) -> None:
    """Отдельный режим: только войти и сохранить сессию."""
    with ThreadsScraper(settings, force_login=True):
        pass
