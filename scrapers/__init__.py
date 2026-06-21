# Import every scraper module here so it registers itself on startup.
from . import example   # noqa: F401
from . import barby     # noqa: F401
from . import eventim   # noqa: F401  (Eventim IL / zappa-club, via the Eventim API)
from . import grayclub  # noqa: F401  (card-based parser; reads h3 titles, not city h2)
from . import kupat     # noqa: F401  (Kupat Tel Aviv aggregator; per-show detail pages)
from . import comy      # noqa: F401  (COMY stand-up; one Show per touring event)
from . import comedybar # noqa: F401  (Comedy Bar via SmartTicket; named artists only)
# from . import kupot_ta     # noqa: F401
