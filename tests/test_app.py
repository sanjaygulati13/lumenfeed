"""Basic tests for Lumenfeed."""

# IMPORTANT: point at an isolated temp DB *before* importing app, because app
# resolves DATA_DIR from the environment at import time. Setting it after the
# import (the old order) silently made every test hit the real data/articles.db.
import os
from pathlib import Path

os.environ["LUMEN_DATA"] = "/tmp/lumen_test_data"

import pytest
from fastapi.testclient import TestClient

from lumenfeed.api import app
from lumenfeed.db import get_db, init_db
from lumenfeed.feeds import clean_html, truncate


@pytest.fixture(autouse=True)
def setup_db():
    """Fresh DB for each test."""
    import shutil
    test_dir = Path("/tmp/lumen_test_data")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    init_db()
    yield
    if test_dir.exists():
        shutil.rmtree(test_dir)


class TestUtils:
    def test_clean_html_basic(self):
        assert clean_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_clean_html_empty(self):
        assert clean_html("") == ""
        assert clean_html(None) == ""

    def test_clean_html_strips_read_more(self):
        assert clean_html("Some text Read More") == "Some text"

    def test_truncate_short(self):
        assert truncate("short text") == "short text"

    def test_truncate_long(self):
        text = "word " * 100
        result = truncate(text, 50)
        assert len(result) <= 51  # 50 + ellipsis
        assert result.endswith("\u2026")


class TestAPI:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Lumenfeed" in resp.text

    def test_favicon(self, client):
        resp = client.get("/favicon.svg")
        assert resp.status_code == 200
        assert "svg" in resp.headers["content-type"]

    def test_manifest(self, client):
        resp = client.get("/manifest.json")
        assert resp.status_code == 200
        import json
        data = json.loads(resp.text)
        assert data["name"] == "Lumenfeed"

    def test_articles_empty(self, client):
        resp = client.get("/api/articles?limit=5")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_articles_invalid_mode(self, client):
        resp = client.get("/api/articles?mode=invalid")
        assert resp.status_code == 400

    def test_preferences_empty(self, client):
        resp = client.get("/api/preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_reads"] == 0

    def test_mark_read_not_found(self, client):
        resp = client.post("/api/articles/99999/read")
        assert resp.status_code == 404

    def test_summary_endpoint_not_found(self, client):
        assert client.get("/api/articles/99999/summary").status_code == 404

    def test_summary_endpoint_enriches(self, client, monkeypatch):
        import lumenfeed.api as appmod
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link) VALUES (?,?,?,?,?)",
                ("Test article", "Short", "X", "AI/ML", "https://example.com/x"),
            )
            db.commit()
            row = db.execute("SELECT id FROM articles WHERE link='https://example.com/x'").fetchone()
        monkeypatch.setattr(
            appmod, "enrich_article",
            lambda art, base=None: ("A nicely enriched, sufficiently long summary from the article page.", "meta"),
        )
        resp = client.get(f"/api/articles/{row['id']}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enriched"] is True
        assert data["summary_source"] == "meta"
        with get_db() as db:
            r = db.execute("SELECT summary_source, enriched_at FROM articles WHERE id=?", (row["id"],)).fetchone()
        assert r["summary_source"] == "meta"
        assert r["enriched_at"] is not None

    def test_summary_endpoint_cache_hit(self, client, monkeypatch):
        import lumenfeed.api as appmod
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link) VALUES (?,?,?,?,?)",
                ("T", "This is a long enough existing summary that does not need enrichment at all.",
                 "X", "AI/ML", "https://example.com/y"),
            )
            db.commit()
            row = db.execute("SELECT id FROM articles WHERE link='https://example.com/y'").fetchone()
        called = []
        monkeypatch.setattr(appmod, "enrich_article", lambda art, base=None: called.append(1) or ("", ""))
        resp = client.get(f"/api/articles/{row['id']}/summary")
        data = resp.json()
        assert data["enriched"] is False
        assert data["summary_source"] == "rss"
        assert not called  # never fetched for a good existing summary

    def test_summary_endpoint_marks_failed_no_retry(self, client, monkeypatch):
        import lumenfeed.api as appmod
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link) VALUES (?,?,?,?,?)",
                ("Paywalled", "Short", "X", "AI/ML", "https://paywalled.example/a"),
            )
            db.commit()
            row = db.execute("SELECT id FROM articles WHERE link='https://paywalled.example/a'").fetchone()
        calls = []
        def fake(art, base=None):
            calls.append(1)
            return ("", "")
        monkeypatch.setattr(appmod, "enrich_article", fake)
        d1 = client.get(f"/api/articles/{row['id']}/summary").json()
        assert d1["enriched"] is False and d1["summary_source"] == "failed"
        d2 = client.get(f"/api/articles/{row['id']}/summary").json()
        assert d2["summary_source"] == "failed"
        assert len(calls) == 1  # only fetched once, then marked failed
        with get_db() as db:
            r = db.execute("SELECT summary_source, enriched_at FROM articles WHERE id=?", (row["id"],)).fetchone()
        assert r["summary_source"] == "failed" and r["enriched_at"] is not None


