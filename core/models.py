from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_artist(name: str) -> str:
    """Key used to match artists across sites and to dedupe favorites.

    Keeps Hebrew + Latin letters and digits, lowercases, strips punctuation
    and extra whitespace. "Radiohead!" and "radiohead" map to the same key.
    """
    s = _clean(name).lower()
    s = re.sub(r"[^\w\s\u0590-\u05FF]", "", s)      # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class Show:
    """A single concert, in a normalized shape every scraper must return."""

    artist: str                 # clean artist name, used for grouping/following
    date_raw: str               # date string exactly as shown on the site
    venue: str
    url: str                    # direct link to this show, ideally
    source: str                 # scraper key, e.g. "zappa"
    date_iso: Optional[str] = None   # "YYYY-MM-DD" if the scraper parsed it
    title: Optional[str] = None      # full show title as shown (if richer than artist)
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def artist_key(self) -> str:
        return normalize_artist(self.artist)

    @property
    def show_id(self) -> str:
        """Stable id used to detect whether we've seen this show before.

        The per-source event URL is unique per show and stable across scrapes,
        so it's the most reliable key (distinguishes same-day matinee/evening
        that share artist+venue). Falls back to artist+date+venue if a scraper
        ever yields no real per-show URL.
        """
        if self.url and "/" in self.url.split("//", 1)[-1]:
            basis = f"{self.source}|{self.url.strip().split('?', 1)[0]}"
        else:
            basis = "|".join([
                self.artist_key,
                (self.date_iso or _clean(self.date_raw).lower()),
                normalize_artist(self.venue),
                self.source,
            ])
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["show_id"] = self.show_id
        d["artist_key"] = self.artist_key
        return d
