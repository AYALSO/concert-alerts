"""Barby (barby.co.il) scraper.

barby.co.il is a React SPA: the homepage HTML is an empty shell and every show
is loaded from a JSON API. The endpoint below returns the whole upcoming-shows
list in a single GET -- no auth, no params.

The site sits behind Cloudflare, which serves an "Attention Required" challenge
page instead of JSON to requests that don't look like a browser. Sending a full
set of browser headers (realistic User-Agent + Origin/Referer) gets through
reliably from a residential network; if Actions logs show an HTML/Cloudflare
body instead of JSON, the datacenter IP is being challenged -- revisit headers.

Verified: 78 upcoming shows against a live fetch on 2026-06-20.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List

import requests

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

# Cloudflare in front of the API rejects non-browser requests, so look like one.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.barby.co.il",
    "Referer": "https://www.barby.co.il/",
}

VENUE = "בארבי"


@register
class BarbyScraper(Scraper):
    name = "barby"
    API = "https://barby.co.il/api/shows/find"

    def fetch(self) -> List[Show]:
        r = requests.get(self.API, timeout=30, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        rows = (data.get("returnShow") or {}).get("show") or []

        today = date.today()
        shows: dict[str, Show] = {}     # showId -> Show (the API id is stable)
        for row in rows:
            show_id = str(row.get("showId") or "").strip()
            name = (row.get("showName") or "").strip()
            raw_date = (row.get("showDate") or "").strip()      # "DD/MM/YYYY"
            if not show_id or not name or not raw_date:
                continue

            m = DATE_RE.search(raw_date)
            if not m:
                continue
            dd, mm, yyyy = m.groups()
            date_iso = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
            try:
                if datetime.fromisoformat(date_iso).date() < today:
                    continue
            except ValueError:
                continue

            time = (row.get("showTime") or "").strip()           # "21:00"
            date_raw = f"{int(dd):02d}.{int(mm):02d}.{yyyy}"
            if time:
                date_raw = f"{date_raw} {time}"

            if show_id in shows:
                continue
            artist = clean_artist(name)
            shows[show_id] = Show(
                artist=artist,
                date_raw=date_raw,
                venue=VENUE,
                url=f"https://www.barby.co.il/show/{show_id}",
                source=self.name,
                date_iso=date_iso,
                title=name if name != artist else None,
            )

        return list(shows.values())
