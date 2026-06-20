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

ISRAEL_TZ = timezone(timedelta(hours=3))  # good enough for weekday gating


def should_scan_today() -> bool:
    if os.environ.get("SKIP_SATURDAY", "true").lower() != "true":
        return True
    now_il = datetime.now(timezone.utc).astimezone(ISRAEL_TZ)
    return now_il.weekday() != 5  # Mon=0 .. Sat=5 .. Sun=6


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
    if not should_scan_today():
        print("Saturday in Israel — skipping scan.")
        return

    new_shows, new_artists = run_scan(all_scrapers())
    print(f"New shows: {len(new_shows)} | New artists: {len(new_artists)}")
    notify_worker(new_shows)


if __name__ == "__main__":
    main()
