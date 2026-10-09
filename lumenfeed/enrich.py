"""Summary enrichment for Lumenfeed.

Fills in *real* article context without any LLM call. When an article's
RSS/scraped summary is empty, too short, or boilerplate (e.g. hnrss
"Article URL: ... Comments URL: ..."), we fetch the article's own page and
extract, in order:

  1. <meta name="description"> / <meta property="og:description">
  2. lead text — the first 1-3 meaningful <p> paragraphs of the article body

This is free (no tokens/API cost), bounded (one bounded HTTP GET per article),
idempotent, and cached in the DB via the `summary_source` / `enriched_at`
columns so a page is never fetched twice.
"""

import logging
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .config import FETCH_TIMEOUT, USER_AGENT

log = logging.getLogger("lumenfeed.enrich")

MIN_SUMMARY_LEN = 40    # stored summaries shorter than this are treated as missing
MAX_SUMMARY_LEN = 300   # cap the context we store for cards
LEAD_PARAGRAPHS = 3
LEAD_PARA_MIN = 40      # ignore short <p> (captions, labels, bylines)

_WS_RE = re.compile(r"\s+")
# hnrss boilerplate, e.g. "Article URL: ... Comments URL: ... item?id=..."
_BOILERPLATE_RE = re.compile(r"Article URL:|Comments URL:|news\.ycombinator\.com/item", re.I)
# Template/boilerplate meta descriptions that carry no real context (e.g. HF's
# "A Blog post by X on Hugging Face") — skip these and fall back to lead text.
_GENERIC_META_RE = re.compile(
    r"^(a|an|the)\s+(blog post|post|blog|article|podcast|page|announcement)\b.*\b(on|at|from)\b"
    r"|^(read|learn|watch|see)\s+more\b"
    r"|^published\s+(on|at)\b",
    re.I,
)


def is_http_url(url: str) -> bool:
    """True for absolute http(s) URLs. Feed links are untrusted input, so
    anything else (javascript:, file:, data:, ...) is never stored or fetched."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def clean(text: str) -> str:
    """Strip tags and normalize whitespace."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    t = soup.get_text(separator=" ", strip=True)
    return _WS_RE.sub(" ", t).strip()


def needs_enrichment(summary) -> bool:
    """True when the stored summary is empty, too short, or known boilerplate."""
    if not summary:
        return True
    s = summary.strip()
    if len(s) < MIN_SUMMARY_LEN:
        return True
    if _BOILERPLATE_RE.search(s):
        return True
    return False


def _truncate(text: str, max_len: int = MAX_SUMMARY_LEN) -> str:
    """Truncate at a word boundary with an ellipsis."""
    text = clean(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    sp = cut.rfind(" ")
    if sp > max_len * 0.7:
        cut = cut[:sp]
    return cut.rstrip(" \u2014-") + "\u2026"


def _meta_description(soup: BeautifulSoup) -> str:
    """Return the og:description / meta description, if it looks real."""
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or "").lower()
        if key in ("og:description", "description"):
            content = (m.get("content") or "").strip()
            if len(content) < MIN_SUMMARY_LEN:
                continue
            if _GENERIC_META_RE.search(content):
                continue
            return content
    return ""


def _lead_summary(soup: BeautifulSoup) -> str:
    """Return the first 1-3 meaningful paragraphs of the article body."""
    scope = soup.find("article") or soup.find("main") or soup.body or soup
    parts = []
    for p in scope.find_all("p"):
        text = clean(p.get_text())
        if len(text) < LEAD_PARA_MIN:
            continue
        parts.append(text)
        if len(parts) >= LEAD_PARAGRAPHS:
            break
    return " ".join(parts)


def enrich_article(art: dict, base: str | None = None) -> tuple[str, str]:
    """Fetch an article page once and return (summary, source).

    `source` is one of {"meta", "lead", ""}. `art` must contain "link".
    If `link` is relative, `base` (the source's base URL) is used to resolve
    it. Returns ("", "") on any fetch/parse failure so the caller can keep
    the existing summary.
    """
    link = (art.get("link") or "").strip()
    if not link:
        return "", ""
    url = urljoin(base, link) if base else link
    if not is_http_url(url):
        return "", ""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        resp.close()
    except Exception as e:
        log.warning("Enrich fetch failed for %s: %s", url, e)
        return "", ""

    meta = _meta_description(soup)
    if meta:
        return _truncate(meta), "meta"
    lead = _lead_summary(soup)
    del soup  # release DOM before next iteration
    if lead:
        return _truncate(lead), "lead"
    return "", ""


def extract_content(url: str, base: str | None = None, max_chars: int = 6000) -> list[str]:
    """Fetch a page and return its main body as a list of paragraphs (capped).

    Used by the in-app reader. Returns [] on any failure so the UI can fall
    back to 'open original'.
    """
    link = (url or "").strip()
    if not link:
        return []
    link = urljoin(base, link) if base else link
    if not is_http_url(link):
        return []
    try:
        resp = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        resp.close()
    except Exception:
        return []
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "aside",
                "form", "button"):
        for t in soup.find_all(tag):
            t.decompose()
    scope = soup.find("article") or soup.find("main") or soup.body or soup
    paras: list[str] = []
    total = 0
    for p in scope.find_all("p"):
        text = clean(p.get_text())
        if len(text) < 25:
            continue
        paras.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return paras
