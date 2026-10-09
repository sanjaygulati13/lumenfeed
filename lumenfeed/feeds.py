"""Feed fetching: RSS/Atom parsing, HTML scraping, date normalization, refresh.

This module knows nothing about the database schema beyond inserting article
rows, and nothing about HTTP routing. It produces plain article dicts.
"""

import re
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import FETCH_TIMEOUT, USER_AGENT, log
from .db import get_db
from .enrich import is_http_url
from .sources import SOURCES

# Base domain per source, used to resolve relative article links at enrichment
# time (some scraped sources historically stored relative paths).
SOURCE_BASE = {s["name"]: "/".join(s["url"].split("/")[:3]) for s in SOURCES}

# --- Text helpers --------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},\s+\d{4})"
)
_CATEGORY_PREFIX_RE = re.compile(
    r"^(Announcements?|Product|Research|Engineering|Policy|Safety|Economic)\s+"
)
# arXiv's RSS prefixes every abstract with "arXiv:2610.02391v1 Announce Type: new
# Abstract:" (type can also be cross / replace / replace-cross).
_ARXIV_PREFIX_RE = re.compile(r"^arXiv:\S+\s+Announce Type:\s*[\w-]+\s+Abstract:\s*")
_SKIP_PATHS = (
    "/category", "/tag", "/page", "/about", "/contact",
    "/products", "/research/", "/careers", "/solutions",
    "/case-studies", "/customer", "/blog/",
)


def clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    clean = soup.get_text(separator=" ", strip=True)
    clean = _WS_RE.sub(" ", clean).strip()
    clean = re.sub(r"\s*Read More\s*$", "", clean, flags=re.IGNORECASE).strip()
    clean = clean.rstrip(" \u2192")
    return clean


def truncate(text: str, max_len: int = 500) -> str:
    """Truncate at word boundary with ellipsis."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.8:
        truncated = truncated[:last_space]
    return truncated + "\u2026"


def parse_published(value) -> int | None:
    """Parse a date string (RSS/Atom/scrape) into unix epoch seconds (UTC).

    Handles the three formats we actually see:
      * ISO 8601             ``2026-09-19T17:10:08+00:00``  (feedparser datetime)
      * RFC 822 / struct     ``Sat, 19 Sep 2026 14:00:00 +0000`` (str(struct_time))
      * scraper short form   ``Jan 5, 2026``

    Returns ``None`` when the value is empty or unparseable.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # ISO 8601 (fromisoformat does not accept a trailing 'Z' before 3.11).
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass

    # RFC 822 ("Sat, 19 Sep 2026 14:00:00 +0000").
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        pass

    # Scraper short form ("Jan 5, 2026").
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


# --- RSS -----------------------------------------------------------------


def fetch_rss(source: dict) -> list[dict]:
    """Fetch and parse a single RSS/Atom feed."""
    articles: list[dict] = []
    try:
        # Download with requests so the fetch honours FETCH_TIMEOUT (feedparser's
        # own fetcher has no timeout, so one hung feed would stall the refresh).
        resp = requests.get(
            source["url"], headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT
        )
        resp.raise_for_status()
        parsed = feedparser.parse(
            resp.content,
            response_headers={
                "content-location": resp.url,
                "content-type": resp.headers.get("content-type", ""),
            },
        )
        if parsed.bozo and not parsed.entries:
            log.warning("Feed error for %s: %s", source["name"], parsed.bozo_exception)
            return []

        for entry in parsed.entries[:30]:
            title = clean_html(getattr(entry, "title", ""))
            if not title:
                continue

            desc = ""
            for field in ("summary", "description", "content"):
                val = getattr(entry, field, None)
                if val:
                    if isinstance(val, list):
                        val = val[0].get("value", "") if val else ""
                    desc = _ARXIV_PREFIX_RE.sub("", clean_html(val))
                    if desc:
                        break

            link = getattr(entry, "link", "")
            if not link:
                continue

            published = ""
            for date_field in ("published", "updated", "created"):
                date_val = getattr(entry, date_field, None)
                if date_val:
                    try:
                        dv = date_val
                        published = dv.isoformat() if hasattr(dv, "isoformat") else str(dv)
                    except Exception:
                        pass
                    break

            articles.append({
                "title": title,
                "summary": truncate(desc),
                "source": source["name"],
                "category": source["category"],
                "link": link,
                "published": published,
                "published_ts": parse_published(published),
            })
    except Exception as e:
        log.warning("Failed to fetch %s: %s", source["name"], e)

    return articles


