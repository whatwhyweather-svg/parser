"""Публикация комментария в Threads из сохранённой сессии Playwright.

На странице поста Threads уже есть inline-поле «Reply to …».
Текст-only ответ уходит Ctrl+Enter — отдельной кнопки Post в этом поле нет.
Модалка [role=dialog] открывается только если нажать Reply / Expand.
"""

from __future__ import annotations

import re
import threading
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from config import Settings
from models import logger
from playwright_env import fix_playwright_browsers_path
from scraper import looks_like_login_wall, prepare_threads_page
from session_store import SESSION_PATH, session_exists

_LOCK = threading.Lock()

SEND_TEXT = re.compile(
    r"^(Post|Reply|Ответить|Опубликовать|Send|Отправить)$", re.I
)
HOME_COMPOSER_RE = re.compile(
    r"what's new|start a thread|what.s on your mind|что нового|начать ветк",
    re.I,
)
REPLY_PLACEHOLDER_RE = re.compile(
    r"reply|ответ|comment|коммент", re.I
)
INTERSTITIAL_RE = re.compile(
    r"^(Not now|Не сейчас|Continue|Продолжить|Close|Закрыть|"
    r"Dismiss|Allow all|Разрешить все)$",
    re.I,
)


def _url_variants(url: str) -> list[str]:
    raw = (url or "").strip()
    if not raw:
        return []
    out = [raw]
    for a, b in (
        ("threads.net", "threads.com"),
        ("threads.com", "threads.net"),
        ("www.threads.net", "www.threads.com"),
        ("www.threads.com", "www.threads.net"),
    ):
        if a in raw:
            out.append(raw.replace(a, b))
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def post_comment(url: str, text: str, settings: Settings) -> str:
    """Пустая строка = ок, иначе текст ошибки."""
    body = (text or "").strip()
    if not body:
        return "пустой комментарий"
    if not url:
        return "нет ссылки на пост"
    if not session_exists():
        return "нет сессии Threads — сначала «Войти в Threads»"

    with _LOCK:
        last = "не удалось открыть пост"
        for candidate in _url_variants(url):
            last = _post_locked(candidate, body, settings)
            if not last:
                return ""
        return last


def _column(page):
    loc = page.locator('[aria-label="Column body"]')
    try:
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    return page


def _reply_dialog(page):
    loc = page.locator('[role="dialog"]')
    try:
        n = loc.count()
    except Exception:
        return None
    for i in range(n - 1, -1, -1):
        d = loc.nth(i)
        try:
            if d.is_visible() and d.locator('[contenteditable="true"]').count() > 0:
                return d
        except Exception:
            continue
    return None


def _box_hint(box) -> str:
    parts: list[str] = []
    for attr in ("aria-placeholder", "placeholder", "aria-label"):
        try:
            parts.append(box.get_attribute(attr, timeout=400) or "")
        except Exception:
            pass
    try:
        parts.append(box.inner_text(timeout=400) or "")
    except Exception:
        pass
    return " ".join(parts)


def _composer(page):
    """Поле ответа: сначала модалка, иначе inline на странице поста."""
    dlg = _reply_dialog(page)
    if dlg is not None:
        box = dlg.locator('[contenteditable="true"]')
        try:
            if box.count() > 0:
                return box.last
        except Exception:
            pass

    scope = _column(page)
    boxes = scope.locator('[contenteditable="true"]')
    try:
        n = boxes.count()
    except Exception:
        n = 0
    reply_box = None
    fallback = None
    for i in range(n - 1, -1, -1):
        el = boxes.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        hint = _box_hint(el)
        if HOME_COMPOSER_RE.search(hint):
            continue
        if REPLY_PLACEHOLDER_RE.search(hint):
            reply_box = el
            break
        if fallback is None:
            fallback = el
    return reply_box or fallback


def _dom_click(locator) -> bool:
    try:
        if locator.count() == 0:
            return False
        target = locator.first
        target.wait_for(state="visible", timeout=4000)
        handle = target.element_handle()
        if handle is None:
            return False
        handle.evaluate("el => el.closest('div[role=\"button\"], button, a')?.click() || el.click()")
        handle.dispose()
        return True
    except Exception:
        return False


def _dismiss_interstitial(page) -> None:
    try:
        btns = page.get_by_role("button", name=INTERSTITIAL_RE)
        n = btns.count()
    except Exception:
        n = 0
    for i in range(min(n, 4)):
        try:
            b = btns.nth(i)
            if b.is_visible():
                _dom_click(b)
                page.wait_for_timeout(400)
        except Exception:
            continue


