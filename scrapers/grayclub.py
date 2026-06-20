"""Gray Club (grayclub.co.il) scraper.

The homepage server-renders every upcoming show inside per-city carousels, so a
single GET is enough -- the "load more" button only affects one list, and the
carousels already enumerate the full catalogue. We dedupe by the stable event
URL (/event/<a>/<b>/) and drop past dates.

NOTE: selectors validated against the rendered page structure; run once live to
confirm class names didn't change, then lock in.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List

import requests
from bs4 import BeautifulSoup

from core.models import Show
from scrapers.base import Scraper, register

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
EVENT_HREF_RE = re.compile(r"/event/\d+/\d+/?")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; concert-alerts/1.0)"}


@register
class GrayClubScraper(Scraper):
    name = "grayclub"
    HOME = "https://grayclub.co.il/"

    def fetch(self) -> List[Show]:
        r = requests.get(self.HOME, timeout=30, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        today = date.today()
        shows: dict[str, Show] = {}

        # Each show card has a title heading; the event link and date live in
        # the same card. Drive off the headings and resolve link + date nearby.
        for heading in soup.find_all(["h3", "h2"]):
            title = heading.get_text(" ", strip=True)
            if not title:
                continue

            # nearest event link (search the card around this heading)
            link = None
            for a in heading.find_all_previous("a", href=EVENT_HREF_RE, limit=1):
                link = a
                break
            if link is None:
                for a in heading.find_all_next("a", href=EVENT_HREF_RE, limit=1):
                    link = a
                    break
            if link is None:
                continue
            href = link.get("href", "")
            url = href if href.startswith("http") else "https://grayclub.co.il" + href

            # date: first DD.MM.YYYY appearing just after the title
            date_raw = date_iso = None
            for nxt in heading.find_all_next(string=DATE_RE, limit=1):
                m = DATE_RE.search(str(nxt))
                if m:
                    dd, mm, yyyy = m.groups()
                    date_raw = f"{int(dd):02d}.{int(mm):02d}.{yyyy}"
                    date_iso = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
                break
            if not date_iso:
                continue

            try:
                if datetime.fromisoformat(date_iso).date() < today:
                    continue
            except ValueError:
                pass

            key = re.search(EVENT_HREF_RE, href).group(0)
            if key in shows:
                continue
            shows[key] = Show(
                artist=title,
                date_raw=date_raw,
                venue="\u05de\u05d5\u05e2\u05d3\u05d5\u05df \u05d2\u05e8\u05d9\u05d9",
                url=url,
                source=self.name,
                date_iso=date_iso,
            )

        return list(shows.values())
