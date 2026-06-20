"""Example/mock scraper.

Proves the whole pipeline (scan -> dedupe -> detect new -> notify) without any
network access. Once real scrapers are in, turn this off by setting the repo
variable EXAMPLE_SCRAPER=off (or delete this file and its import).
"""
from __future__ import annotations

import os
from typing import List

from core.models import Show
from scrapers.base import Scraper, register


@register
class ExampleScraper(Scraper):
    name = "example"

    def fetch(self) -> List[Show]:
        if os.environ.get("EXAMPLE_SCRAPER", "on").lower() == "off":
            return []
        return [
            Show(
                artist="Radiohead",
                date_raw="12.09.2026",
                venue="Park HaYarkon, Tel Aviv",
                url="https://example.com/shows/radiohead",
                source=self.name,
                date_iso="2026-09-12",
            ),
            Show(
                artist="\u05e2\u05d5\u05de\u05e8 \u05d0\u05d3\u05dd",
                date_raw="03.08.2026",
                venue="\u05e7\u05d9\u05e1\u05e8\u05d9\u05d4",
                url="https://example.com/shows/omer-adam",
                source=self.name,
                date_iso="2026-08-03",
            ),
        ]
