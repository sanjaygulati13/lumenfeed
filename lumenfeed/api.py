"""FastAPI application: HTTP routes, app lifecycle, and the CLI entry point."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import __version__
from .config import (
    ENRICH_ENABLED,
    FAVICON,
    FEED_PER_SOURCE_CAP,
    HOST,
    INDEX_HTML,
    MANIFEST,
    PORT,
    log,
)
from .db import get_db, init_db
from .enrich import enrich_article, extract_content, needs_enrichment
from .feeds import SOURCE_BASE, normalize_dates, refresh_all_feeds, strip_arxiv_boilerplate
from .jobs import _backfill_on_startup, auto_refresh_loop
from .personalization import (
    diversify,
    get_dismissed_source_counts,
    get_preference_profile,
    get_read_article_ids,
    score_articles,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    init_db()
    normalize_dates()  # backfill published_ts for pre-existing rows (idempotent)
    strip_arxiv_boilerplate()  # clean summaries stored by older versions (idempotent)
    with get_db() as db:
        row = db.execute("SELECT COUNT(*) as c FROM articles").fetchone()
        if row["c"] == 0:
            log.info("Empty database, performing initial fetch...")
            asyncio.create_task(asyncio.to_thread(refresh_all_feeds))
    task = asyncio.create_task(auto_refresh_loop())
    backfill_task = None
    if ENRICH_ENABLED:
        backfill_task = asyncio.create_task(_backfill_on_startup())
    yield
    task.cancel()
    if backfill_task is not None:
        backfill_task.cancel()

DIGEST_WINDOW_SECONDS = 7 * 24 * 3600


app = FastAPI(
    title="Lumenfeed",
    description="Infinite scroll feed of curated tech/AI news with personalization.",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/")
async def index() -> FileResponse:
    """Serve the main SPA."""
    return FileResponse(str(INDEX_HTML))


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    """Serve the favicon."""
    return FileResponse(str(FAVICON), media_type="image/svg+xml")


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    """Serve PWA manifest."""
    return FileResponse(str(MANIFEST), media_type="application/json")


# --- API Endpoints -------------------------------------------------------


@app.get("/api/articles")
async def get_articles(
    after_ts: Optional[int] = Query(None, description="Cursor: published_ts < this value"),
    after_id: Optional[int] = Query(None, description="Cursor id for equal published_ts"),
    limit: int = Query(15, ge=1, le=50),
    mode: str = Query("all", description="'all' or 'personalized'"),
    category: Optional[str] = Query(None, description="Filter by category"),
    exclude: Optional[str] = Query(
        None, description="Comma-separated ids to hide (personalized pagination)"
    ),
) -> list[dict]:
    """Get articles, newest first across all sources.

    Ordered by publication time (``published_ts``) with ``id`` as a tie-breaker,
    so the feed is chronological and naturally interleaves sources instead of
    clustering by fetch batch. Use ``mode=personalized`` for the preference-
    scored "For You" feed.

    Consumed content never resurfaces: articles the user has read or swiped
    away are filtered out at the DB level for BOTH modes, so a read story stays
    gone after reload and a swiped story stays gone across tab switches. The
    "All" mode paginates with a composite ``(published_ts, id)`` cursor; the
    personalized mode is re-ranked on every request, so it ignores that cursor
    and instead honours ``exclude`` (the ids already shown) to yield the next
    best unseen set with no duplicates and no gaps.
    """
    if mode not in ("all", "personalized"):
        raise HTTPException(400, "mode must be 'all' or 'personalized'")

    is_pers = mode == "personalized"
    fetch_limit = limit * 6 if is_pers else limit

    where_clauses: list[str] = []
    params: list = []
    # Composite cursor: everything strictly "older" than (after_ts, after_id).
    # Only meaningful for the chronological 'all' mode; the personalized mode is
    # re-ranked every request, so a cursor there would silently drop rows.
    if after_ts is not None and after_id is not None and not is_pers:
        where_clauses.append("(published_ts < ? OR (published_ts = ? AND id < ?))")
        params.extend([after_ts, after_ts, after_id])
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    # Consumed content (read or swiped away) never resurfaces in EITHER feed.
    where_clauses.append(
        "id NOT IN (SELECT article_id FROM reads) "
        "AND id NOT IN (SELECT article_id FROM dismissals)"
    )
    # Personalized infinite scroll: hide what the client has already shown so
    # each page is the next best unseen set (no duplicates, no gaps).
    if is_pers and exclude:
        ids = [int(x) for x in exclude.split(",") if x.strip().lstrip("-").isdigit()]
        if ids:
            where_clauses.append("id NOT IN (" + ",".join("?" * len(ids)) + ")")
            params.extend(ids)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    params.append(fetch_limit)

    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, summary, source, category, link, published, published_ts "
            f"FROM articles {where_sql} ORDER BY published_ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        saved_ids = {r["article_id"] for r in db.execute("SELECT article_id FROM saves")}

    articles = [dict(r) for r in rows]

    if is_pers and len(articles) > 1:
        profile = get_preference_profile()
        scored = score_articles(articles, profile, get_dismissed_source_counts())
        # Quota-constrained selection so one chatty source (e.g. Hacker News)
        # can't dominate the page — see personalization.diversify.
        articles = diversify(scored, limit, FEED_PER_SOURCE_CAP or None)

    for art in articles:
        art["is_read"] = False  # read articles are filtered out above
        art["is_saved"] = art["id"] in saved_ids

    return articles


@app.get("/api/articles/{article_id}/summary")
async def get_article_summary(article_id: int) -> dict:
    """Return real context for an article, enriching on demand if needed.

    Cache-first: if a good summary already exists it is returned immediately.
    Otherwise the article page is fetched once (no LLM) and the result is
    persisted so the page is never fetched twice.
    """
    if not ENRICH_ENABLED:
        raise HTTPException(404, "Enrichment disabled")

    with get_db() as db:
        row = db.execute(
            "SELECT id, title, summary, source, link, summary_source "
            "FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Article not found")

    if not needs_enrichment(row["summary"]) or row["summary_source"] == "failed":
        return {
            "id": row["id"],
            "summary": row["summary"] or "",
            "summary_source": row["summary_source"] or "rss",
            "enriched": False,
        }

    base = SOURCE_BASE.get(row["source"])
    summary, src = await asyncio.to_thread(enrich_article, dict(row), base)
    if not summary:
        # Mark as failed so we don't re-fetch paywalled/blocked pages on every view.
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            db.execute(
                "UPDATE articles SET summary_source = 'failed', enriched_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            db.commit()
        return {
            "id": row["id"],
            "summary": row["summary"] or "",
            "summary_source": "failed",
            "enriched": False,
        }

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            "UPDATE articles SET summary = ?, summary_source = ?, enriched_at = ? WHERE id = ?",
            (summary, src, now, row["id"]),
        )
        db.commit()

    return {"id": row["id"], "summary": summary, "summary_source": src, "enriched": True}


@app.post("/api/articles/{article_id}/read")
async def mark_read(article_id: int) -> dict:
    """Mark an article as read. Logs source/category for preference learning."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        row = db.execute(
            "SELECT source, category FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")

        db.execute(
            "INSERT OR IGNORE INTO reads "
            "(article_id, source, category, read_at) VALUES (?, ?, ?, ?)",
            (article_id, row["source"], row["category"], now),
        )
        db.commit()

    return {"ok": True, "article_id": article_id}