class TestMoreEndpoints:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _insert(self, link="https://x.example/p"):
        with get_db() as db:
            db.execute("INSERT INTO articles (title, summary, source, category, link) VALUES (?,?,?,?,?)",
                       ("A post", "A long enough summary for testing purposes here.", "X", "AI/ML", link))
            db.commit()
            return db.execute("SELECT id FROM articles WHERE link=?", (link,)).fetchone()["id"]

    def test_save_list_unsave(self, client):
        aid = self._insert()
        assert client.post(f"/api/articles/{aid}/save").json()["saved"] is True
        saved = client.get("/api/saved").json()
        assert any(a["id"] == aid and a["is_saved"] for a in saved)
        assert client.delete(f"/api/articles/{aid}/save").json()["saved"] is False
        assert all(a["id"] != aid for a in client.get("/api/saved").json())

    def test_articles_return_is_saved(self, client):
        aid = self._insert("https://x.example/isv1")
        arts = {a["id"]: a for a in client.get("/api/articles?mode=all&limit=50").json()}
        assert arts[aid]["is_saved"] is False
        client.post(f"/api/articles/{aid}/save")
        arts = {a["id"]: a for a in client.get("/api/articles?mode=all&limit=50").json()}
        assert arts[aid]["is_saved"] is True

    def test_saved_count(self, client):
        assert client.get("/api/saved/count").json() == {"count": 0}
        a1 = self._insert("https://x.example/sc1")
        a2 = self._insert("https://x.example/sc2")
        client.post(f"/api/articles/{a1}/save")
        client.post(f"/api/articles/{a2}/save")
        assert client.get("/api/saved/count").json() == {"count": 2}

    def test_content_endpoint(self, client, monkeypatch):
        import lumenfeed.api as appmod
        aid = self._insert()
        monkeypatch.setattr(appmod, "extract_content", lambda *a, **k: ["First paragraph of the article body here.", "Second paragraph."])
        d = client.get(f"/api/articles/{aid}/content").json()
        assert d["title"] == "A post"
        assert d["paragraphs"][0].startswith("First paragraph")

    def test_digest(self, client):
        import time
        now = int(time.time())
        with get_db() as db:
            db.execute("INSERT INTO articles (title, source, category, link, published_ts) VALUES (?,?,?,?,?)",
                       ("This week", "X", "AI/ML", "https://x.example/new", now - 3600))
            db.execute("INSERT INTO articles (title, source, category, link, published_ts) VALUES (?,?,?,?,?)",
                       ("Last month", "X", "Research", "https://x.example/old", now - 30 * 86400))
            db.commit()
        d = client.get("/api/digest").json()
        assert set(d.keys()) == {"total", "categories", "highlights"}
        assert d["total"] == 1
        assert [c["name"] for c in d["categories"]] == ["AI/ML"]
        assert [h["title"] for h in d["highlights"]] == ["This week"]


