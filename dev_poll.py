#!/usr/bin/env python3
"""Local dev helper: run the Telegram bot in a fast polling loop.

Production responds to bot commands only during the scheduled GitHub Actions
scan (not instant). This script loads `.env` for TELEGRAM_BOT_TOKEN and calls
`bot.process_updates()` every couple of seconds so the bot replies right away
while developing/testing. It does NOT scan sites or send alerts — run `scan.py`
for that. Ctrl+C to stop.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def _load_env() -> None:
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def main() -> None:
    _load_env()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("No TELEGRAM_BOT_TOKEN — set it in .env first.")

    from bot import process_updates

    print("Bot polling every 2s — Ctrl+C to stop.")
    while True:
        try:
            process_updates()
        except Exception as e:  # keep the loop alive on transient errors
            print("poll error:", e)
        time.sleep(2)


if __name__ == "__main__":
    main()
