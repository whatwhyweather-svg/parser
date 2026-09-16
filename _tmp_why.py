import sqlite3
from config import load_settings
from filters import post_passes_filters, has_intent, looks_like_seo_contractor_request

s = load_settings()
c = sqlite3.connect(s.database_path)
c.row_factory = sqlite3.Row

print("=== last hour drops ===")
for r in c.execute(
    """
    SELECT detail, SUM(n) AS n
    FROM pipeline_events
    WHERE stage='drop' AND ts >= datetime('now','-2 hours')
    GROUP BY detail ORDER BY n DESC LIMIT 30
    """
):
    print(r["n"], (r["detail"] or "")[:120])

print("\n=== fetched/passed/sent 2h ===")
for r in c.execute(
    """
    SELECT stage, SUM(n) n FROM pipeline_events
    WHERE ts >= datetime('now','-2 hours')
    GROUP BY stage ORDER BY n DESC
    """
):
    print(dict(r))

# simulate typical posts
samples = [
    "Ищу сео специалиста на сайт интернет-магазина, бюджет обсуждаем",
    "ищу сео специалиста",
    "Нужен SEO специалист с кейсами",
    "Ищу хорошего seo-шника, передавать заказы",
]
print("\n=== filter on SEO/GEO ===")
for t in samples:
    for tag in ("SEO", "GEO"):
        ok, why = post_passes_filters(t, tag)
        print(repr(t[:50]), tag, ok, why, "intent", has_intent(t), "seo_req", looks_like_seo_contractor_request(t, tag))
