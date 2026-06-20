"""Eventim Israel scraper — covers zappa-club + every Eventim IL venue.

zappa-club.co.il is a white-label storefront of Eventim Israel; both block plain
HTTP clients at the TLS layer (connection reset / tarpit). We bypass that with
curl_cffi (Chrome TLS impersonation) and read Eventim's public exploration API
directly, which returns clean, structured events — no HTML scraping.

API (1-indexed pages, ~20/page):
  GET .../search/api/exploration/v1/products
      ?webId=web__eventim-co-il&language=he&categories=הופעות חיות&page=N

Each product gives a clean attraction (artist) name, the event title, venue,
city, an ISO start datetime, and a direct event link. `categories=הופעות חיות`
is the *category name* (the URL's "51" does NOT work). Verified ~269 live shows
against a live fetch on 2026-06-20.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List

from curl_cffi import requests as cffi

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

API = ("https://public-api.eventim.com/websearch/search/api/exploration/v1/"
       "products")
BASE_PARAMS = {
    "webId": "web__eventim-co-il",
    "language": "he",
    "categories": "הופעות חיות",      # live shows (the category NAME, not "51")
}
MAX_PAGES = 40                          # safety cap; loop really stops when dry


@register
class EventimScraper(Scraper):
    name = "eventim"

    def fetch(self) -> List[Show]:
        today = date.today()
        shows: dict[str, Show] = {}     # productId -> Show
        page = 1
        while page <= MAX_PAGES:
            r = cffi.get(API, params={**BASE_PARAMS, "page": str(page)},
                         impersonate="chrome", timeout=30,
                         headers={"Accept": "application/json"})
            r.raise_for_status()
            products = r.json().get("products") or []
            if not products:
                break
            added = 0
            for p in products:
                pid = str(p.get("productId") or "").strip()
                if not pid or pid in shows:
                    continue
                show = self._parse(p, today)
                if show:
                    shows[pid] = show
                    added += 1
            if added == 0:              # page had only already-seen items -> done
                break
            page += 1
        return list(shows.values())

    @staticmethod
    def _parse(p: dict, today: date) -> Show | None:
        att = p.get("attractions") or []
        # attractions[0].name is the clean artist; fall back to the (messy)
        # event title, which then needs the same cleanup the other sites get.
        if att and att[0].get("name"):
            artist = att[0]["name"].strip()
        else:
            artist = clean_artist(p.get("name") or "")
        le = (p.get("typeAttributes") or {}).get("liveEntertainment") or {}
        start = le.get("startDate") or ""
        if not artist or not start:
            return None
        try:
            dt = datetime.fromisoformat(start)
        except ValueError:
            return None
        if dt.date() < today:
            return None

        loc = le.get("location") or {}
        venue = (loc.get("name") or loc.get("city") or "Eventim").strip()
        link = p.get("link") or "".join([
            (p.get("url") or {}).get("domain", ""),
            (p.get("url") or {}).get("path", ""),
        ])
        title = (p.get("name") or "").strip()
        return Show(
            artist=artist,
            date_raw=dt.strftime("%d.%m.%Y %H:%M"),
            venue=venue,
            url=link,
            source="eventim",
            date_iso=dt.date().isoformat(),
            title=title if title and title != artist else None,
        )
