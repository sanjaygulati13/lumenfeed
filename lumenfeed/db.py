"""SQLite schema and connection management for Lumenfeed."""

import sqlite3
from contextlib import contextmanager

from .config import DATA_DIR, DB_PATH


@contextmanager
def get_db():
    """Get a database connection (context manager)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_columns(db) -> None:
    """Add columns to an existing articles table if missing (forward migration).

    New installs get these columns straight from the CREATE TABLE below; this
    only matters for databases created by an older version of the app.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(articles)").fetchall()}
    if "summary_source" not in existing:
        db.execute("ALTER TABLE articles ADD COLUMN summary_source TEXT")
    if "enriched_at" not in existing:
        db.execute("ALTER TABLE articles ADD COLUMN enriched_at TEXT")
    if "published_ts" not in existing:
        # Normalized unix epoch (seconds) used for chronological ordering with
        # a mix of sources. Populated by feeds.normalize_dates() and at ingest.
        db.execute("ALTER TABLE articles ADD COLUMN published_ts INTEGER")


def init_db() -> None:
    """Initialize database schema (idempotent)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT,
                category TEXT,
                link TEXT UNIQUE NOT NULL,
                published TEXT,
                published_ts INTEGER,
                fetched_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS reads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL UNIQUE,
                source TEXT,
                category TEXT,
                read_at TEXT NOT NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS saves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL UNIQUE,
                saved_at TEXT NOT NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS dismissals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL UNIQUE,
                source TEXT,
                category TEXT,
                kind TEXT NOT NULL DEFAULT 'source',
                created_at TEXT NOT NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id)
            )
        """)
        # Ensure all columns exist before creating indexes that depend on them
        # (migrates databases created by older versions of the app).
        _ensure_columns(db)
        db.execute("CREATE INDEX IF NOT EXISTS idx_articles_id ON articles(id DESC)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_ts ON articles(published_ts DESC, id DESC)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_reads_article ON reads(article_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_reads_time ON reads(read_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_saves_article ON saves(article_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_dismissals_article ON dismissals(article_id)")
        db.commit()
