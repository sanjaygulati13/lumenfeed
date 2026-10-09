"""Time-decayed preference engine and feed scoring.

The engine watches what you actually read. Every read logs
``(source, category, timestamp)``; recent reads weigh more via an exponential
30-day half-life. Articles are then ranked by a blend of your taste, how fresh
they are, and a bit of exploration so one source can't take over.
"""

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from .config import PREFERENCE_GAMMA
from .db import get_db

HALF_LIFE_HOURS = 30 * 24  # 30-day half-life


def get_preference_profile() -> dict:
    """
    Compute time-decayed preference scores per source and category.
    Returns: {"sources": {name: score}, "categories": {name: score}, "total_reads": int}
    """
    with get_db() as db:
        rows = db.execute("SELECT source, category, read_at FROM reads").fetchall()

    if not rows:
        return {"sources": {}, "categories": {}, "total_reads": 0}

    now = datetime.now(timezone.utc)
    decay_lambda = math.log(2) / HALF_LIFE_HOURS

    source_scores: dict[str, float] = defaultdict(float)
    category_scores: dict[str, float] = defaultdict(float)

    for row in rows:
        try:
            read_time = datetime.fromisoformat(row["read_at"])
            if read_time.tzinfo is None:
                read_time = read_time.replace(tzinfo=timezone.utc)
            age_hours = (now - read_time).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = HALF_LIFE_HOURS

        weight = math.exp(-decay_lambda * max(0, age_hours))
        source_scores[row["source"]] += weight
        category_scores[row["category"]] += weight

    return {
        "sources": _compress(source_scores),
        "categories": _compress(category_scores),
        "total_reads": len(rows),
    }


def _compress(scores: dict[str, float]) -> dict[str, float]:
    """Normalize scores to [0, 1] (top = 1.0) and compress the spread.

    Plain max-normalization lets the single most-read source pin to 1.0 while
    every other source collapses toward 0, which feeds the read-more ->
    rank-higher feedback loop and lets one source (often Hacker News) dominate
    the personalized feed. Raising the normalized value to ``PREFERENCE_GAMMA``
    (0 < gamma < 1) keeps the ordering intact but shrinks the gap, so a
    favourite is still preferred without steamrolling the rest.
    """
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {k: 0.0 for k in scores}
    return {k: (v / top) ** PREFERENCE_GAMMA for k, v in scores.items()}


def score_articles(
    articles: list[dict], profile: dict, dismissed_sources: dict | None = None
) -> list[dict]:
    """
    Score articles: 70% preference + 20% recency + 10% exploration.
    Returns articles sorted by score descending.
    """
    if not articles:
        return []

    dismissed_sources = dismissed_sources or {}
    # Recency from the normalized publish time (epoch seconds). Rows without a
    # date fall back to 0 so the term degrades gracefully to 0.
    ts_vals = [a.get("published_ts") or 0 for a in articles]
    max_ts, min_ts = max(ts_vals), min(ts_vals)
    ts_range = max(max_ts - min_ts, 1)
    total_reads = profile["total_reads"]

    scored = []
    for art in articles:
        src_pref = profile["sources"].get(art["source"], 0.3)
        cat_pref = profile["categories"].get(art["category"], 0.3)
        preference = 0.6 * src_pref + 0.4 * cat_pref

        recency = ((art.get("published_ts") or 0) - min_ts) / ts_range

        if total_reads < 5:
            exploration = 0.5
        else:
            src_score = profile["sources"].get(art["source"], 0)
            exploration = max(0.2, min(1.0, 1.0 - src_score * 0.5))

        score = 0.7 * preference + 0.2 * recency + 0.1 * exploration
        score += random.uniform(0, 0.05)  # Serendipity jitter

        # "Recommend less": each swipe-dismissal of this source cuts its ranking.
        pen = dismissed_sources.get(art["source"], 0)
        if pen:
            score *= max(0.05, 1.0 - 0.5 * pen)

        scored.append((score, art))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [art for _, art in scored]


def diversify(
    scored: list[dict], limit: int, per_source_cap: int | None = None
) -> list[dict]:
    """Pick up to ``limit`` articles from an already best-first sorted list
    while keeping the source mix balanced.

    A plain top-N selection lets a chatty source (Hacker News publishes far
    more — and newer — stories than a quiet blog) monopolize the page. Instead
    we deal sources out round-robin, in order of each source's best remaining
    story, subject to a per-source cap. If there are not enough distinct
    sources to fill the page, the cap is relaxed *equally* across all sources
    (still round-robin) rather than piling the extra slots on one source.

    ``per_source_cap`` of ``None``/<=0 means auto: ``max(1, ceil(limit / 5))``.
    """
    if not scored:
        return []
    if per_source_cap is None or per_source_cap <= 0:
        per_source_cap = max(1, math.ceil(limit / 5))

    # Group article indices by source; each group stays in score order so
    # index 0 is always that source's best remaining story. The global index
    # doubles as a score rank (lower == better) for ordering sources.
    by_source: dict[str, list[int]] = defaultdict(list)
    for i, art in enumerate(scored):
        by_source[art.get("source", "?")].append(i)

    counts: dict[str, int] = defaultdict(int)
    picked: list[dict] = []
    cap = per_source_cap

    while len(picked) < limit:
        # One interleaved round: visit sources by their current best story,
        # taking one from each that still has stories and is under the cap.
        eligible = [s for s in by_source if by_source[s] and counts[s] < cap]
        if not eligible:
            if not any(by_source.values()):
                break  # out of articles entirely
            cap *= 2  # not enough sources to fill the page; relax fairly
            continue

        for src in sorted(eligible, key=lambda s: by_source[s][0]):
            if len(picked) >= limit:
                break
            if counts[src] < cap and by_source[src]:
                picked.append(scored[by_source[src].pop(0)])
                counts[src] += 1

    return picked


def get_read_article_ids() -> set[int]:
    """Get set of article IDs the user has read."""
    with get_db() as db:
        rows = db.execute("SELECT article_id FROM reads").fetchall()
    return {row["article_id"] for row in rows}


def get_dismissed_source_counts() -> dict[str, int]:
    """How many times the user asked to 'recommend less' from each source."""
    with get_db() as db:
        rows = db.execute(
            "SELECT source, COUNT(*) c FROM dismissals "
            "WHERE kind='source' AND source IS NOT NULL GROUP BY source"
        ).fetchall()
    return {row["source"]: row["c"] for row in rows}
