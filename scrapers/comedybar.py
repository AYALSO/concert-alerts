"""Comedy Bar (tickets.comedybar.net / SmartTicket) scraper — stand-up, ARTISTS ONLY.

The Comedy Bar marketing site (comedybar.co.il) sells through SmartTicket at
tickets.comedybar.net, which renders a clean `.show_cube` per performance:
title (`.h2`), date (`.show_date` e.g. "ביום חמישי, 25 ביוני 2026"), time
(`.show_time`), venue (`.theater_name`) and a `?id=<n>` order link (a distinct
id per date). We emit ONE Show per date, keyed `url#<iso>` so each date is a
distinct `show_id` — `show_id` strips the `?query`, and the path repeats across
an artist's dates, so the iso fragment is what separates them (and lets each new
date raise its own alert).

ARTISTS ONLY: the listing is mostly recurring club nights — open-mic, marathons,
"ערב סטנד אפ עם כוכבי…", "קומדי בר <city>", "במה פתוחה…". Those are skipped by
keyword (`_SKIP`). We keep only single-comedian shows ("<name> במופע סטנד אפ")
and force their category to stand-up (see `scan.force_standup` / `make_artist_page`).
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
# Recurring club nights / non-artist items — skipped (named artists only).
_SKIP = ("מרתון", "במה פתוחה", "open mic", "מיקרופון", "כוכבי", "אמני הקומדי",
         "עושים צחוק", "נותנים בראש", "סיבה למסיבה", "שבוע טוב", "שובר", "קומדי בר",
         "ערב סטנד")
_DATE_RE = re.compile(r"(\d{1,2})\s+ב?([א-ת]+)\s+(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
# strip the "... במופע/בהופעת סטנד אפ ..." marketing suffix to leave the name
_SUFFIX_RE = re.compile(r"\s*ב(?:מופע|הופע\w*)\s+סטנד.*$")


@register
class ComedyBarScraper(Scraper):
    name = "comedybar"

    def fetch(self) -> List[Show]:
        r = creq.get(BASE, impersonate="chrome", timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        today_iso = date.today().isoformat()
        shows: dict[str, Show] = {}

        for cube in soup.select(".show_cube"):
            h = cube.select_one(".details .h2") or cube.select_one(".h3")
            a = cube.find("a", href=True)
            sd = cube.select_one(".show_date")
            if not h or not a or not sd:
                continue
            title = h.get_text(" ", strip=True)
            if any(k.lower() in title.lower() for k in _SKIP):
                continue                                   # recurring club night, not an artist
            name = clean_artist(_SUFFIX_RE.sub("", title).strip())
            if not name or re.search(r"[؀-ۿ]", name):   # skip Arabic-script duplicates
                continue

            m = _DATE_RE.search(sd.get_text(" ", strip=True))
            if not m or m.group(2) not in _MONTHS:
                continue
            dd, mm, yyyy = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
            iso = f"{yyyy}-{mm:02d}-{dd:02d}"
            if iso < today_iso:
                continue

            st = cube.select_one(".show_time")
            tm = _TIME_RE.search(st.get_text(" ", strip=True)) if st else None
            path = a["href"].split("?")[0].split("#")[0].lstrip("/")
            url = f"{BASE}{path}#{iso}"
            if url in shows:
                continue
            ven = cube.select_one(".theater_name")
            shows[url] = Show(
                artist=name,
                date_raw=f"{dd:02d}.{mm:02d}.{yyyy}" + (f" {tm.group(1)}" if tm else ""),
                venue=ven.get_text(" ", strip=True) if ven else "",
                url=url,
                source=self.name,
                date_iso=iso,
                title=title if title != name else None,
            )
        return list(shows.values())
