#!/usr/bin/env python3
"""Entry point, run by GitHub Actions on a schedule.

Each run scrapes every site, detects newly-added shows, and posts them to the
Cloudflare Worker, which pushes an alert to each user following that artist.
The interactive bot (open/search/follow) is handled in real time by the Worker
(a Telegram webhook), not here.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

import scrapers  # noqa: F401  (importing registers all scrapers)
from core import storage
from core.engine import run_scan
from scrapers.base import all_scrapers

try:                                   # DST-aware Israel time (needs tzdata pkg)
    from zoneinfo import ZoneInfo
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:                      # fallback: fixed +3 (off by 1h in winter)
    ISRAEL_TZ = timezone(timedelta(hours=3))

# Active window: every day, 07:00–00:00 Israel time (i.e. skip 01:00–06:00).
ACTIVE_HOURS = set(range(7, 24)) | {0}


def should_scan_now() -> bool:
    if os.environ.get("FORCE_SCAN", "").lower() == "true":   # manual run
        return True
    return datetime.now(ISRAEL_TZ).hour in ACTIVE_HOURS


def notify_worker(new_shows, stats=None, classify=None) -> None:
    """Post the scan result to the Worker EVERY scan: it alerts followers about new
    shows (reads follows from KV) AND sends the developer a scan report (per-source
    counts + Gemini classification), so scans are visible even when nothing's new."""
    url = os.environ.get("WORKER_NOTIFY_URL")
    secret = os.environ.get("NOTIFY_SECRET")
    if not (url and secret):
        print("[notify] WORKER_NOTIFY_URL/NOTIFY_SECRET not set; skipping push")
        return
    ts = datetime.now(ISRAEL_TZ).strftime("%d/%m %H:%M")
    try:
        r = requests.post(url, timeout=30, json={
            "secret": secret,
            "shows": new_shows,            # already canonical dicts from run_scan
            "stats": stats or {},
            "classify": classify or {},
            "ts": ts,
        })
        print(f"[notify] worker {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        print(f"[notify] error: {e}")


# Bump when the classifier (model/prompt) changes — artists are re-classified
# until their cat_v matches, keeping the old category meanwhile (no regression).
# v3: also apply Gemini's name as a safe shortening (see classify_artists).
CLS_VERSION = 3


# Sources that are stand-up-only by definition — their artists are forced to the
# "standup" category, bypassing (and never spending quota on) the AI classifier.
STANDUP_SOURCES = {"comy", "comedybar"}


def force_standup() -> int:
    """Tag every artist sourced from a stand-up-only site (COMY / Comedy Bar)
    directly as standup, so the AI classifier never needs to (and can't mislabel)
    them. Runs every scan, independent of Worker credentials."""
    artists = storage.load("artists.json", {})
    n = 0
    for info in artists.values():
        if STANDUP_SOURCES.intersection(info.get("sources", [])) and not (
                info.get("category") == "standup" and info.get("cat_v") == CLS_VERSION):
            info.update(category="standup", is_artist=True, cat_v=CLS_VERSION)
            n += 1
    if n:
        storage.save("artists.json", artists)
        print(f"[standup] forced standup on {n} artists")
    return n


def classify_artists(cap: int = 60) -> dict:
    """Annotate artists.json with {category, is_artist, cat_v} via the Worker's AI
    classifier (Gemini, cached). Re-does artists whose cat_v is stale; capped per
    run so it stays within the free quota (the bulk fills in over a few runs).
    Returns {"count", "items": [(display, category), ...]} for the scan report."""
    base = os.environ.get("WORKER_NOTIFY_URL")
    secret = os.environ.get("NOTIFY_SECRET")
    if not (base and secret):
        return {"count": 0, "items": []}
    classify_url = base.rsplit("/", 1)[0] + "/classify"
    artists = storage.load("artists.json", {})
    by_disp = {}
    for k, info in artists.items():
        by_disp.setdefault(info["display"], k)
    todo = [d for d, k in by_disp.items() if artists[k].get("cat_v") != CLS_VERSION][:cap]
    if not todo:
        return {"count": 0, "items": []}
    items = []
    for i in range(0, len(todo), 8):
        try:
            res = requests.post(classify_url, timeout=120,
                                json={"secret": secret, "titles": todo[i:i + 8]}).json()
        except requests.RequestException as e:
            print(f"[classify] error: {e}")
            break
        for disp, c in res.items():
            k = by_disp.get(disp)
            if k and isinstance(c, dict) and c.get("category"):
                is_art = bool(c.get("is_artist", True))
                artists[k]["category"] = c["category"]
                artists[k]["is_artist"] = is_art
                artists[k]["cat_v"] = CLS_VERSION
                # Use Gemini's cleaned name ONLY as a safe shortening: it must be a
                # contiguous part of the current name (so "…של תמיר בר" → "תמיר בר",
                # "מייקל הרפז שר אלטון ג'ון" → "מייקל הרפז"), never an expansion/rewrite
                # like "מצבי רוח" → "תזמורת המהפכה - מצבי רוח". Display only; the
                # follow key is unchanged.
                gname = " ".join((c.get("name") or "").split())
                cur = artists[k]["display"]
                if (is_art and len(gname) >= 2 and len(gname) < len(cur)
                        and gname.lower() in " ".join(cur.split()).lower()):
                    artists[k]["display"] = gname
                    disp = gname
                items.append((disp, c["category"], is_art))
    storage.save("artists.json", artists)
    print(f"[classify] annotated {len(items)} artists (cat_v={CLS_VERSION})")
    return {"count": len(items), "items": items}


def fetch_merges() -> dict:
    """Manual artist merges (loser_key -> winner_key), set in the admin panel and
    stored in the Worker's KV overrides. Read so the scan collapses duplicates."""
    base = os.environ.get("WORKER_NOTIFY_URL")
    if not base:
        return {}
    url = base.rsplit("/", 1)[0] + "/api/overrides"
    try:
        ov = requests.get(url, timeout=20).json()
    except (requests.RequestException, ValueError):
        return {}
    return {k: v["merge_into"] for k, v in ov.items()
            if isinstance(v, dict) and v.get("merge_into")}


def main():
    if not should_scan_now():
        print("Outside active hours (07:00–00:00 Israel) — skipping scan.")
        return

    new_shows, new_artists, stats = run_scan(all_scrapers(), merges=fetch_merges())
    print(f"New shows: {len(new_shows)} | New artists: {len(new_artists)}")
    force_standup()
    classify = classify_artists()
    notify_worker(new_shows, stats, classify)   # always — so every scan is reported


if __name__ == "__main__":
    main()