class TestPersonalizationAndDismiss:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _insert_many(self, n=10, source="X", category="AI/ML"):
        with get_db() as db:
            for i in range(n):
                db.execute("INSERT INTO articles (title, summary, source, category, link) VALUES (?,?,?,?,?)",
                           (f"Post {i}", "A long enough summary to pass the minimum length check for testing.",
                            source, category, f"https://x.example/p{i}"))
            db.commit()
            rows = db.execute("SELECT id FROM articles ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def test_dismiss_hidden_from_every_feed(self, client):
        ids = self._insert_many(10)
        target = ids[0]
        r = client.post(f"/api/articles/{target}/dismiss?kind=source")
        assert r.status_code == 200 and r.json()["kind"] == "source"
        pers = [a["id"] for a in client.get("/api/articles?mode=personalized&limit=50").json()]
        al = [a["id"] for a in client.get("/api/articles?mode=all&limit=50").json()]
        assert target not in pers  # For You
        assert target not in al    # All: a swipe hides it everywhere and persists

    def test_read_hidden_from_every_feed(self, client):
        ids = self._insert_many(10)
        target = ids[0]
        assert client.post(f"/api/articles/{target}/read").status_code == 200
        pers = [a["id"] for a in client.get("/api/articles?mode=personalized&limit=50").json()]
        al = [a["id"] for a in client.get("/api/articles?mode=all&limit=50").json()]
        assert target not in pers  # For You
        assert target not in al    # All: read content no longer resurfaces

    def test_personalized_exclude_no_dup_no_gap(self, client):
        self._insert_many(30)
        seen: list = []
        exclude = ""
        for _ in range(6):
            q = "/api/articles?mode=personalized&limit=10"
            if exclude:
                q += f"&exclude={exclude}"
            arts = client.get(q).json()
            page_ids = [a["id"] for a in arts]
            assert not [i for i in page_ids if i in seen], "personalized page had duplicates"
            seen += page_ids
            if len(page_ids) < 10:
                break
            exclude = ",".join(map(str, seen))
        assert len(set(seen)) == 30, f"expected all 30 articles, saw {len(set(seen))}"

    def test_dismiss_not_found(self, client):
        assert client.post("/api/articles/99999/dismiss").status_code == 404

    def test_dismiss_bad_kind(self, client):
        ids = self._insert_many(2)
        assert client.post(f"/api/articles/{ids[0]}/dismiss?kind=bogus").status_code == 400


class TestScrape:
    FAKE = """
    <html><body>
    <header><nav>
      <a href="https://claude.com/solutions/financial-services">Financial services</a>
      <a href="/engineering">Engineering at Anthropic</a>
    </nav></header>
    <main><ul>
      <li><a href="/news/real-post">
        <time>Jan 5, 2026</time>
        <span class="subject">Announcements</span>
        <span class="title">A real post title that is long enough</span>
        <p>A lead paragraph that is definitely long enough to be captured by the summary picker here.</p>
      </a></li>
      <li><a href="/news/another"><h3>Another genuine post headline</h3></a></li>
    </ul></main>
    <footer><nav><a href="/economic-futures">Economic Futures</a></nav></footer>
    </body></html>
    """

    def _scrape(self, monkeypatch):
        import lumenfeed.feeds as appmod
        class FakeResp:
            content = property(lambda self: self.text.encode())
            text = TestScrape.FAKE
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: FakeResp())
        return appmod.fetch_scrape({"name": "TestCo", "category": "X", "url": "https://example.com/news"})

    def test_skips_nav_and_extracts_clean_fields(self, monkeypatch):
        arts = self._scrape(monkeypatch)
        assert len(arts) == 2
        titles = [a["title"] for a in arts]
        # nav/footer junk excluded
        assert "Financial services" not in titles
        assert not any("engineering" in a["link"] or "economic-futures" in a["link"] for a in arts)
        # clean fields from the .title span, <time>, and <p>
        post = next(a for a in arts if a["link"] == "https://example.com/news/real-post")
        assert post["title"] == "A real post title that is long enough"
        assert post["published"] == "Jan 5, 2026"
        assert post["summary"].startswith("A lead paragraph")
        # heading-based title
        assert next(a for a in arts if a["link"] == "https://example.com/news/another")["title"] == "Another genuine post headline"


