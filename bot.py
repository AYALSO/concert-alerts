"""Telegram interaction, processed once per scan run.

No always-on server: every scheduled run calls process_updates(), which reads
any commands/button taps the user sent since last time and updates the
followed-artists list. Confirmations arrive on that run (trigger a manual run
for instant).

Commands:
  /start | /help          register this chat and show help
  /artists                tap-to-follow buttons (paginated) for discovered artists
  /follow <number|name>   follow by name (buttons are easier; kept for power users)
  /unfollow <number|name>
  /following              show who you currently follow
  /upcoming               upcoming shows for the artists you follow

Button taps arrive as callback_query updates and toggle follow in place,
re-rendering the same message's keyboard.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import requests

from core import storage
from core.models import normalize_artist

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

ISRAEL_TZ = timezone(timedelta(hours=3))
PAGE_SIZE = 8            # artist buttons per page
TG_LIMIT = 3500         # safe chunk size (Telegram hard limit is 4096)


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


def _send(chat_id, text, reply_markup=None):
    payload = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                   disable_web_page_preview=True)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _api("sendMessage", **payload)


def _answer_callback(cq_id, text=None):
    payload = {"callback_query_id": cq_id}
    if text:
        payload["text"] = text
    _api("answerCallbackQuery", **payload)


def _chunks(text):
    """Split on line boundaries so no message exceeds Telegram's limit."""
    out, buf = [], ""
    for ln in text.split("\n"):
        if buf and len(buf) + len(ln) + 1 > TG_LIMIT:
            out.append(buf)
            buf = ln
        else:
            buf = ln if not buf else f"{buf}\n{ln}"
    if buf:
        out.append(buf)
    return out


def _send_long(chat_id, text):
    for chunk in _chunks(text):
        _send(chat_id, chunk)


# ---------------------------------------------------------------------------
# Artist list + inline keyboard
# ---------------------------------------------------------------------------
def _numbered_artists():
    artists = storage.load("artists.json", {})
    return sorted(artists.items(), key=lambda kv: kv[1]["display"].lower())


