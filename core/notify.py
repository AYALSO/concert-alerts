from __future__ import annotations

import os

import requests

from core.models import Show

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def send_message(chat_id, text: str, disable_preview: bool = False) -> bool:
    token = _token()
    if not token:
        # Dry run (local testing without a token): print instead of send.
        print(f"[notify] (no token) -> {chat_id}:\n{text}\n")
        return False
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
            timeout=20,
        )
        if not r.ok:
            print(f"[notify] telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"[notify] error: {e}")
        return False


def format_show(show: Show) -> str:
    lines = [f"\U0001F3B5 <b>{show.artist}</b> \u2014 \u05d4\u05d5\u05e4\u05e2\u05d4 \u05d7\u05d3\u05e9\u05d4!"]
    if show.title and show.title != show.artist:
        lines.append(show.title)                       # full show title, for context
    lines += [
        f"\U0001F4C5 {show.date_raw}",
        f"\U0001F4CD {show.venue}",
        f"\U0001F517 {show.url}",
    ]
    return "\n".join(lines)
