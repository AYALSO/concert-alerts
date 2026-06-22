"""Comedy Bar (tickets.comedybar.net / SmartTicket) scraper — stand-up, ARTISTS ONLY.

The Comedy Bar marketing site (comedybar.co.il) sells through SmartTicket at
tickets.comedybar.net. The homepage is only a rolling window of the soonest ~100
date-cards (so a far-future date appears late and looks "new" the day it enters
the window). We use the homepage ONLY to discover which shows are single
comedians, then read each show's DETAIL page, whose table lists ALL of its dates
(date + venue + `?id` per row) — so we always have the complete schedule and a
date raises its alert once, when it really opens.

We emit one Show per date, keyed `url#<iso>` (the path repeats across an artist's
dates and `show_id` strips the `?query`, so the iso fragment separates them).

ARTISTS ONLY: recurring club nights (open-mic, marathons, "ערב סטנד אפ עם
כוכבי…", "קומדי בר <city>", "במה פתוחה…") are skipped by keyword (`_SKIP`); kept
titles are single-comedian "<name> במופע סטנד אפ" → suffix stripped → clean_artist.
Every kept artist is forced to stand-up (scan.force_standup / make_artist_page).
"""
from __future__ import annotations

import re
from datetime import date
from typing import List

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

BASE = "https://tickets.comedybar.net/"
_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
           "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}
_SKIP = ("מרתון", "במה פתוחה", "open mic", "מיקרופון", "כוכבי", "אמני הקומדי",
         "עושים צחוק", "נותנים בראש", "סיבה למסיבה", "שבוע טוב", "שובר", "קומדי בר",
         "ערב סטנד")
_DATE_RE = re.compile(r"(\d{1,2})\s+ב?([א-ת]+)\s+(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
_SUFFIX_RE = re.compile(r"\s*ב(?:מופע|הופע\w*)\s+סטנד.*$")


@register
class ComedyBarScraper(Scraper):
    name = "comedybar"

    def fetch(self) -> List[Show]:
        r = creq.get(BASE, impersonate="chrome", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Homepage = discovery only: collect each single-comedian show (path -> name).
        shows_by_path: dict[str, tuple[str, str]] = {}
        for cube in soup.select(".show_cube"):
            h = cube.select_one(".details .h2") or cube.select_one(".h3")
            a = cube.find("a", href=True)
            if not h or not a:
                continue
            title = h.get_text(" ", strip=True)
            if any(k.lower() in title.lower() for k in _SKIP):
                continue                                   # recurring club night
            name = clean_artist(_SUFFIX_RE.sub("", title).strip())
            if not name or re.search(r"[؀-ۿ]", name):     # skip Arabic-script duplicates
                continue
            path = a["href"].split("?")[0].split("#")[0].lstrip("/")
            if path:
                shows_by_path.setdefault(path, (name, title))

        out: List[Show] = []
        for path, (name, title) in shows_by_path.items():
            try:
                out.extend(self._fetch_dates(path, name, title))   # ALL dates for this show
            except Exception as e:  # one bad detail page must not stop the rest
                print(f"[comedybar] {path} ERROR: {e}")
        return out

    def _fetch_dates(self, path: str, name: str, title: str) -> List[Show]:
        today_iso = date.today().isoformat()
        r = creq.get(BASE + path, impersonate="chrome", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # The page has a short "featured" table AND a full `.list-table`; parse every
        # row from all tables and dedupe by url#iso, so we get the COMPLETE schedule.
        rows = soup.select("table tbody tr")

        shows: dict[str, Show] = {}                        # url#iso -> Show (dedupe per date)
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            dtext = tds[0].get_text(" ", strip=True)        # "ביום שישי, 3 ביולי 2026 : בשעה 22:00"
            m = _DATE_RE.search(dtext)
            if not m or m.group(2) not in _MONTHS:
                continue
            dd, mm, yyyy = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
            iso = f"{yyyy}-{mm:02d}-{dd:02d}"
            if iso < today_iso:
                continue
            url = f"{BASE}{path}#{iso}"
            if url in shows:
                continue
            tm = _TIME_RE.search(dtext)
            shows[url] = Show(
                artist=name,
                date_raw=f"{dd:02d}.{mm:02d}.{yyyy}" + (f" {tm.group(1)}" if tm else ""),
                venue=tds[1].get_text(" ", strip=True),
                url=url,
                source=self.name,
                date_iso=iso,
                title=title if title != name else None,
            )
        return list(shows.values())
