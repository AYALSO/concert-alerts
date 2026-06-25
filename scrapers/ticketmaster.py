"""Ticketmaster Israel (ticketmaster.co.il) scraper.

Angular SPA, but the public sitemap enumerates every event and each event PAGE is
server-rendered (SSR), so no API reverse-engineering needed:
  - `GET /wbtxapi/api/v1/siteMap/event` → `<loc>…/event/<code>/ALL/iw` for every event.
  - Each event page has an `<h1>` artist name and one `.performance-listing` per date:
    `.date-box` (`.day` + Hebrew `.month`), `.time` ("יום חמישי • 20:15"),
    `.performance-listing-venue`, and a status chip that reads "אזל…" when sold out.

We emit one Show per date, url `…/event/<code>/ALL/iw#<iso>` (the event page lists all
its dates → one shared link). Year is inferred (nearest future). Sold-out dates are
flagged (`sold_out`) like the other sources.

Transport: the sitemap host trips curl_cffi's HTTP/2 ("unsupported HTTP/1 subversion"),
so the sitemap is fetched with plain `requests` (HTTP/1.1); event pages use curl_cffi
Chrome impersonation (plain requests is challenged).
"""
from __future__ import annotations

import re
from datetime import date
from typing import List

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

BASE = "https://www.ticketmaster.co.il"
SITEMAP = BASE + "/wbtxapi/api/v1/siteMap/event"
_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
           "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/xml,text/xml,*/*", "Accept-Language": "he-IL,he;q=0.9"}


def _iso(day: int, mm: int, today: date):
    for y in (today.year, today.year + 1):          # no year in the date-box → nearest future
        try:
            d = date(y, mm, day)
        except ValueError:
            return None
        if d >= today:
            return d.isoformat()
    return None


@register
class TicketmasterScraper(Scraper):
    name = "ticketmaster"

    def fetch(self) -> List[Show]:
        sm = requests.get(SITEMAP, timeout=30, headers=_HEADERS).text
        codes: list[str] = []
        for loc in re.findall(r"<loc>(.*?)</loc>", sm):
            m = re.search(r"/event/([A-Za-z0-9]+)/ALL/iw\b", loc)
            if m and m.group(1) not in codes and "BDIKA" not in m.group(1):  # skip the test event
                codes.append(m.group(1))

        today = date.today()
        out: List[Show] = []
        for code in codes:
            try:
                out.extend(self._event(code, today))
            except Exception as e:  # one bad event page must not stop the rest
                print(f"[ticketmaster] {code} ERROR: {e}")
        return out

    def _event(self, code: str, today: date) -> List[Show]:
        url = f"{BASE}/event/{code}/ALL/iw"
        r = creq.get(url, impersonate="chrome", timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        h = soup.select_one("h1") or soup.select_one(".btx-title")
        name = h.get_text(" ", strip=True) if h else ""
        if not name:
            return []
        artist = clean_artist(name)

        shows: dict[str, Show] = {}                  # url#iso -> Show
        for pl in soup.select(".performance-listing"):
            day = pl.select_one(".date-box .day")
            mon = pl.select_one(".date-box .month")
            if not day or not mon:
                continue
            try:
                dd = int(day.get_text(strip=True))
            except ValueError:
                continue
            mm = _MONTHS.get(mon.get_text(strip=True))
            if not mm:
                continue
            iso = _iso(dd, mm, today)
            if not iso:
                continue
            key = f"{url}#{iso}"
            if key in shows:
                continue
            ven = pl.select_one(".performance-listing-venue")
            tnode = pl.select_one(".time")
            tm = _TIME_RE.search(tnode.get_text(" ", strip=True)) if tnode else None
            shows[key] = Show(
                artist=artist,
                date_raw=f"{dd:02d}.{mm:02d}.{iso[:4]}" + (f" {tm.group(1)}" if tm else ""),
                venue=ven.get_text(" ", strip=True) if ven else "",
                url=key,
                source=self.name,
                date_iso=iso,
                title=name if name != artist else None,
                sold_out="אזל" in pl.get_text(" "),
            )
        return list(shows.values())
