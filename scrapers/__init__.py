# Import every scraper module here so it registers itself on startup.
from . import example   # noqa: F401
from . import barby     # noqa: F401

# TEMP disabled: grayclub's heading-walk mislabels ~30/80 entries with city
# names (תלאביב/יהוד/מודיעין) instead of artists. Re-enable after fixing the
# title selector against the live homepage DOM.
# from . import grayclub  # noqa: F401

# Remaining site scrapers get added below as we build and verify each one:
# from . import zappa        # noqa: F401  (Eventim API recipe is in CLAUDE.md)
# from . import kupot_ta     # noqa: F401
