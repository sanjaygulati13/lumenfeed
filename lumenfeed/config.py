"""Runtime configuration for Lumenfeed.

Everything here is overridable via ``LUMEN_*`` environment variables so
the same code runs from a source checkout, a venv, or an installed wheel.

This module has no dependencies on the rest of the package, so it is safe to
import first (it is where the process-wide logger and the BeautifulSoup warning
filter are set up).
"""

import logging
import os
import warnings
from pathlib import Path

from bs4 import MarkupResemblesLocatorWarning

from . import __version__

# Quiet a noisy BeautifulSoup warning (anchor tags that resemble locators).
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lumenfeed")

# --- Paths ---------------------------------------------------------------
# The package directory holds the bundled frontend assets (templates/, static/).
PACKAGE_DIR = Path(__file__).resolve().parent

# In a source checkout the package lives at <repo>/lumenfeed, so the repo
# root (where ./data lives by default) is one level up. In an installed wheel
# there is no repo root, so we fall back to ./data relative to the current
# working directory. Either way the user can override with LUMEN_DATA.
_CANDIDATE_ROOT = PACKAGE_DIR.parent
_DATA_DEFAULT = (
    _CANDIDATE_ROOT / "data"
    if (_CANDIDATE_ROOT / "pyproject.toml").exists()
    else Path("data")
)

BASE_DIR = _CANDIDATE_ROOT
DATA_DIR = Path(os.environ.get("LUMEN_DATA", _DATA_DEFAULT))
DB_PATH = DATA_DIR / "articles.db"

INDEX_HTML = PACKAGE_DIR / "templates" / "index.html"
FAVICON = PACKAGE_DIR / "static" / "favicon.svg"
MANIFEST = PACKAGE_DIR / "static" / "manifest.json"

# --- Server --------------------------------------------------------------
# Loopback by default: there is no auth, so listening on every interface is
# opt-in. Set LUMEN_HOST=0.0.0.0 to reach it from other devices (e.g. a phone).
HOST = os.environ.get("LUMEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("LUMEN_PORT", "5024"))
REFRESH_INTERVAL_SECONDS = int(os.environ.get("LUMEN_REFRESH", "1800"))
FETCH_TIMEOUT = int(os.environ.get("LUMEN_TIMEOUT", "20"))
USER_AGENT = (
    f"Mozilla/5.0 (compatible; Lumenfeed/{__version__}; +https://github.com/sanjaygulati13/lumenfeed)"
)

# --- Summary enrichment (no LLM) -----------------------------------------
ENRICH_ENABLED = os.environ.get("LUMEN_ENRICH", "1") != "0"
ENRICH_BACKFILL_LIMIT = int(os.environ.get("LUMEN_ENRICH_LIMIT", "20"))
ENRICH_DELAY = float(os.environ.get("LUMEN_ENRICH_DELAY", "0.2"))

# --- Feed balance / personalization --------------------------------------
# The "For You" feed caps how many stories a single source may contribute per
# page so one chatty feed (e.g. Hacker News, which always has the newest and
# most high-point stories) can't dominate. 0 = auto (approx. limit/5, at least
# 1). Raise for a more even mix; lower toward 1 to let your favourite sources
# run longer before the cap bites.
FEED_PER_SOURCE_CAP = int(os.environ.get("LUMEN_PER_SOURCE_CAP", "0"))

# Preference scores are raised to this power before scoring. With 0 < gamma < 1
# the spread is compressed so the single most-read source (often HN, via the
# read-more -> rank-higher feedback loop) sits at 1.0 without steamrolling the
# rest toward 0. 1.0 disables compression; 0.5 = square root (a good default).
PREFERENCE_GAMMA = float(os.environ.get("LUMEN_PREF_GAMMA", "0.5"))