class TestEnrich:
    def test_needs_enrichment_empty(self):
        from lumenfeed.enrich import needs_enrichment
        assert needs_enrichment("") is True
        assert needs_enrichment(None) is True

    def test_needs_enrichment_short(self):
        from lumenfeed.enrich import needs_enrichment
        assert needs_enrichment("Mistral Small 4") is True

    def test_needs_enrichment_boilerplate(self):
        from lumenfeed.enrich import needs_enrichment
        s = "Article URL: https://x.com/a Comments URL: https://news.ycombinator.com/item?id=1"
        assert needs_enrichment(s) is True

    def test_needs_enrichment_good(self):
        from lumenfeed.enrich import needs_enrichment
        assert needs_enrichment("A perfectly fine and reasonably long summary of the article here today.") is False

    def test_enrich_article_meta(self, monkeypatch):
        import lumenfeed.enrich as enrich
        class FakeResp:
            content = property(lambda self: self.text.encode())
            text = ('<html><head><meta property="og:description" '
                    'content="A real, sufficiently long description of the article for enrichment testing.">'
                    '</head><body></body></html>')
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(enrich.requests, "get", lambda *a, **k: FakeResp())
        summary, src = enrich.enrich_article({"link": "https://example.com/a"})
        assert src == "meta"
        assert "real" in summary

    def test_enrich_article_lead(self, monkeypatch):
        import lumenfeed.enrich as enrich
        class FakeResp:
            content = property(lambda self: self.text.encode())
            text = ('<html><body><article>'
                    '<p>First paragraph of the article body that is long enough to be captured by the lead extractor.</p>'
                    '<p>Second paragraph of the article body that is also long enough to be captured as well.</p>'
                    '</article></body></html>')
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(enrich.requests, "get", lambda *a, **k: FakeResp())
        summary, src = enrich.enrich_article({"link": "https://example.com/b"})
        assert src == "lead"
        assert "First paragraph" in summary

    def test_enrich_article_failure(self, monkeypatch):
        import lumenfeed.enrich as enrich
        def boom(*a, **k): raise RuntimeError("network down")
        monkeypatch.setattr(enrich.requests, "get", boom)
        assert enrich.enrich_article({"link": "https://example.com/c"}) == ("", "")

    def test_enrich_article_resolves_relative(self, monkeypatch):
        import lumenfeed.enrich as enrich
        seen = {}
        class FakeResp:
            content = property(lambda self: self.text.encode())
            text = ('<html><head><meta property="og:description" '
                    'content="A sufficiently long resolved description from the resolved relative article page.">'
                    '</head><body></body></html>')
            def raise_for_status(self): pass
            def close(self): pass
        def fake_get(url, *a, **k):
            seen["url"] = url
            return FakeResp()
        monkeypatch.setattr(enrich.requests, "get", fake_get)
        summary, src = enrich.enrich_article({"link": "/news/foo"}, base="https://www.anthropic.com")
        assert seen["url"] == "https://www.anthropic.com/news/foo"
        assert src == "meta"

    def test_enrich_article_skips_generic_meta(self, monkeypatch):
        import lumenfeed.enrich as enrich
        class FakeResp:
            content = property(lambda self: self.text.encode())
            text = ('<html><head><meta property="og:description" '
                    'content="A Blog post by Someone on Hugging Face"></head>'
                    '<body><article><p>A real lead paragraph from the article that is long enough to be captured by the extractor.</p></article></body></html>')
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(enrich.requests, "get", lambda *a, **k: FakeResp())
        summary, src = enrich.enrich_article({"link": "https://example.com/hf"})
        assert src == "lead"  # generic template meta skipped
        assert "real lead paragraph" in summary


