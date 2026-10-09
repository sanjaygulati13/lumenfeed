"""Curated RSS sources for Lumenfeed feed."""

SOURCES = [
    # --- AI / ML Blogs (personal, high-signal) ---
    {
        "name": "Lilian Weng",
        "category": "AI/ML",
        "url": "https://lilianweng.github.io/index.xml",
    },
    {
        "name": "Simon Willison",
        "category": "AI/ML",
        "url": "https://simonwillison.net/atom/everything/",
    },
    {
        "name": "Chip Huyen",
        "category": "AI/ML",
        "url": "https://huyenchip.com/feed.xml",
    },
    {
        "name": "Jay Alammar",
        "category": "AI/ML",
        "url": "https://jalammar.github.io/feed.xml",
    },

    # --- AI Lab / Company Blogs ---
    {
        "name": "Hugging Face",
        "category": "AI Labs",
        "url": "https://huggingface.co/blog/feed.xml",
    },
    {
        "name": "Google AI",
        "category": "AI Labs",
        "url": "https://blog.google/technology/ai/rss/",
    },
    {
        "name": "DeepMind",
        "category": "AI Labs",
        "url": "https://deepmind.google/blog/rss.xml",
    },
    {
        "name": "Microsoft Research",
        "category": "AI Labs",
        "url": "https://www.microsoft.com/en-us/research/feed/",
    },
    {
        "name": "Anthropic",
        "category": "AI Labs",
        "url": "https://www.anthropic.com/news",
        "type": "rsc",
    },
    {
        "name": "Claude Dev Blog",
        "category": "AI Labs",
        "url": "https://claude.dev/rss.xml",
    },
    {
        "name": "OpenAI",
        "category": "AI Labs",
        "url": "https://openai.com/blog/rss.xml",
    },
    {
        "name": "Mistral AI",
        "category": "AI Labs",
        "url": "https://mistral.ai/news",
        "type": "scrape",
    },

    # --- Programming Languages ---
    {
        "name": "Rust Blog",
        "category": "Languages",
        "url": "https://blog.rust-lang.org/feed.xml",
    },
    {
        "name": "Go Blog",
        "category": "Languages",
        "url": "https://go.dev/blog/feed.atom",
    },
    {
        "name": "Python Blog",
        "category": "Languages",
        "url": "https://blog.python.org/feeds/posts/default",
    },

    # --- Tech News ---
    {
        "name": "Hacker News",
        "category": "Tech News",
        "url": "https://hnrss.org/best?points=50",
    },
    {
        "name": "TechCrunch AI",
        "category": "Tech News",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
    },

    # --- Research / Papers ---
    {
        "name": "arXiv cs.LG",
        "category": "Research",
        "url": "https://rss.arxiv.org/rss/cs.LG",
    },

    # --- Physical AI / Robotics ---
    {
        "name": "TechCrunch Robotics",
        "category": "Physical AI",
        "url": "https://techcrunch.com/tag/robotics/feed/",
    },
]
