"""Аудит отказов: что думают текущие правила и что думает DeepSeek.

Запуск: .venv\\Scripts\\python.exe -X utf8 _audit_rejects.py [с_времени] [лимит]
"""

from __future__ import annotations

import io
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from config import load_settings
from deepseek_filter import DeepSeekFilter
from filters import post_passes_filters

sys.stdout.reconfigure(encoding="utf-8")

SINCE = sys.argv[1] if len(sys.argv) > 1 else "00:00"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 500
WORKERS = 8
LINE_RE = re.compile(
    r"^(?P<ts>\d\d:\d\d:\d\d) (?P<tag>#\S+) @(?P<user>\S+) · (?P<verdict>.+?) · «(?P<text>.*)»"
    r"(?: · (?P<url>\S*))?$"
)
# Ночное окно идёт через полночь: берём всё, что позже SINCE, либо до 12:00
NIGHT = SINCE >= "12:00"


def in_window(ts: str) -> bool:
    hhmm = ts[:5]
    return hhmm >= SINCE if not NIGHT else (hhmm >= SINCE or hhmm < "12:00")


seen: set[str] = set()
rows: list[dict[str, str]] = []
for line in io.open("scan_log.txt", encoding="utf-8"):
    m = LINE_RE.match(line.strip())
    if not m or not in_window(m["ts"]) or "✓" in m["verdict"]:
        continue
    if "не русский" in m["verdict"] or "уже в базе" in m["verdict"]:
        continue
    key = f"{m['user']}|{m['text'][:60]}"
    if key in seen:
        continue
    seen.add(key)
    rows.append(m.groupdict())

print(f"уникальных русских отказов: {len(rows)} (проверю {min(LIMIT, len(rows))})")
rows = rows[:LIMIT]

ai = DeepSeekFilter(load_settings().deepseek_api_key)


def judge(row: dict[str, str]) -> tuple[dict[str, str], bool, bool, str]:
    tag = row["tag"].lstrip("#")
    rules_ok, _r = post_passes_filters(row["text"], tag, require_russian=True)
    ai_ok, why = ai.is_good_lead(row["text"], tag)
    return row, rules_ok, ai_ok, why


recovered: list[tuple[dict[str, str], str]] = []
missed: list[tuple[dict[str, str], str]] = []
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    for row, rules_ok, ai_ok, why in pool.map(judge, rows):
        if not ai_ok:
            continue
        (recovered if rules_ok else missed).append((row, why))


def show(title: str, items: list[tuple[dict[str, str], str]]) -> None:
    print(f"\n=== {title}: {len(items)} ===")
    for row, why in items:
        print(
            f"\n{row['ts']} @{row['user']} · было: {row['verdict']}\n"
            f"  DeepSeek: {why}\n  текст: {row['text'][:220]}\n"
            f"  {row.get('url') or ''}"
        )


show("ЛИДЫ, которые новые правила уже пропускают", recovered)
show("ЛИДЫ, которые правила всё ещё режут — надо править", missed)