class TestChronologicalFeed:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _insert(self, title, source, published_ts, category="AI/ML"):
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link, published, published_ts, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (title, "A long enough summary to pass the minimum length check for testing.",
                 source, category, f"https://x.example/{title}",
                 "2026-01-01T00:00:00+00:00", published_ts, "2026-01-01T00:00:00+00:00"),
            )
            db.commit()

    def test_all_mode_orders_by_publish_time_across_sources(self, client):
        # Inserted so that id order != publish order, across distinct sources.
        self._insert("old-robotics", "SourceA", 1000)   # id 1, oldest
        self._insert("new-ai", "SourceB", 3000)         # id 2, newest
        self._insert("mid-hn", "SourceC", 2000)         # id 3, middle
        arts = client.get("/api/articles?mode=all&limit=10").json()
        assert [a["title"] for a in arts] == ["new-ai", "mid-hn", "old-robotics"]
        # sources interleaved by time, NOT clustered by fetch order (id desc)
        assert [a["source"] for a in arts] == ["SourceB", "SourceC", "SourceA"]

    def test_composite_cursor_paginates_without_gap_or_dup(self, client):
        for i in range(7):
            self._insert(f"p{i}", "S", 100 + i)  # p0 oldest ... p6 newest
        page1 = client.get("/api/articles?mode=all&limit=3").json()
        assert [a["title"] for a in page1] == ["p6", "p5", "p4"]
        last = page1[-1]
        page2 = client.get(
            f"/api/articles?mode=all&limit=3&after_ts={last['published_ts']}&after_id={last['id']}"
        ).json()
        assert [a["title"] for a in page2] == ["p3", "p2", "p1"]
        last2 = page2[-1]
        page3 = client.get(
            f"/api/articles?mode=all&limit=3&after_ts={last2['published_ts']}&after_id={last2['id']}"
        ).json()
        assert [a["title"] for a in page3] == ["p0"]
        all_titles = [a["title"] for a in page1 + page2 + page3]
        assert len(all_titles) == len(set(all_titles)) == 7  # no duplicates, no gaps

    def test_null_publish_ts_sorts_last(self, client):
        self._insert("dated", "S", 5000)
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link, published, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("undated", "A long enough summary to pass the minimum length check for testing.",
                 "S", "AI/ML", "https://x.example/undated", "", "2026-01-01T00:00:00+00:00"),
            )
            db.commit()
        titles = [a["title"] for a in client.get("/api/articles?mode=all&limit=10").json()]
        assert titles.index("undated") > titles.index("dated")  # undated falls after dated