@app.post("/api/articles/{article_id}/save")
async def save_article(article_id: int) -> dict:
    """Save/bookmark an article (for the Saved view)."""
    with get_db() as db:
        if not db.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone():
            raise HTTPException(404, "Article not found")
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO saves (article_id, saved_at) VALUES (?, ?)",
            (article_id, now),
        )
        db.commit()
    return {"ok": True, "article_id": article_id, "saved": True}


@app.delete("/api/articles/{article_id}/save")
async def unsave_article(article_id: int) -> dict:
    """Remove an article from Saved."""
    with get_db() as db:
        db.execute("DELETE FROM saves WHERE article_id = ?", (article_id,))
        db.commit()
    return {"ok": True, "article_id": article_id, "saved": False}


@app.post("/api/articles/{article_id}/dismiss")
async def dismiss_article(article_id: int, kind: str = Query("source")) -> dict:
    """
    Dismiss an article. kind='article' hides just this one; kind='source'
    hides it AND tells the personalized feed to recommend less from its source.
    """
    if kind not in ("article", "source"):
        raise HTTPException(400, "kind must be 'article' or 'source'")
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        row = db.execute(
            "SELECT source, category FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        db.execute(
            "INSERT OR REPLACE INTO dismissals "
            "(article_id, source, category, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            (article_id, row["source"], row["category"], kind, now),
        )
        db.commit()
    return {"ok": True, "article_id": article_id, "kind": kind, "source": row["source"]}


