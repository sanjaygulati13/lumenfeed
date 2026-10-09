# Contributing

Thanks for taking a look. This is a small project, so the process is pretty informal.

## Setup

```bash
git clone https://github.com/sanjaygulati13/lumenfeed.git
cd lumenfeed
make install   # creates .venv with the app, pytest and ruff
make test
```

The tests use a throwaway database and don't need the network.

## Adding a source

This is the most useful thing you can send. Add an entry to `lumenfeed/sources.py`:

```python
{
    "name": "My Favorite Blog",
    "category": "AI/ML",  # AI/ML, AI Labs, Languages, Tech News, Research, Physical AI
    "url": "https://example.com/feed.xml",
}
```

A few things I look for:

- It should be about tech or AI, and post things worth reading. A feed that publishes 50 times a day will over .
- It needs an RSS/Atom feed. If it doesn't have one, `"type": "scrape"` on a stable listing page can work, but check what it actually pulls.
- Use one of the existing categories. The filter row in `templates/index.html` is hardcoded, so a new category means a UI change too.
- Run it locally and make sure stories show up. If the logs say `Feed error` or `Scrape failed`, it isn't working.

A source that used to work and stopped is a bug, so feel free to open an issue for that.

## Other changes

The frontend is all in `lumenfeed/templates/index.html`. The backend is a handful of modules in `lumenfeed/` (the README has a quick map).

Some things I'd like to keep true:

- No build step, no JS framework, nothing loaded from a CDN.
- Nothing about what someone reads gets sent anywhere. Fetching feeds and article pages is fine; analytics or third-party APIs aren't.
- The ranking should stay simple enough to explain in a paragraph.

If just in case you are planning something big, open an issue first so we can talk about it before you sink time into it.

## Before you open a PR

```bash
make test
.venv/bin/ruff check .
```

CI runs both on Python 3.10, 3.11 and 3.12. Keep a PR to one change if you can, and say why you're making it, not just what it does.
