from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Dict, List, Tuple

from core import storage
from core.models import Show
from scrapers.base import all_scrapers


def collect(scrapers) -> Tuple[List[Show], Dict[str, dict]]:
    """Return (all_shows, per_source_stats). stats[name] = {"found": n} or
    {"error": "..."} so a failed scraper is visible in the scan report."""
    shows: List[Show] = []
    stats: Dict[str, dict] = {}
    for s in scrapers:
        try:
            found = s.fetch()
            print(f"[{s.name}] fetched {len(found)} shows")
            stats[s.name] = {"found": len(found)}
            shows.extend(found)
        except Exception as e:  # one broken site must not stop the others
            print(f"[{s.name}] ERROR: {e}")
            stats[s.name] = {"error": str(e)[:120]}
    return shows, stats


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


def _resolve_merge(key: str, merges: dict) -> str:
    """Follow a manual merge chain (loser_key -> winner_key) to its end."""
    seen = set()
    while key in merges and key not in seen:
        seen.add(key)
        key = merges[key]
    return key


def run_scan(scrapers=None, merges=None) -> Tuple[List[dict], List[str], Dict[str, dict]]:
    """Run all scrapers, persist state, and return (new_show_dicts, new_artists).

    - New shows are keyed to a canonical artist (see `_canonical_artist`) so a
      follower never misses a same-artist show that came in under a longer title.
    - `merges` ({loser_key: winner_key}, set manually in the admin panel) absorb a
      duplicate artist into another: the loser's shows are re-attributed to the
      winner, so the catalogue/shows/alerts all collapse onto one entry.
    - The artist catalogue only ever GROWS (never pruned) — users can follow an
      artist with no current shows and still get alerted when one opens.
    - `shows.json` is pruned to today-and-future; past shows are dropped.
    """
    scrapers = scrapers if scrapers is not None else all_scrapers()
    merges = merges or {}
    current, stats = collect(scrapers)

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
        merged = _resolve_merge(d["artist_key"], merges)         # manual merge first
        if merged != d["artist_key"]:
            d["artist_key"] = merged
            d["artist"] = artists.get(merged, {}).get("display", d["artist"])
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

    # Prune past shows (keep today & future). Artists are NEVER pruned…
    for sid in [s for s, v in known.items() if v.get("date_iso") and v["date_iso"] < today]:
        del known[sid]
    # …except a manually-merged loser, whose shows are now under the winner.
    for loser in merges:
        artists.pop(loser, None)

    storage.save("shows.json", known)
    storage.save("artists.json", artists)

    new_per_source = Counter(d["source"] for d in new_shows)   # annotate stats with new counts
    for src, st in stats.items():
        if "found" in st:
            st["new"] = new_per_source.get(src, 0)
    return new_shows, new_artists, stats
