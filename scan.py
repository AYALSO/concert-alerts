#!/usr/bin/env python3
"""Entry point, run by GitHub Actions on a schedule.

Each run:
  1. applies any /follow /unfollow the user sent via Telegram
  2. scrapes every site and detects newly-added shows
  3. sends an alert for each new show, only to users following that artist
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import scrapers  # noqa: F401  (importing registers all scrapers)
from bot import process_updates, subscribers_following
from core.engine import run_scan
from core.notify import format_show, send_message
from scrapers.base import all_scrapers

ISRAEL_TZ = timezone(timedelta(hours=3))  # good enough for weekday gating


def should_scan_today() -> bool:
    if os.environ.get("SKIP_SATURDAY", "true").lower() != "true":
        return True
    now_il = datetime.now(timezone.utc).astimezone(ISRAEL_TZ)
    return now_il.weekday() != 5  # Mon=0 .. Sat=5 .. Sun=6


def main():
    process_updates()  # always apply selection changes, even on Saturday

    if not should_scan_today():
        print("Saturday in Israel \u2014 skipping scan.")
        return

    new_shows, new_artists = run_scan(all_scrapers())
    print(f"New shows: {len(new_shows)} | New artists: {len(new_artists)}")

    sent = 0
    for show in new_shows:
        for chat_id in subscribers_following(show.artist_key):
            if send_message(chat_id, format_show(show)):
                sent += 1
    print(f"Alerts sent: {sent}")


if __name__ == "__main__":
    main()