def _artist_hash(key):
    """Short stable id for callback_data (artist keys can be long Hebrew)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _build_keyboard(items, follows, page):
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = []
    for key, info in items[start:start + PAGE_SIZE]:
        mark = "✅" if key in follows else "⬜"
        rows.append([{
            "text": f"{mark} {info['display']}",
            "callback_data": f"t:{page}:{_artist_hash(key)}",
        }])
    nav = []
    if page > 0:
        nav.append({"text": "« הקודם", "callback_data": f"p:{page-1}"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "x"})
    if page < pages - 1:
        nav.append({"text": "הבא »", "callback_data": f"p:{page+1}"})
    rows.append(nav)
    return {"inline_keyboard": rows}


def _edit_keyboard(chat_id, message_id, items, follows, page):
    _api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
         reply_markup=_build_keyboard(items, follows, page))


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


SEARCH_LIMIT = 24       # max artist buttons shown for one search


def _search(query, items):
    """Artists whose key/display contains the query; prefix matches first."""
    q = normalize_artist(query)
    if not q:
        return []
    starts, contains = [], []
    for key, info in items:
        disp = normalize_artist(info["display"])
        if key.startswith(q) or disp.startswith(q):
            starts.append((key, info))
        elif q in key or q in disp:
            contains.append((key, info))
    return starts + contains


def _build_search_kb(matches, follows):
    rows = []
    for key, info in matches[:SEARCH_LIMIT]:
        mark = "✅" if key in follows else "⬜"
        rows.append([{"text": f"{mark} {info['display']}",
                      "callback_data": f"s:{_artist_hash(key)}"}])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Update handlers
# ---------------------------------------------------------------------------
def _handle_callback(cq, subs):
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id"))
    message_id = msg.get("message_id")
    cq_id = cq.get("id")
    sub = subs["subscribers"].setdefault(chat_id, {"follows": []})
    items = _numbered_artists()

    if data.startswith("p:"):
        page = int(data[2:] or 0)
        _edit_keyboard(chat_id, message_id, items, set(sub["follows"]), page)
        _answer_callback(cq_id)
        return

    if data.startswith("t:"):
        _, page_s, h = data.split(":", 2)
        page = int(page_s)
        key = next((k for k, _ in items if _artist_hash(k) == h), None)
        if not key:
            _answer_callback(cq_id, "הרשימה התעדכנה, נסה /artists")
            return
        display = dict(items)[key]["display"]
        if key in sub["follows"]:
            sub["follows"].remove(key)
            note = f"⬜ הוסר: {display}"
        else:
            sub["follows"].append(key)
            note = f"✅ עוקב אחרי: {display}"
        _edit_keyboard(chat_id, message_id, items, set(sub["follows"]), page)
        _answer_callback(cq_id, note)
        return

    if data.startswith("s:"):                 # toggle from a search-result list
        key = next((k for k, _ in items if _artist_hash(k) == data[2:]), None)
        if not key:
            _answer_callback(cq_id, "הרשימה התעדכנה, חפש שוב")
            return
        display = dict(items)[key]["display"]
        if key in sub["follows"]:
            sub["follows"].remove(key)
            note = f"⬜ הוסר: {display}"
        else:
            sub["follows"].append(key)
            note = f"✅ עוקב אחרי: {display}"
        matches = _search(sub.get("last_query", ""), items)
        if matches:                            # re-render same search, updated marks
            _api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                 reply_markup=_build_search_kb(matches, set(sub["follows"])))
        _answer_callback(cq_id, note)
        return

    _answer_callback(cq_id)   # "x" page indicator / unknown


def _handle_command(chat_id, text, subs):
    sub = subs["subscribers"].setdefault(chat_id, {"follows": []})
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()

    if cmd == "/start":
        if arg.startswith("f_"):            # follow deep-link from the web page
            items = _numbered_artists()
            key = next((k for k, _ in items if _artist_hash(k) == arg[2:]), None)
            if key:
                if key not in sub["follows"]:
                    sub["follows"].append(key)
                _send(chat_id, f"✅ עוקב אחרי {dict(items)[key]['display']}")
            else:
                _send(chat_id, "לא מצאתי את האמן — נסה שוב מהאתר.")
            return
        # Greet + explain search (the catalogue is too big to page through) and
        # still offer the browsable list.
        _send(chat_id,
              "ברוך הבא! \U0001F3B6\n"
              "אתריע לך על כל הופעה חדשה של האמנים שתבחר.\n\n"
              "\U0001F50E פשוט הקלד שם של אמן (או חלק ממנו) כדי לחפש — "
              "לחיצה על תוצאה = מעקב/ביטול.")
        items = _numbered_artists()
        if items:
            _send(chat_id,
                  f"\U0001F3A4 {len(items)} אמנים בקטלוג. הקלד שם לחיפוש, או דפדף:",
                  reply_markup=_build_keyboard(items, set(sub["follows"]), 0))
        else:
            _send(chat_id, "עוד לא נמצאו אמנים. הסריקה הראשונה תמלא את הרשימה.")
        return

    if cmd == "/help":
        _send(chat_id,
              "ברוך הבא! \U0001F3B6\n"
              "אתריע לך על הופעות חדשות של האמנים שתבחר.\n\n"
              "\U0001F50E הקלד שם של אמן כדי לחפש ולעקוב (הדרך המהירה)\n"
              "/artists – דפדוף בכל האמנים\n"
              "/following – מי שאתה עוקב אחריו\n"
              "/upcoming – הופעות קרובות של האמנים שלך\n"
              "/follow <שם> · /unfollow <שם> – מעקב לפי שם")
        return

    items = _numbered_artists()

    if cmd == "/artists":
        if not items:
            _send(chat_id, "עוד לא נמצאו אמנים. הסריקה הראשונה תמלא את הרשימה.")
            return
        _send(chat_id,
              "\U0001F3A4 <b>אמנים שהתגלו</b> — לחץ כדי לעקוב/לבטל:",
              reply_markup=_build_keyboard(items, set(sub["follows"]), 0))
        return

    if cmd in ("/follow", "/unfollow"):
        key = _resolve(arg, items)
        if not key:
            _send(chat_id, f"לא מצאתי: {arg}\nנסה /artists")
            return
        display = dict(items).get(key, {}).get("display", arg)
        if cmd == "/follow":
            if key not in sub["follows"]:
                sub["follows"].append(key)
            _send(chat_id, f"✅ עוקב אחרי {display}")
        else:
            if key in sub["follows"]:
                sub["follows"].remove(key)
            _send(chat_id, f"⬜ הופסק מעקב אחרי {display}")
        return

    if cmd == "/following":
        if not sub["follows"]:
            _send(chat_id, "אינך עוקב אחרי אף אמן עדיין. /artists")
            return
        follow_set = set(sub["follows"])
        names = [info["display"] for key, info in items if key in follow_set]
        _send_long(chat_id, "אתה עוקב אחרי:\n" +
                   "\n".join(f"• {n}" for n in names))
        return

    if cmd == "/upcoming":
        follow_set = set(sub["follows"])
        if not follow_set:
            _send(chat_id, "אינך עוקב אחרי אף אמן עדיין. /artists")
            return
        today = datetime.now(ISRAEL_TZ).strftime("%Y-%m-%d")
        shows = storage.load("shows.json", {})
        mine = [
            s for s in shows.values()
            if s.get("artist_key") in follow_set
            and (not s.get("date_iso") or s["date_iso"] >= today)
        ]
        if not mine:
            _send(chat_id, "אין כרגע הופעות קרובות לאמנים שאתה עוקב אחריהם.")
            return
        mine.sort(key=lambda s: s.get("date_iso") or s.get("date_raw") or "")
        lines = ["\U0001F3B6 <b>הופעות קרובות:</b>"]
        for s in mine:
            title = s.get("title")
            extra = f"\n  {title}" if title and title != s["artist"] else ""
            lines.append(
                f"• <b>{s['artist']}</b>{extra}\n  {s['date_raw']} · {s['venue']}\n  {s['url']}"
            )
        _send_long(chat_id, "\n".join(lines))
        return

    # Any other plain text is treated as an artist search.
    if text and not text.startswith("/"):
        sub["last_query"] = text
        matches = _search(text, items)
        if not matches:
            _send(chat_id, f"\U0001F50E לא נמצאו אמנים עבור \"{text}\". נסה שם אחר.")
            return
        more = "" if len(matches) <= SEARCH_LIMIT else f" (מציג {SEARCH_LIMIT} ראשונים)"
        _send(chat_id,
              f"\U0001F50E תוצאות עבור \"{text}\"{more} — לחץ כדי לעקוב/לבטל:",
              reply_markup=_build_search_kb(matches, set(sub["follows"])))
        return

    _send(chat_id, "הקלד שם של אמן כדי לחפש, או /help")


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
        if "callback_query" in upd:
            _handle_callback(upd["callback_query"], subs)
            continue
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
