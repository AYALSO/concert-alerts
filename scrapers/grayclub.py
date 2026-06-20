"""Gray Club (grayclub.co.il) scraper.

The homepage server-renders every upcoming show as a card (`div.article-list`):
each card holds an `<h3>` title, a date (DD.MM.YYYY) and one `/event/<a>/<b>/`
link. The earlier version walked *all* headings and caught the city-section
`<h2>` labels (תלאביב/יהוד/מודיעין) as artists; driving off the per-card `<h3>`
fixes that. We dedupe by the event path, drop past dates, and clean the artist
name (titles often read "X – מופע …"). Verified 75 upcoming shows on 2026-06-20.

403 from datacenter networks (anti-bot): if Actions logs show 403, the realistic
browser headers below may need refreshing.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List

import requests
from bs4 import BeautifulSoup

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
EVENT_RE = re.compile(r"/event/\d+/\d+/?")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
VENUE = "מועדון גריי"


@register
class GrayClubScraper(Scraper):
    name = "grayclub"
    HOME = "https://grayclub.co.il/"

    def fetch(self) -> List[Show]:
        r = requests.get(self.HOME, timeout=30, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        today = date.today()
        shows: dict[str, Show] = {}     # /event/<a>/<b>/ -> Show

        for card in soup.select("div.article-list"):
            h3 = card.find("h3")
            link_tag = card.find("a", href=EVENT_RE)
            if not h3 or not link_tag:
                continue
            title = h3.get_text(" ", strip=True)
            href = link_tag.get("href", "")
            key = EVENT_RE.search(href).group(0)
            if not title or key in shows:
                continue

            m = DATE_RE.search(card.get_text(" ", strip=True))
            if not m:
                continue
            dd, mm, yyyy = m.groups()
            date_iso = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
            try:
                if datetime.fromisoformat(date_iso).date() < today:
                    continue
            except ValueError:
                continue

            url = href if href.startswith("http") else "https://grayclub.co.il" + href
            artist = clean_artist(title)
            shows[key] = Show(
                artist=artist,
                date_raw=f"{int(dd):02d}.{int(mm):02d}.{yyyy}",
                venue=VENUE,
                url=url,
                source=self.name,
                date_iso=date_iso,
                title=title if title != artist else None,
            )

        return list(shows.values())
