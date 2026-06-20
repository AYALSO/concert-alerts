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


def notify_worker(new_shows) -> None:
    """Post new shows to the Worker, which alerts followers (reads follows from KV)."""
    url = os.environ.get("WORKER_NOTIFY_URL")
    secret = os.environ.get("NOTIFY_SECRET")
    if not (url and secret):
        print("[notify] WORKER_NOTIFY_URL/NOTIFY_SECRET not set; skipping push")
        return
    if not new_shows:
        print("[notify] no new shows")
        return
    try:
        r = requests.post(url, timeout=30, json={
            "secret": secret,
            "shows": [s.to_dict() for s in new_shows],
        })
        print(f"[notify] worker {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        print(f"[notify] error: {e}")


def main():
    if not should_scan_now():
        print("Outside active hours (07:00–00:00 Israel) — skipping scan.")
        return

    new_shows, new_artists = run_scan(all_scrapers())
    print(f"New shows: {len(new_shows)} | New artists: {len(new_artists)}")
    notify_worker(new_shows)


if __name__ == "__main__":
    main()