def _open_reply(page) -> str:
    """Найти/открыть поле ответа. Пусто = ок."""
    if _composer(page) is not None:
        return ""
    scope = _column(page)
    clicks = [
        lambda: _dom_click(
            scope.locator(
                'svg[aria-label="Reply"], svg[aria-label="Ответ"], '
                'svg[aria-label="Comment"], svg[aria-label="Комментарий"]'
            )
        ),
        lambda: _dom_click(
            scope.locator(
                '[aria-label="Reply"], [aria-label="Ответ"], '
                '[aria-label="Comment"], [aria-label="Комментарий"]'
            )
        ),
        lambda: scope.get_by_role("button", name=REPLY_PLACEHOLDER_RE).first.click(
            timeout=4000
        ),
        lambda: page.get_by_role("button", name=REPLY_PLACEHOLDER_RE).first.click(
            timeout=4000
        ),
    ]
    for fn in clicks:
        if _composer(page) is not None:
            return ""
        try:
            fn()
            page.wait_for_timeout(800)
        except Exception:
            continue
        if _composer(page) is not None:
            return ""
    try:
        page.wait_for_selector("[contenteditable='true']", timeout=6000)
    except PlaywrightTimeout:
        pass
    if _composer(page) is not None:
        return ""
    return "не открылось поле ответа (Reply)"


def _read_composer(page) -> str:
    box = _composer(page)
    if box is None:
        return ""
    try:
        return (box.inner_text(timeout=2000) or "").strip()
    except Exception:
        return ""


def _insert_text(page, text: str) -> bool:
    box = _composer(page)
    if box is None:
        try:
            page.wait_for_selector("[contenteditable='true']", timeout=12000)
        except PlaywrightTimeout:
            return False
        box = _composer(page)
    if box is None:
        return False
    try:
        box.click(timeout=8000)
    except Exception:
        try:
            box.click(force=True, timeout=4000)
        except Exception:
            return False
    page.wait_for_timeout(250)
    try:
        page.keyboard.press("Control+A")
        page.wait_for_timeout(60)
        page.keyboard.press("Backspace")
    except Exception:
        pass
    # Lexical: именно type(), не insert_text/execCommand — иначе Post остаётся disabled
    try:
        page.keyboard.type(text, delay=16)
    except Exception:
        try:
            box.type(text, delay=16)
        except Exception:
            return False
    page.wait_for_timeout(500)
    got = _read_composer(page)
    if text[:10] not in got and (got == "" or len(got) < max(3, min(8, len(text)))):
        try:
            box.click(timeout=3000)
            page.keyboard.type(text, delay=20)
        except Exception:
            return False
        page.wait_for_timeout(400)
        got = _read_composer(page)
    return bool(got) and (text[:8] in got or len(got) >= min(8, len(text)))


def _click_dialog_post(page) -> bool:
    root = _reply_dialog(page)
    if root is None:
        return False
    btn = root.get_by_role("button", name=SEND_TEXT)
    try:
        n = btn.count()
    except Exception:
        n = 0
    for i in range(n - 1, -1, -1):
        b = btn.nth(i)
        try:
            if not b.is_visible():
                continue
            disabled = (b.get_attribute("aria-disabled") or "").lower()
            if disabled in {"true", "1"}:
                continue
            b.click(timeout=8000)
            return True
        except Exception:
            try:
                b.click(force=True, timeout=4000)
                return True
            except Exception:
                continue
    send = root.locator("div[role='button'], button").filter(has_text=SEND_TEXT)
    try:
        n = send.count()
    except Exception:
        n = 0
    for i in range(n - 1, -1, -1):
        b = send.nth(i)
        try:
            if b.is_visible():
                b.click(timeout=6000)
                return True
        except Exception:
            continue
    return False


def _click_send(page) -> None:
    # В модалке есть Post. В inline-композиторе кнопки нет — только Ctrl+Enter.
    if _click_dialog_post(page):
        return
    box = _composer(page)
    if box is not None:
        try:
            box.press("Control+Enter")
            return
        except Exception:
            pass
    page.keyboard.press("Control+Enter")


def _toast_error(page) -> bool:
    try:
        alert = page.locator('[role="alert"]')
        if alert.count() == 0:
            return False
        txt = (alert.last.inner_text(timeout=800) or "").lower()
    except Exception:
        return False
    needles = (
        "couldn't post",
        "couldn’t post",
        "something went wrong",
        "не удалось",
        "попробуй ещё",
        "try again later",
    )
    return any(n in txt for n in needles)


def _composer_cleared(page, posted: str) -> bool:
    """После успешного ответа inline-поле пустеет (остаётся placeholder)."""
    got = _read_composer(page)
    if not got:
        return True
    probe = posted[:12]
    if probe and probe in got:
        return False
    if HOME_COMPOSER_RE.search(got) or REPLY_PLACEHOLDER_RE.search(got):
        if posted[:8] not in got:
            return True
    return len(got) < 3


