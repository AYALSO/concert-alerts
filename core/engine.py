from __future__ import annotations

from typing import List, Tuple

from core import storage
from core.models import Show
from scrapers.base import all_scrapers


def collect(scrapers) -> List[Show]:
    shows: List[Show] = []
    for s in scrapers:
        try:
            found = s.fetch()
            print(f"[{s.name}] fetched {len(found)} shows")
            shows.extend(found)
        except Exception as e:  # one broken site must not stop the others
            print(f"[{s.name}] ERROR: {e}")
    return shows


def run_scan(scrapers=None) -> Tuple[List[Show], List[str]]:
    """Run all scrapers, persist state, and return (new_shows, new_artist_names)."""
    scrapers = scrapers if scrapers is not None else all_scrapers()
    current = collect(scrapers)

    # Dedupe within this run by show_id.
    by_id = {sh.show_id: sh for sh in current}

    # New shows = those not present in the last persisted scan.
    known = storage.load("shows.json", {})          # show_id -> show dict
    new_shows = [sh for sid, sh in by_id.items() if sid not in known]

    for sid, sh in by_id.items():
        known[sid] = sh.to_dict()
    storage.save("shows.json", known)

    # Maintain the artist catalogue (deduped by artist_key, grows over time).
    artists = storage.load("artists.json", {})      # artist_key -> info
    new_artists: List[str] = []
    for sh in by_id.values():
        k = sh.artist_key
        if k not in artists:
            artists[k] = {
                "display": sh.artist,
                "sources": [sh.source],
                "first_seen": sh.scraped_at,
            }
            new_artists.append(sh.artist)
        elif sh.source not in artists[k]["sources"]:
            artists[k]["sources"].append(sh.source)
    storage.save("artists.json", artists)

    return new_shows, new_artists
