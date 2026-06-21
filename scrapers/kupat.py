"""Kupat Tel Aviv (kupat.co.il) scraper.

WordPress aggregator. The homepage lists shows as cards (`article.item-show`
with `a.item_link` -> `/show/<slug>`); cards with class `external_url` are promos
for other sites and are skipped. Each show's dates + venues live on its own page
(`ul.show-details-list`), in one of two layouts:

  * single date  — labelled rows: "תאריך 17.8", "מיקום היכל מנורה"
  * multi date   — one row per date+place: "18/12 היכל התרבות, ת"א" (a tour can
                   run on many dates in many cities, all under ONE url)

We emit a separate Show per (date, venue) so a follower gets every date/place.
Dates are "DD.M" / "DD/M" with no year, so the next occurrence is inferred and
past dates dropped. Detail pages are fetched fresh each scan (so newly-added
tour dates are caught). Verified live on 2026-06-20 (e.g. כשאמא באה הנה → 10
dates/venues under one url).
"""
from __future__ import annotations

import re
from datetime import date
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup

from core.artist_names import clean_artist
from core.models import Show
from scrapers.base import Scraper, register

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
DATE_TOKEN = r"\d{1,2}(?:-\d{1,2})?[./]\d{1,2}(?:[./]\d{2,4})?"
DATES_RE = re.compile(r"(\d{1,2})(?:-\d{1,2})?[./](\d{1,2})(?:[./](\d{2,4}))?")
LAYOUT_B = re.compile(r"^\s*(" + DATE_TOKEN + r")\s+(.+)$")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _mk_date(dd: str, mm: str, yy, today: date):
    dd, mm = int(dd), int(mm)
    years = [int(yy) + (2000 if int(yy) < 100 else 0)] if yy else [today.year, today.year + 1]
    for y in years:
        try:
            d = date(y, mm, dd)
        except ValueError:
            return None
        if d >= today:
            return d.isoformat(), f"{d.day:02d}.{d.month:02d}.{d.year}"
    return None


def _parse_shows(ul, today: date) -> Tuple[List[Tuple[str, str, str]], str]:
    """Return ([(date_iso, date_raw, venue), ...], time_str) from a details list."""
    pairs, label_dates_text, label_venue, time_s = [], "", "", ""
    for li in ul.find_all("li"):
        t = li.get_text(" ", strip=True)
        if not t:
            continue
        if "תארי" in t and not LAYOUT_B.match(t):          # "תאריך/תאריכים ..."
            label_dates_text = t
            continue
        if t.startswith("מיקום") or t.startswith("אולם"):
            label_venue = re.sub(r"^\s*(מיקום|אולם)\s*:?\s*", "", t).strip()
            continue
        if "שעה" in t or "תחילת מופע" in t:
            m = TIME_RE.search(t)
            time_s = m.group(0) if m else time_s
            continue
        m = LAYOUT_B.match(t)                               # "<date> <venue>" row
        if m:
            md = DATES_RE.search(m.group(1))
            d = _mk_date(md.group(1), md.group(2), md.group(3), today) if md else None
            if d:
                pairs.append((d[0], d[1], m.group(2).strip()))

    if not pairs and label_dates_text:                     # labelled (single/multi)
        dates = [d for md in DATES_RE.findall(label_dates_text)
                 if (d := _mk_date(md[0], md[1], md[2], today))]
        venues = re.split(r"\s*[|,]\s*", label_venue) if label_venue else []
        for i, (iso, raw) in enumerate(dates):
            v = venues[i] if i < len(venues) else (label_venue if len(venues) <= 1 else "")
            pairs.append((iso, raw, v))
    return pairs, time_s


@register
class KupatScraper(Scraper):
    name = "kupat"
    HOME = "https://www.kupat.co.il/"

    def fetch(self) -> List[Show]:
        r = requests.get(self.HOME, timeout=30, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        today = date.today()
        out: List[Show] = []
        seen = set()
        for art in soup.select("article.item-show"):
            if "external_url" in (art.get("class") or []):
                continue                       # promo card for another site
            a = art.select_one("a.item_link[href]")
            if not a:
                continue
            url = a.get("href", "").split("?")[0].split("#")[0]
            name = a.get_text(" ", strip=True)
            if not url or not name or url in seen:
                continue
            seen.add(url)
            out.extend(self._fetch_detail(url, name, today))  # fresh each scan
        return out

    def _fetch_detail(self, url: str, name: str, today: date) -> List[Show]:
        try:
            r = requests.get(url, timeout=30, headers=HEADERS)
            r.raise_for_status()
        except requests.RequestException:
            return []
        ul = BeautifulSoup(r.text, "lxml").select_one("ul.show-details-list")
        if not ul:
            return []
        pairs, time_s = _parse_shows(ul, today)
        if not pairs:
            return []

        artist = clean_artist(name)
        title = name if name != artist else None
        single = len(pairs) == 1
        shows = []
        for iso, raw, venue in pairs:
            if time_s and single:
                raw = f"{raw} {time_s}"
            shows.append(Show(
                artist=artist,
                date_raw=raw,
                venue=venue or "קופת תל אביב",
                # ALWAYS key per date (even single-date), so a show_id is stable per
                # (show, date): opening a new date adds exactly ONE new show_id and
                # never re-keys the existing dates (which would re-alert them).
                url=f"{url}#{iso}",
                source="kupat",
                date_iso=iso,
                title=title,
            ))
        return shows