@app.get("/api/saved")
async def get_saved(
    after: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=50),
) -> list[dict]:
    """Saved articles, newest first (for the Saved tab)."""
    with get_db() as db:
        q = ("SELECT a.id, a.title, a.summary, a.source, a.category, a.link, "
             "a.published, a.published_ts "
             "FROM articles a JOIN saves s ON s.article_id = a.id")
        params: list = []
        if after is not None:
            q += " WHERE a.id < ?"
            params.append(after)
        q += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(q, params).fetchall()
    read_ids = get_read_article_ids()
    out = []
    for r in rows:
        d = dict(r)
        d["is_read"] = d["id"] in read_ids
        d["is_saved"] = True
        out.append(d)
    return out


@app.get("/api/saved/count")
async def get_saved_count() -> dict:
    """Number of saved articles (drives the Saved tab badge, accurate on load)."""
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) c FROM saves").fetchone()["c"]
    return {"count": n}


@app.get("/api/articles/{article_id}/content")
async def get_article_content(article_id: int) -> dict:
    """Extracted article body (paragraphs) for the in-app reader. No LLM."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, title, source, link FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Article not found")
    base = SOURCE_BASE.get(row["source"])
    paragraphs = await asyncio.to_thread(extract_content, row["link"], base)
    return {"id": row["id"], "title": row["title"], "source": row["source"],
            "link": row["link"], "paragraphs": paragraphs}


@app.get("/api/digest")
async def get_digest() -> dict:
    """A lightweight 'this week' digest: category mix + highlights from the last 7 days."""
    since = int(datetime.now(timezone.utc).timestamp()) - DIGEST_WINDOW_SECONDS
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) c FROM articles WHERE published_ts >= ?", (since,)
        ).fetchone()["c"]
        cats = db.execute(
            "SELECT category, COUNT(*) c FROM articles WHERE published_ts >= ? "
            "GROUP BY category ORDER BY c DESC",
            (since,),
        ).fetchall()
        top = db.execute("SELECT id, title, source, category, link, published, published_ts "
                         "FROM articles WHERE published_ts >= ? "
                         "ORDER BY published_ts DESC, id DESC LIMIT 5", (since,)).fetchall()
    return {
        "total": total,
        "categories": [{"name": c["category"], "count": c["c"]} for c in cats],
        "highlights": [dict(r) for r in top],
    }


@app.get("/api/preferences")
async def get_preferences() -> dict:
    """Get current preference profile."""
    profile = get_preference_profile()
    profile["sources"] = dict(
        sorted(profile["sources"].items(), key=lambda x: x[1], reverse=True)
    )
    profile["categories"] = dict(
        sorted(profile["categories"].items(), key=lambda x: x[1], reverse=True)
    )
    return profile


@app.post("/api/refresh")
async def refresh() -> dict:
    """Manually trigger a feed refresh (a no-op if one is already running)."""
    count = await asyncio.to_thread(refresh_all_feeds)
    return {"new_articles": count or 0, "already_running": count is None}


def main() -> None:
    """Entry point for the ``lumenfeed`` CLI command."""
    uvicorn.run("lumenfeed.api:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
