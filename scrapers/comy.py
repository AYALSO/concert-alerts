"""COMY (comy.co.il) scraper — stand-up only.

The homepage server-renders each act as an `.event` card: an `a.event-inner`
link to /event/<slug>/, an `<h3>` performer/show name, an `<h4>` ("סטנדאפ") and
an `<h5>` date range "DD/MM/YYYY-DD/MM/YYYY". These are touring stand-up shows
under ONE URL with many dates, so we record one Show per event (one alert per
new tour, not per date) keyed on the event URL. date_iso = the tour's END date,
so an in-progress tour isn't pruned until it is fully over.

Every COMY act is stand-up by definition; `make_artist_page` / `classify_artists`
force the category to "standup" for any artist sourced from here, regardless of
the AI classifier.

Behind a CDN — fetched with curl_cffi browser impersonation, like eventim.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


@register
class ComyScraper(Scraper):
    name = "comy"
    HOME = "https://www.comy.co.il/"

    def fetch(self) -> List[Show]:
        r = creq.get(self.HOME, impersonate="chrome", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        today = date.today()
        shows: dict[str, Show] = {}            # event url -> Show

        for card in soup.select(".event"):
            link = card.select_one("a.event-inner")
            h3 = card.find("h3")
            if not link or not h3:
                continue
            title = h3.get_text(" ", strip=True)
            href = (link.get("href") or "").split("?")[0].split("#")[0]
            if not title or not href.startswith("http") or href in shows:
                continue

            h5 = card.find("h5")
            raw = h5.get_text(" ", strip=True) if h5 else ""
            dates = DATE_RE.findall(raw)
            date_iso = None
            if dates:
                dd, mm, yyyy = dates[-1]            # tour END date
                date_iso = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
                try:
                    if datetime.fromisoformat(date_iso).date() < today:
                        continue                     # whole tour already over
                except ValueError:
                    date_iso = None

            artist = clean_artist(title)
            shows[href] = Show(
                artist=artist,
                date_raw=raw,
                venue="",
                url=href,
                source=self.name,
                date_iso=date_iso,
                title=title if title != artist else None,
            )

        return list(shows.values())