class TestFeedBalance:
    """Guarantees that one chatty source (Hacker News) can't dominate the
    personalized "For You" feed — the fix for the HN-only feed problem."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _insert(self, title, source, published_ts, category="Tech News"):
        with get_db() as db:
            db.execute(
                "INSERT INTO articles (title, summary, source, category, link, published, published_ts, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (title, "A long enough summary to pass the minimum length check for testing.",
                 source, category, f"https://x.example/{title}",
                 "2026-01-01T00:00:00+00:00", published_ts, "2026-01-01T00:00:00+00:00"),
            )
            db.commit()

    def _reads(self, n, source, category="Tech News"):
        """Log n reads across the top-n articles of a source (drives preference)."""
        with get_db() as db:
            rows = db.execute(
                "SELECT id FROM articles WHERE source=? ORDER BY published_ts DESC LIMIT ?",
                (source, n),
            ).fetchall()
            now = "2026-09-01T00:00:00+00:00"
            for r in rows:
                db.execute(
                    "INSERT OR IGNORE INTO reads (article_id, source, category, read_at) "
                    "VALUES (?,?,?,?)", (r["id"], source, category, now),
                )
            db.commit()

    def test_diversify_caps_dominant_source(self):
        from collections import Counter

        from lumenfeed.personalization import diversify

        scored = [{"source": "HN", "title": f"hn{i}"} for i in range(25)]
        for s in ("B", "C", "D", "E"):
            scored += [{"source": s, "title": f"{s}{i}"} for i in range(5)]

        picked = diversify(scored, limit=15, per_source_cap=3)
        c = Counter(a["source"] for a in picked)
        assert len(picked) == 15
        assert c["HN"] <= 3          # the chatty source is capped
        assert len(c) >= 4           # several other sources get through

    def test_diversify_few_sources_still_balanced(self):
        from collections import Counter

        from lumenfeed.personalization import diversify

        # Only 3 sources exist; HN is far bigger. We can't hit cap 3 on all of
        # them, but HN must be capped well below its uncapped top-N share.
        scored = [{"source": "HN", "title": f"hn{i}"} for i in range(20)]
        scored += [{"source": "A", "title": f"a{i}"} for i in range(10)]
        scored += [{"source": "B", "title": f"b{i}"} for i in range(10)]

        picked = diversify(scored, limit=15, per_source_cap=3)
        c = Counter(a["source"] for a in picked)
        assert len(picked) == 15
        assert c["HN"] < 10          # capped, not the ~15 a plain top-N would give
        assert set(c) == {"HN", "A", "B"}  # every available source is represented

    def test_diversify_empty_and_trivial(self):
        from lumenfeed.personalization import diversify

        assert diversify([], limit=10) == []
        one = [{"source": "X", "title": "only"}]
        assert len(diversify(one, limit=10)) == 1

    def test_personalized_feed_is_balanced(self, client):
        from collections import Counter

        # A chatty dominant source with many *unread* stories in the pool, plus
        # a handful of quieter sources with fresh stories.
        for i in range(25):
            self._insert(f"hn{i}", "Hacker News", 6000 - i)
        for i, s in enumerate(("Simon Willison", "OpenAI", "Hugging Face", "arXiv cs.LG")):
            for j in range(6):
                self._insert(f"{s}-{j}", s, 5000 - i * 100 - j)
        # A little HN read-history so the preference loop would otherwise boost
        # it, but most HN stays unread in the pool.
        self._reads(6, "Hacker News")

        arts = client.get("/api/articles?mode=personalized&limit=15").json()
        c = Counter(a["source"] for a in arts)
        assert len(arts) == 15
        assert c["Hacker News"] <= 3        # capped, not dominating
        assert len(c) >= 4                  # a genuinely mixed page
        # A plain top-N (no quota) would have surfaced mostly HN; assert we
        # didn't by checking non-HN stories make up a healthy share.
        assert sum(v for k, v in c.items() if k != "Hacker News") >= 10

    def test_all_feed_unchanged_by_balance(self, client):
        from collections import Counter
        # The chronological "All" feed is intentionally NOT quota-capped.
        for i in range(8):
            self._insert(f"hn{i}", "Hacker News", 6000 - i)
        for i in range(4):
            self._insert(f"other-{i}", "OpenAI", 5000 - i)
        arts = client.get("/api/articles?mode=all&limit=12").json()
        c = Counter(a["source"] for a in arts)
        assert c["Hacker News"] == 8  # all 8 newest, no capping applied


class TestRefresh:
    def test_refresh_skips_when_already_running(self, monkeypatch):
        import lumenfeed.feeds as feeds
        monkeypatch.setattr(feeds, "fetch_feed", lambda s: [])
        assert feeds.refresh_all_feeds() == 0
        with feeds._refresh_lock:
            assert feeds.refresh_all_feeds() is None
            d = TestClient(app).post("/api/refresh").json()
        assert d == {"new_articles": 0, "already_running": True}


class TestLinkSafety:
    def test_is_http_url(self):
        from lumenfeed.enrich import is_http_url
        assert is_http_url("https://example.com/a")
        assert is_http_url("http://example.com")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "/rel", "", None):
            assert not is_http_url(bad)

    def test_refresh_skips_non_http_links(self, monkeypatch):
        import lumenfeed.feeds as feeds
        arts = [
            {"title": "Good", "summary": "", "source": "X", "category": "AI/ML",
             "link": "https://x.example/good", "published": "", "published_ts": None},
            {"title": "Bad", "summary": "", "source": "X", "category": "AI/ML",
             "link": "javascript:alert(1)", "published": "", "published_ts": None},
        ]
        monkeypatch.setattr(feeds, "SOURCES", [{"name": "X", "category": "AI/ML", "url": "u"}])
        monkeypatch.setattr(feeds, "fetch_feed", lambda s: arts)
        assert feeds.refresh_all_feeds() == 1
        with get_db() as db:
            links = [r["link"] for r in db.execute("SELECT link FROM articles")]
        assert links == ["https://x.example/good"]

    def test_enrich_refuses_non_http(self, monkeypatch):
        import lumenfeed.enrich as enrich
        monkeypatch.setattr(enrich.requests, "get", lambda *a, **k: pytest.fail("fetched"))
        assert enrich.enrich_article({"link": "file:///etc/passwd"}) == ("", "")
        assert enrich.extract_content("javascript:alert(1)") == []


class TestEncoding:
    def test_enrich_uses_page_charset_not_latin1(self, monkeypatch):
        # Server says "text/html" with no charset; requests would decode .text
        # as ISO-8859-1 and turn the em dash into mojibake.
        import lumenfeed.enrich as enrich
        html = ('<html><head><meta charset="utf-8"><meta name="description" '
                'content="A car with internet access \u2014 Wi-Fi, cellular, GPS \u2014 talks to its maker.">'
                '</head></html>').encode("utf-8")

        class FakeResp:
            content = html
            text = html.decode("iso-8859-1")
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(enrich.requests, "get", lambda *a, **k: FakeResp())
        summary, src = enrich.enrich_article({"link": "https://example.com/car"})
        assert "access \u2014 Wi-Fi" in summary


class TestArxiv:
    PREFIX = "arXiv:2610.02391v1 Announce Type: replace-cross Abstract: "

    def test_rss_strips_announce_prefix(self, monkeypatch):
        import lumenfeed.feeds as feeds

        class Entry:
            title = "A paper"
            summary = "<p>" + self.PREFIX + "When a large language model solves a problem.</p>"
            link = "https://arxiv.org/abs/2610.02391"
            published = "Mon, 05 Oct 2026 04:00:00 -0400"

        class Parsed:
            bozo = False
            entries = [Entry()]

        class FakeResp:
            content, url, headers = b"", "https://rss.arxiv.org/rss/cs.LG", {}
            def raise_for_status(self): pass
            def close(self): pass
        monkeypatch.setattr(feeds.requests, "get", lambda *a, **k: FakeResp())
        monkeypatch.setattr(feeds.feedparser, "parse", lambda *a, **k: Parsed())
        arts = feeds.fetch_rss({"name": "arXiv cs.LG", "category": "Research", "url": "u"})
        assert arts[0]["summary"] == "When a large language model solves a problem."

    def test_strip_existing_rows(self):
        from lumenfeed.feeds import strip_arxiv_boilerplate
        with get_db() as db:
            db.execute("INSERT INTO articles (title, summary, link) VALUES (?,?,?)",
                       ("p", self.PREFIX + "Looped Transformers apply the same layers.", "https://arxiv.org/abs/1"))
            db.commit()
        assert strip_arxiv_boilerplate() == 1
        assert strip_arxiv_boilerplate() == 0
        with get_db() as db:
            assert db.execute("SELECT summary FROM articles").fetchone()["summary"] == "Looped Transformers apply the same layers."
