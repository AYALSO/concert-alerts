"""COMY (comy.co.il) scraper — stand-up only, one Show per DATE.

Homepage `.event` cards give each act's name (`<h3>`) and event URL
(`a.event-inner`). Each event is a touring stand-up show with many dates, so we
fetch its detail page and emit ONE Show per performance date — comedians open
new dates one at a time, and each new date must raise its own alert.

Per-date rows on the detail page: `.single-event-details` →
`.single-date-details .date` (DD.MM, no year) + `.single-light` (day + time) +
`.single-place-string` (venue). The year is inferred as the nearest future
occurrence. The url gets `#<iso>` so each date is a distinct `show_id`;
same-date rows dedupe, so no duplicate alerts.

Every COMY artist is stand-up (forced in `scan.force_comy_standup` and
`make_artist_page`). curl_cffi browser impersonation (behind a CDN).
"""
from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

DM_RE = re.compile(r"(\d{1,2})[.\/](\d{1,2})")


def _infer_iso(dd: int, mm: int, today: date) -> Optional[str]:
    """DD.MM with no year -> the nearest occurrence that is today-or-future."""
    for y in (today.year, today.year + 1):
        try:
            d = date(y, mm, dd)
        except ValueError:
            return None
        if d >= today:
            return d.isoformat()
    return None


@register
class ComyScraper(Scraper):
    name = "comy"
    HOME = "https://www.comy.co.il/"

    def fetch(self) -> List[Show]:
        r = creq.get(self.HOME, impersonate="chrome", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        today = date.today()

        events: dict[str, str] = {}              # event url -> name
        for card in soup.select(".event"):
            link = card.select_one("a.event-inner")
            h3 = card.find("h3")
            if not link or not h3:
                continue
            href = (link.get("href") or "").split("?")[0].split("#")[0]
            name = h3.get_text(" ", strip=True)
            if href.startswith("http") and name:
                events.setdefault(href, name)

        shows: dict[str, Show] = {}              # url#iso -> Show (dedupes same date)
        for url, name in events.items():
            artist = clean_artist(name)
            try:
                d = creq.get(url, impersonate="chrome", timeout=30)
                rows = BeautifulSoup(d.text, "html.parser").select(".single-event-details")
            except Exception as e:  # one bad event page must not stop the rest
                print(f"[comy] {url} ERROR: {e}")
                continue
            for row in rows:
                wrap = row.find_parent("a")             # <a single-page-event [not-clickable]>
                sold_out = bool(wrap and wrap.select_one(".event-sold-out"))
                dt = row.select_one(".single-date-details .date")
                m = DM_RE.search(dt.get_text(" ", strip=True)) if dt else None
                if not m:
                    continue
                iso = _infer_iso(int(m.group(1)), int(m.group(2)), today)
                if not iso:
                    continue
                key = f"{url}#{iso}"
                if key in shows:
                    continue
                light = row.select_one(".single-light")
                place = row.select_one(".single-place-string")
                when = light.get_text(" ", strip=True) if light else ""
                shows[key] = Show(
                    artist=artist,
                    date_raw=f"{iso[8:10]}.{iso[5:7]} {when}".strip(),
                    venue=place.get_text(" ", strip=True) if place else "",
                    url=key,
                    source=self.name,
                    date_iso=iso,
                    title=name if name != artist else None,
                    sold_out=sold_out,
                )
        return list(shows.values())