# --- HTML scraping -------------------------------------------------------


def _skip_nav(a) -> bool:
    """True if the anchor sits inside site chrome (nav/header/footer/aside)."""
    for p in a.parents:
        if p.name in ("nav", "header", "footer", "aside"):
            return True
    return False


def _pick_date(a) -> str:
    """Publication date: prefer a <time> element, else a date pattern in the text."""
    t = a.find("time")
    if t:
        txt = clean_html(t.get_text())
        if txt:
            return txt
    m = _DATE_RE.search(clean_html(a.get_text()))
    return m.group(1) if m else ""


def _pick_title(a) -> str:
    """Clean title: prefer a heading or a *-title* element; else cleaned anchor
    text with the date removed. Avoids concatenating date+category+title blobs."""
    for tag in ("h1", "h2", "h3", "h4"):
        h = a.find(tag)
        if h:
            t = clean_html(h.get_text())
            if t:
                return t
    for el in a.find_all(True):
        if el is a:
            continue
        if "title" in " ".join(el.get("class", [])).lower():
            t = clean_html(el.get_text())
            if t and len(t) >= 5:
                return t
    text = clean_html(a.get_text())
    time_el = a.find("time")
    if time_el:
        text = text.replace(clean_html(time_el.get_text()), " ", 1)
    text = re.sub(r"\s+", " ", text).strip()
    text = _CATEGORY_PREFIX_RE.sub("", text).strip()
    return text


def _pick_summary(a) -> str:
    """Optional lead text: the first substantial <p> inside the card, if any."""
    for p in a.find_all("p"):
        text = clean_html(p.get_text())
        if len(text) >= 40:
            return truncate(text, 300)
    return ""


def fetch_scrape(source: dict) -> list[dict]:
    """Scrape an HTML page for article links (for sites without RSS).

    Structure-aware: skips navigation chrome, and pulls the title from a
    heading/title element, the date from <time>, and an optional lead from a
    <p> — instead of concatenating the whole anchor's text.
    """
    articles: list[dict] = []
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        seen_links: set[str] = set()
        base_domain = "/".join(source["url"].split("/")[:3])
        domain = source["url"].split("/")[2]

        for a in soup.find_all("a", href=True):
            if _skip_nav(a):
                continue
            href = a["href"]
            if href.startswith("/"):
                href = base_domain + href
            elif not href.startswith(domain):
                continue
            if any(skip in href for skip in _SKIP_PATHS):
                continue

            title = _pick_title(a)
            if len(title) < 10:
                continue
            if href in seen_links:
                continue
            seen_links.add(href)

            published = _pick_date(a)
            articles.append({
                "title": title,
                "summary": _pick_summary(a),
                "source": source["name"],
                "category": source["category"],
                "link": href,
                "published": published,
                "published_ts": parse_published(published),
            })
            if len(articles) >= 20:
                break

    except Exception as e:
        log.warning("Scrape failed for %s: %s", source["name"], e)

    return articles


def fetch_rsc_news(source: dict) -> list[dict]:
    """Parse Next.js RSC payload for article data.

    Anthropic's /news page embeds its full post list (268+ entries) as JSON
    inside a ``self.__next_f.push`` script tag. The visible HTML only renders
    ~5 featured cards, so the generic scraper misses most posts. This parser
    extracts title, date, slug, and summary directly from the RSC payload.
    """
    articles: list[dict] = []
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.content.decode("utf-8", "replace")  # Next.js pages are UTF-8

        # Extract the RSC script content
        m = re.search(r"<script[^>]*>(self\.__next_f\.push.*?)</script>", html, re.DOTALL)
        if not m:
            log.warning("RSC: no script payload found for %s", source["name"])
            return []
        script = m.group(1)

        # Split on post markers: \"_type\":\"post\"
        marker = r'\"_type\":\"post\"'
        segments = script.split(marker)
        if len(segments) <= 1:
            log.warning("RSC: no post markers found for %s", source["name"])
            return []

        base_domain = "/".join(source["url"].split("/")[:3])
        seen: set[str] = set()

        for seg in segments[1:]:
            tm = re.search(r'\\\"title\\\":\\\"(.*?)\\\"', seg)
            dm = re.search(r'\\\"publishedOn\\\":\\\"(.*?)\\\"', seg)
            sm = re.search(r'\\\"current\\\":\\\"(.*?)\\\"', seg)
            summary_m = re.search(r'\\\"summary\\\":\\\"(.*?)\\\"', seg)

            if not (tm and dm and sm):
                continue

            slug = sm.group(1)
            link = f"{base_domain}/{slug}"
            if link in seen:
                continue
            seen.add(link)

            title = tm.group(1).strip()
            if len(title) < 5:
                continue

            # Convert ISO 8601 (e.g. 2026-10-01T15:19:00.000Z) to "Oct 1, 2026" style
            raw_date = dm.group(1)
            published_ts = parse_published(raw_date)
            published = raw_date[:10]  # keep ISO date for display

            summary = ""
            if summary_m:
                raw_summary = summary_m.group(1)
                if raw_summary and raw_summary != "null":
                    summary = truncate(raw_summary.replace("\\\"", '"'), 300)

            articles.append({
                "title": title,
                "summary": summary,
                "source": source["name"],
                "category": source["category"],
                "link": link,
                "published": published,
                "published_ts": published_ts,
            })
            if len(articles) >= 50:
                break

    except Exception as e:
        log.warning("RSC fetch failed for %s: %s", source["name"], e)

    return articles


