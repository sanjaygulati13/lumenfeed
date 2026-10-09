"""Background jobs: periodic feed refresh and summary backfill.

These are the long-running tasks started in the FastAPI lifespan. They are kept
out of the API module so the request handlers stay focused on serving HTTP.
"""

import asyncio
import time
from datetime import datetime, timezone

from .config import (
    ENRICH_BACKFILL_LIMIT,
    ENRICH_DELAY,
    ENRICH_ENABLED,
    REFRESH_INTERVAL_SECONDS,
    log,
)
from .db import get_db
from .enrich import MIN_SUMMARY_LEN, enrich_article
from .feeds import SOURCE_BASE, refresh_all_feeds


def backfill_summaries(limit: int = ENRICH_BACKFILL_LIMIT) -> int:
    """Enrich up to ``limit`` articles whose summary is missing/short/boilerplate.

    Idempotent: only touches rows with ``enriched_at IS NULL`` and a weak
    summary. Rate-limited to be polite to upstream sites. Returns count enriched.
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT id, title, summary, source, link FROM articles
               WHERE enriched_at IS NULL
                 AND (summary IS NULL OR summary = '' OR LENGTH(summary) < ?
                      OR summary LIKE '%Comments URL%' OR summary LIKE 'Article URL:%')
               ORDER BY id DESC LIMIT ?""",
            (MIN_SUMMARY_LEN, limit),
        ).fetchall()

    enriched = 0
    for r in rows:
        base = SOURCE_BASE.get(r["source"])
        summary, src = enrich_article(dict(r), base)
        now = datetime.now(timezone.utc).isoformat()
        # Mark attempted so paywalled/blocked pages aren't retried on every cycle.
        src_final = src if summary else "failed"
        with get_db() as db:
            db.execute(
                "UPDATE articles SET summary = COALESCE(?, summary), "
                "summary_source = ?, enriched_at = ? WHERE id = ?",
                (summary or None, src_final, now, r["id"]),
            )
            db.commit()
        if summary:
            enriched += 1
        time.sleep(ENRICH_DELAY)

    if enriched:
        log.info("Backfill enriched %d summaries", enriched)
    return enriched


async def _backfill_on_startup() -> None:
    """Warm up summaries for the existing pool shortly after startup."""
    await asyncio.sleep(3)
    try:
        await asyncio.to_thread(backfill_summaries)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("Startup backfill failed: %s", e)


async def auto_refresh_loop() -> None:
    """Background task: refresh feeds periodically (and backfill summaries)."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(refresh_all_feeds)
            if ENRICH_ENABLED:
                await asyncio.to_thread(backfill_summaries)
        except Exception as e:
            log.error("Auto-refresh failed: %s", e)
