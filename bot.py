"""Telegram interaction, processed once per scan run.

No always-on server: every scheduled run calls process_updates(), which reads
any commands the user sent since last time and updates the followed-artists
list. Confirmations arrive on that run (trigger a manual run for instant).

Commands:
  /start | /help          register this chat and show help
  /artists                numbered list of discovered artists + follow state
  /follow <number|name>   follow (by the number from /artists, or by name)
  /unfollow <number|name>
  /following              show who you currently follow
"""
from __future__ import annotations

import os

import requests

from core import storage
from core.models import normalize_artist

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _api(method, **payload):
    token = _token()
    if not token:
        return None
    try:
        r = requests.post(
            TELEGRAM_API.format(token=token, method=method), json=payload, timeout=20
        )
        return r.json()
    except requests.RequestException:
        return None


def _send(chat_id, text):
    _api("sendMessage", chat_id=chat_id, text=text,
         parse_mode="HTML", disable_web_page_preview=True)


def _numbered_artists():
    artists = storage.load("artists.json", {})
    return sorted(artists.items(), key=lambda kv: kv[1]["display"].lower())


def _resolve(arg, items):
    arg = (arg or "").strip()
    if not arg:
        return None
    if arg.isdigit():
        idx = int(arg) - 1
        return items[idx][0] if 0 <= idx < len(items) else None
    target = normalize_artist(arg)
    for key, info in items:
        if key == target or normalize_artist(info["display"]) == target:
            return key
    for key, _info in items:           # fall back to partial match
        if target and target in key:
            return key
    return None


def _handle_command(chat_id, text, subs):
    sub = subs["subscribers"].setdefault(chat_id, {"follows": []})
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()

    if cmd in ("/start", "/help"):
        _send(chat_id,
              "\u05d1\u05e8\u05d5\u05da \u05d4\u05d1\u05d0! \U0001F3B6\n"
              "\u05d0\u05ea\u05e8\u05d9\u05e2 \u05dc\u05da \u05e2\u05dc \u05d4\u05d5\u05e4\u05e2\u05d5\u05ea \u05d7\u05d3\u05e9\u05d5\u05ea \u05e9\u05dc \u05d4\u05d0\u05de\u05e0\u05d9\u05dd \u05e9\u05ea\u05d1\u05d7\u05e8.\n\n"
              "/artists \u2013 \u05e8\u05e9\u05d9\u05de\u05ea \u05d4\u05d0\u05de\u05e0\u05d9\u05dd \u05e9\u05d4\u05ea\u05d2\u05dc\u05d5\n"
              "/follow <\u05de\u05e1\u05e4\u05e8 \u05d0\u05d5 \u05e9\u05dd> \u2013 \u05e2\u05e7\u05d5\u05d1\n"
              "/unfollow <\u05de\u05e1\u05e4\u05e8 \u05d0\u05d5 \u05e9\u05dd> \u2013 \u05d4\u05e4\u05e1\u05e7 \u05de\u05e2\u05e7\u05d1\n"
              "/following \u2013 \u05de\u05d9 \u05e9\u05d0\u05ea\u05d4 \u05e2\u05d5\u05e7\u05d1 \u05d0\u05d7\u05e8\u05d9\u05d5")
        return

    items = _numbered_artists()

    if cmd == "/artists":
        if not items:
            _send(chat_id, "\u05e2\u05d5\u05d3 \u05dc\u05d0 \u05e0\u05de\u05e6\u05d0\u05d5 \u05d0\u05de\u05e0\u05d9\u05dd. \u05d4\u05e1\u05e8\u05d9\u05e7\u05d4 \u05d4\u05e8\u05d0\u05e9\u05d5\u05e0\u05d4 \u05ea\u05de\u05dc\u05d0 \u05d0\u05ea \u05d4\u05e8\u05e9\u05d9\u05de\u05d4.")
            return
        lines = ["\U0001F3A4 <b>\u05d0\u05de\u05e0\u05d9\u05dd \u05e9\u05d4\u05ea\u05d2\u05dc\u05d5:</b>"]
        for i, (key, info) in enumerate(items, 1):
            mark = "\u2705" if key in sub["follows"] else "\u2B1C\uFE0F"
            lines.append(f"{i}. {mark} {info['display']}")
        lines.append("\n\u05dc\u05de\u05e2\u05e7\u05d1: /follow <\u05de\u05e1\u05e4\u05e8>")
        _send(chat_id, "\n".join(lines))
        return

    if cmd in ("/follow", "/unfollow"):
        key = _resolve(arg, items)
        if not key:
            _send(chat_id, f"\u05dc\u05d0 \u05de\u05e6\u05d0\u05ea\u05d9: {arg}\n\u05e0\u05e1\u05d4 /artists")
            return
        display = dict(items).get(key, {}).get("display", arg)
        if cmd == "/follow":
            if key not in sub["follows"]:
                sub["follows"].append(key)
            _send(chat_id, f"\u2705 \u05e2\u05d5\u05e7\u05d1 \u05d0\u05d7\u05e8\u05d9 {display}")
        else:
            if key in sub["follows"]:
                sub["follows"].remove(key)
            _send(chat_id, f"\u2B1C\uFE0F \u05d4\u05d5\u05e4\u05e1\u05e7 \u05de\u05e2\u05e7\u05d1 \u05d0\u05d7\u05e8\u05d9 {display}")
        return

    if cmd == "/following":
        if not sub["follows"]:
            _send(chat_id, "\u05d0\u05d9\u05e0\u05da \u05e2\u05d5\u05e7\u05d1 \u05d0\u05d7\u05e8\u05d9 \u05d0\u05e3 \u05d0\u05de\u05df \u05e2\u05d3\u05d9\u05d9\u05df. /artists")
            return
        follow_set = set(sub["follows"])
        names = [info["display"] for key, info in items if key in follow_set]
        _send(chat_id, "\u05d0\u05ea\u05d4 \u05e2\u05d5\u05e7\u05d1 \u05d0\u05d7\u05e8\u05d9:\n" + "\n".join(f"\u2022 {n}" for n in names))
        return

    _send(chat_id, "\u05dc\u05d0 \u05d4\u05d1\u05e0\u05ea\u05d9. \u05e0\u05e1\u05d4 /help")


def process_updates():
    if not _token():
        print("[bot] no token; skipping updates")
        return
    state = storage.load("state.json", {})
    offset = state.get("tg_offset", 0)
    subs = storage.load("favorites.json", {"subscribers": {}})
    subs.setdefault("subscribers", {})

    resp = _api("getUpdates", offset=offset, timeout=0)
    if not resp or not resp.get("ok"):
        return

    for upd in resp.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        if text:
            _handle_command(chat_id, text, subs)

    state["tg_offset"] = offset
    storage.save("state.json", state)
    storage.save("favorites.json", subs)


def subscribers_following(artist_key):
    subs = storage.load("favorites.json", {"subscribers": {}})
    return [
        chat_id
        for chat_id, sub in subs.get("subscribers", {}).items()
        if artist_key in sub.get("follows", [])
    ]