def _text_on_page(page, posted: str) -> bool:
    probe = posted[:24]
    if len(probe) < 8:
        return False
    try:
        body = page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
        return False
    return probe in body and _composer_cleared(page, posted)


def _verify_posted(page, graphql_ok: bool, posted: str) -> bool:
    if _toast_error(page):
        return False
    if graphql_ok:
        return True
    if _composer_cleared(page, posted):
        return True
    if _text_on_page(page, posted):
        return True
    return False


def _looks_like_reply_api(url: str, payload: str) -> bool:
    blob = f"{url}\n{payload}".lower()
    keys = (
        "createreply",
        "create_reply",
        "usebarcelonacreatereply",
        "barcelonacreatereply",
        "text_post_app_create",
        "textpostappcreate",
        "addreply",
        "add_reply",
        "post_reply",
        "postreply",
        "replyto",
        "reply_to",
    )
    return any(k in blob for k in keys)


def _launch_browser(pw):
    from playwright_env import CHROME_ARGS

    try:
        return pw.chromium.launch(headless=False, channel="chrome", args=list(CHROME_ARGS))
    except Exception:
        return pw.chromium.launch(headless=False, args=list(CHROME_ARGS))


def _goto(page, url: str) -> None:
    last: Exception | None = None
    for _ in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as exc:
            last = exc
            msg = str(exc)
            if any(
                t in msg
                for t in ("QUIC", "CONNECTION", "ERR_TUNNEL", "net::ERR", "Timeout")
            ):
                page.wait_for_timeout(900)
                continue
            raise
    assert last is not None
    raise last


def _post_locked(url: str, body: str, settings: Settings) -> str:
    fix_playwright_browsers_path()
    user_agent = getattr(settings, "user_agent", "") or None
    cookies = getattr(settings, "threads_cookies", None) or []
    pw = sync_playwright().start()
    browser = None
    graphql_ok = {"v": False}
    armed = {"v": False}
    try:
        browser = _launch_browser(pw)
        ctx_kw: dict = {
            "viewport": {"width": 1365, "height": 900},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "storage_state": str(SESSION_PATH),
        }
        if user_agent:
            ctx_kw["user_agent"] = user_agent
        context = browser.new_context(**ctx_kw)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception:
                pass
        page = context.new_page()

        def _maybe_mark(u: str, data: str, status: int | None = None) -> None:
            if not armed["v"]:
                return
            if status is not None and status not in (200, 201):
                return
            low_u = (u or "").lower()
            if "graphql" not in low_u and "/api/" not in low_u:
                return
            payload = data or ""
            if body[:32] and body[:32] in payload:
                graphql_ok["v"] = True
                return
            if _looks_like_reply_api(low_u, payload):
                graphql_ok["v"] = True

        def on_request(req) -> None:
            try:
                if req.method != "POST":
                    return
                _maybe_mark(req.url or "", req.post_data or "")
            except Exception:
                pass

        def on_response(resp) -> None:
            try:
                if resp.request.method != "POST":
                    return
                data = ""
                try:
                    data = (resp.request.post_data or "")[:8000]
                except Exception:
                    data = ""
                _maybe_mark(resp.url or "", data, resp.status)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        _goto(page, url)
        page.wait_for_timeout(1600)
        prepare_threads_page(page)
        _dismiss_interstitial(page)
        page.wait_for_timeout(700)
        if looks_like_login_wall(page):
            return "Threads просит вход — нажми «Войти в Threads» в программе"

        why = _open_reply(page)
        if why:
            return why
        if not _insert_text(page, body):
            return "не смог вставить текст в поле ответа"

        logger.info("Комментарий набран, отправляю Ctrl+Enter / Post")
        armed["v"] = True
        _click_send(page)
        deadline = time.time() + 18
        while time.time() < deadline:
            page.wait_for_timeout(400)
            if _verify_posted(page, graphql_ok["v"], body):
                break
        if not _verify_posted(page, graphql_ok["v"], body):
            _click_send(page)
            page.wait_for_timeout(2800)
        if _toast_error(page):
            return "Threads показал ошибку публикации — попробуй ещё раз"
        if not _verify_posted(page, graphql_ok["v"], body):
            return (
                "не подтвердилось, что комментарий ушёл "
                "(проверь пост вручную)"
            )

        try:
            context.storage_state(path=str(SESSION_PATH))
        except Exception:
            pass
        logger.info("Комментарий подтверждён в Threads: %s", url)
        page.wait_for_timeout(800)
        return ""
    except PlaywrightTimeout:
        return "таймаут Threads — попробуй ещё раз"
    except Exception as exc:
        logger.error("commenter: %s", exc)
        return str(exc)[:180]
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
