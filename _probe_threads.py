"""Разовая проверка: какие URL Threads отдаёт, а какие лимитирует (429)."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from config import load_settings
from playwright_env import fix_playwright_browsers_path
from scraper import SELECTORS
from session_store import SESSION_PATH

sys.stdout.reconfigure(encoding="utf-8")

URLS = [
    ("главная лента", "https://www.threads.com/"),
    ("поиск recent", "https://www.threads.com/search?q=%23SEO&filter=recent"),
    ("поиск tags", "https://www.threads.com/search?q=%23SEO&serp_type=tags"),
    ("поиск без фильтра", "https://www.threads.com/search?q=SEO"),
]
s = load_settings()
fix_playwright_browsers_path()

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=s.user_agent,
        viewport={"width": 1365, "height": 900},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        storage_state=str(SESSION_PATH),
    )
    page = ctx.new_page()
    for label, url in URLS:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = resp.status if resp else 0
        except Exception as exc:
            print(f"{label:20} ошибка: {str(exc)[:80]}")
            continue
        page.wait_for_timeout(4000)
        cards = len(page.query_selector_all(SELECTORS["post_container"]))
        print(f"{label:20} HTTP {status} | карточек {cards}")
        page.wait_for_timeout(2500)
    ctx.close()
    browser.close()
