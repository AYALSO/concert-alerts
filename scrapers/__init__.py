# Import every scraper module here so it registers itself on startup.
from . import example   # noqa: F401
from . import grayclub  # noqa: F401

# Remaining site scrapers get added below as we build and verify each one:
# from . import barby        # noqa: F401
# from . import zappa        # noqa: F401
# from . import kupot_ta     # noqa: F401
