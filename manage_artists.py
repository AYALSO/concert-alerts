#!/usr/bin/env python3
"""Developer tool: review and hand-edit the artist catalogue (names + categories).

    python manage_artists.py export      # -> data/artists_review.csv (open in Excel)
    #   edit the display / category / is_artist columns, save
    python manage_artists.py apply       # write your edits to data/overrides.json

`data/overrides.json` is keyed by the stable `artist_key` and WINS over the AI /
source classification at page-build, and survives every scan:
    { "<artist_key>": {"name": "...", "category": "music|standup|theater", "is_artist": true|false} }
- `name`     renames the displayed artist (the follow key/hash is unchanged, so
             existing follows keep working).
- `category` / `is_artist` pin the classification (set is_artist=false to hide).

`apply` only records the fields you actually changed (a diff against what the
catalogue would show on its own); reverting a value back to the default removes
that override. Re-running export/apply is idempotent.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter

from core import storage

CSV_PATH = "data/artists_review.csv"
STANDUP_SRC = {"comy", "comedybar"}
CATS = {"music", "standup", "theater"}
FIELDS = ["artist_key", "display", "category", "is_artist", "sources", "shows"]


def _base(info: dict):
    """What the catalogue shows for this artist WITHOUT any manual override."""
    name = info["display"]
    if info.get("category"):
        cat = info["category"]
    elif STANDUP_SRC.intersection(info.get("sources", [])):
        cat = "standup"
    else:
        cat = "music"
    if "is_artist" in info:
        art = info["is_artist"]
    elif STANDUP_SRC.intersection(info.get("sources", [])):
        art = True
    else:
        art = True
    return name, cat, art


def _effective(key: str, info: dict, ov: dict):
    """What the catalogue shows WITH the current override applied."""
    name, cat, art = _base(info)
    o = ov.get(key, {})
    if o.get("name"):
        name = o["name"]
    if o.get("category"):
        cat = o["category"]
    if "is_artist" in o:
        art = o["is_artist"]
    return name, cat, art


def export() -> None:
    artists = storage.load("artists.json", {})
    ov = storage.load("overrides.json", {})
    cnt = Counter(s.get("artist_key") for s in storage.load("shows.json", {}).values())
    rows = []
    for key, info in artists.items():
        name, cat, art = _effective(key, info, ov)
        rows.append([key, name, cat, "yes" if art else "no",
                     "|".join(info.get("sources", [])), cnt.get(key, 0)])
    rows.sort(key=lambda r: (-r[5], r[1]))            # most-upcoming-shows first
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    print(f"wrote {CSV_PATH} ({len(rows)} artists). "
          f"Edit display/category/is_artist, then: python manage_artists.py apply")


def apply() -> None:
    artists = storage.load("artists.json", {})
    ov = storage.load("overrides.json", {})
    changed = 0
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("artist_key") or "").strip()
            info = artists.get(key)
            if not info:
                continue
            base_name, base_cat, base_art = _base(info)
            o = dict(ov.get(key, {}))
            before = dict(o)

            new_name = (row.get("display") or "").strip()
            if new_name and new_name != base_name:
                o["name"] = new_name
            else:
                o.pop("name", None)

            new_cat = (row.get("category") or "").strip().lower()
            if new_cat in CATS and new_cat != base_cat:
                o["category"] = new_cat
            elif new_cat == base_cat:
                o.pop("category", None)

            new_art = (row.get("is_artist") or "").strip().lower() in ("yes", "true", "1")
            if new_art != base_art:
                o["is_artist"] = new_art
            else:
                o.pop("is_artist", None)

            if o != before:
                changed += 1
            if o:
                ov[key] = o
            else:
                ov.pop(key, None)
    storage.save("overrides.json", ov)
    print(f"applied {changed} change(s); overrides.json now has {len(ov)} entr(ies). "
          f"Run `python make_artist_page.py` (or wait for the next scan) to rebuild the list.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        export()
    elif cmd == "apply":
        apply()
    else:
        print(__doc__)
        sys.exit(1)