def fetch_feed(source: dict) -> list[dict]:
    """Fetch a source (RSS, scrape, or RSC) and return list of article dicts."""
    stype = source.get("type")
    if stype == "scrape":
        return fetch_scrape(source)
    if stype == "rsc":
        return fetch_rsc_news(source)
    return fetch_rss(source)


def normalize_dates() -> int:
    """Backfill ``published_ts`` for existing rows (one-time migration helper).

    For each article whose ``published_ts`` is NULL, parse the stored
    ``published`` string; if that fails, fall back to the fetch time. Returns
    the number of rows updated. Idempotent — only touches NULL rows.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT id, published, fetched_at FROM articles WHERE published_ts IS NULL"
        ).fetchall()
    updated = 0
    for r in rows:
        ts = parse_published(r["published"])
        if ts is None:
            ts = parse_published(r["fetched_at"])
        if ts is None:
            continue
        with get_db() as db:
            db.execute("UPDATE articles SET published_ts = ? WHERE id = ?", (ts, r["id"]))
            db.commit()
        updated += 1
    if updated:
        log.info("Normalized published_ts for %d articles", updated)
    return updated


# The background loop, the empty-DB first fetch and POST /api/refresh can all
# trigger a refresh; only one may run at a time.
_refresh_lock = threading.Lock()


def strip_arxiv_boilerplate() -> int:
    """Remove the arXiv announce prefix from summaries stored before ingest
    started stripping it. Idempotent; returns the number of rows fixed."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, summary FROM articles WHERE summary LIKE 'arXiv:%Announce Type:%'"
        ).fetchall()
        fixed = 0
        for r in rows:
            new = _ARXIV_PREFIX_RE.sub("", r["summary"])
            if new != r["summary"]:
                db.execute("UPDATE articles SET summary = ? WHERE id = ?", (new, r["id"]))
                fixed += 1
        db.commit()
    if fixed:
        log.info("Stripped arXiv boilerplate from %d summaries", fixed)
    return fixed


def refresh_all_feeds() -> int | None:
    """Fetch all feeds and insert new articles.

    Returns the count of new articles, or ``None`` if another refresh was
    already running (in which case this call does nothing).
    """
    if not _refresh_lock.acquire(blocking=False):
        log.info("Refresh already in progress; skipping")
        return None
    try:
        return _refresh_all_feeds()
    finally:
        _refresh_lock.release()


def _refresh_all_feeds() -> int:
    new_count = 0
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    now_ts = int(now_dt.timestamp())

    for source in SOURCES:
        articles = fetch_feed(source)
        for art in articles:
            if not is_http_url(art["link"]):
                log.warning("Skipping non-http link from %s: %r", source["name"], art["link"])
                continue
            # Fall back to the fetch time when a feed does not expose a date,
            # so every row has a sortable recency key.
            pub_ts = art.get("published_ts") or now_ts
            try:
                with get_db() as db:
                    cur = db.execute(
                        """INSERT OR IGNORE INTO articles
                           (title, summary, source, category, link, published,
                            published_ts, fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (art["title"], art["summary"], art["source"],
                         art["category"], art["link"], art["published"], pub_ts, now),
                    )
                    new_count += cur.rowcount
                    db.commit()
            except Exception as e:
                log.warning("DB insert error: %s", e)

    log.info("Refresh complete: %d new articles", new_count)
    return new_count
