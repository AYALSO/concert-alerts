from __future__ import annotations

from datetime import date
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


def _canonical_artist(key: str, display: str, artists: dict) -> Tuple[str, str]:
    """Attach a show to an artist the user would already be following.

    If an established artist's name is a leading-word prefix of this show's
    artist, the show belongs to that artist: a new "ריטה lets dance" /
    "רוקוויל במחווה ל" / "חתול בשק מיאסוווו" attaches to the existing
    "ריטה" / "רוקוויל" / "חתול בשק", so their followers get the alert.

    Picks the SHORTEST established prefix (the recognizable name people follow).
    Word-boundary match only, so "אסף מור יוסף" never collapses into a stray
    "מור". Deterministic — no AI — so a followed key never silently fails to
    match a same-artist show. Returns (canonical_key, canonical_display)."""
    sw = key.split()
    best = None
    for k in artists:
        kw = k.split()
        if kw and len(kw) < len(sw) and sw[:len(kw)] == kw:
            if best is None or len(kw) < len(best.split()):
                best = k
    return (best, artists[best]["display"]) if best else (key, display)


def run_scan(scrapers=None) -> Tuple[List[dict], List[str]]:
    """Run all scrapers, persist state, and return (new_show_dicts, new_artists).

    - New shows are keyed to a canonical artist (see `_canonical_artist`) so a
      follower never misses a same-artist show that came in under a longer title.
    - The artist catalogue only ever GROWS (never pruned) — users can follow an
      artist with no current shows and still get alerted when one opens.
    - `shows.json` is pruned to today-and-future; past shows are dropped.
    """
    scrapers = scrapers if scrapers is not None else all_scrapers()
    current = collect(scrapers)

    by_id = {sh.show_id: sh for sh in current}       # dedupe this run by show_id
    known = storage.load("shows.json", {})           # show_id -> show dict
    artists = storage.load("artists.json", {})       # artist_key -> info
    today = date.today().isoformat()

    # Process shorter artist names first, so a base ("ריטה") is registered before
    # its longer tour-variant ("ריטה lets dance") and the variant attaches to it.
    ordered = sorted(by_id.values(), key=lambda sh: len(sh.artist_key.split()))

    new_shows: List[dict] = []
    new_artists: List[str] = []
    for sh in ordered:
        d = sh.to_dict()
        key, display = _canonical_artist(d["artist_key"], d["artist"], artists)
        d["artist_key"], d["artist"] = key, display          # canonical attribution
        is_new = sh.show_id not in known
        known[sh.show_id] = d
        if is_new and (not d.get("date_iso") or d["date_iso"] >= today):
            new_shows.append(d)                              # never alert a past show
        if key not in artists:
            artists[key] = {"display": display, "sources": [d["source"]],
                            "first_seen": d["scraped_at"]}
            new_artists.append(display)
        elif d["source"] not in artists[key]["sources"]:
            artists[key]["sources"].append(d["source"])

    # Prune past shows (keep today & future). Artists are NEVER pruned.
    for sid in [s for s, v in known.items() if v.get("date_iso") and v["date_iso"] < today]:
        del known[sid]

    storage.save("shows.json", known)
    storage.save("artists.json", artists)
    return new_shows, new_artists
