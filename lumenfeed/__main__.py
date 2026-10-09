"""Enable running the app with ``python -m lumenfeed``.

Also serves as the target for the console-script entry point
(``lumenfeed = "lumenfeed.__main__:main"`` in pyproject.toml).
"""

from lumenfeed.api import main

if __name__ == "__main__":
    main()
