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
from core.artist_names import looks_non_artist
from core.engine import run_scan
from core.models import normalize_artist
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


def notify_worker(new_shows, new_artists=None, auto_merges=None) -> None:
    """Post the scan result to the Worker: it alerts followers about new shows and
    accumulates the day's new shows + new artist names (and scan count) that feed the
    single ~22:00 developer summary. `auto_merges` ({loser_key: winner_key}) are
    duplicate artists detected this run; the Worker records them in the KV overrides
    so follows keep resolving and the next scan collapses the catalogue entries."""
    url = os.environ.get("WORKER_NOTIFY_URL")
    secret = os.environ.get("NOTIFY_SECRET")
    if not (url and secret):
        print("[notify] WORKER_NOTIFY_URL/NOTIFY_SECRET not set; skipping push")
        return
    try:
        r = requests.post(url, timeout=30, json={
            "secret": secret,
            "shows": new_shows,            # already canonical dicts from run_scan
            "new_artists": new_artists or [],   # list of new artist display names
            "auto_merges": auto_merges or {},
        })
        print(f"[notify] worker {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        print(f"[notify] error: {e}")


# Bump when the classifier (model/prompt) changes — artists are re-classified
# until their cat_v matches, keeping the old category meanwhile (no regression).
# v3: also apply Gemini's name as a safe shortening (see classify_artists).
# v4: stricter is_artist prompt (lectures/committees/parties/tributes → false).
#     Must match the Worker's KV cache prefix ("cls4:").
CLS_VERSION = 4


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


def mark_non_artists() -> int:
    """Deterministically flag catalogue entries that are self-evidently not an
    artist (lectures, conferences, committee/benefit events — see
    `core.artist_names.looks_non_artist`). Runs before the AI classifier, so no
    quota is ever spent on them and they can't slip through on a quota hiccup.
    Stand-up sources are skipped (their acts are curated by force_standup); the
    admin panel override can always rescue a false positive."""
    artists = storage.load("artists.json", {})
    n = 0
    for info in artists.values():
        if STANDUP_SOURCES.intersection(info.get("sources", [])):
            continue
        if looks_non_artist(info["display"]) and not (
                info.get("is_artist") is False and info.get("cat_v") == CLS_VERSION):
            info.update(is_artist=False, cat_v=CLS_VERSION)
            n += 1
    if n:
        storage.save("artists.json", artists)
        print(f"[junk] flagged {n} non-artist entries")
    return n


def _classify_context() -> dict:
    """artist_key -> one sample 'event title @ venue' line, giving the classifier
    the real show title (not just the extracted artist name) to judge from.
    Deterministic pick (lexicographic) so the Worker's per-input cache holds."""
    ctx = {}
    for s in storage.load("shows.json", {}).values():
        k, t = s.get("artist_key"), (s.get("title") or "").strip()
        if not k or not t or t == s.get("artist"):
            continue
        cand = f"{t} @ {s.get('venue', '')}".strip()
        if k not in ctx or cand < ctx[k]:
            ctx[k] = cand
    return ctx


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
    todo = [d for d, k in by_disp.items() if artists[k].get("cat_v") != CLS_VERSION]
    # Brand-new artists (no cat_v at all) first: they must be classified in THIS
    # run so the daily summary can filter out fresh non-artists. A backlog of
    # stale re-classifications (after a CLS_VERSION bump) must never starve them.
    todo.sort(key=lambda d: "cat_v" in artists[by_disp[d]])
    todo = todo[:cap]
    if not todo:
        return {"count": 0, "items": []}
    # Each request line is "<artist> ::: <sample title @ venue>" when a richer
    # show title exists — the AI judges the act with its real event as context.
    ctx = _classify_context()
    req_of = {}                              # request line -> display name
    for d in todo:
        extra = ctx.get(by_disp[d])
        req_of[f"{d} ::: {extra}" if extra else d] = d
    items = []
    reqs = list(req_of)
    for i in range(0, len(reqs), 8):
        try:
            res = requests.post(classify_url, timeout=120,
                                json={"secret": secret, "titles": reqs[i:i + 8]}).json()
        except (requests.RequestException, ValueError) as e:
            print(f"[classify] error: {e}")
            break
        if res.pop("__v", None) != CLS_VERSION:
            # The Worker still runs the previous prompt/cache version — stamping
            # cat_v now would freeze OLD-prompt results as if they were new.
            print(f"[classify] worker not on v{CLS_VERSION} yet; skipping")
            break
        for req, c in res.items():
            disp = req_of.get(req)
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


def propose_merges(existing: dict) -> dict:
    """Duplicate artists = two catalogue keys showing the SAME display name (a
    marketing-title key whose display was shortened next to the plain key, e.g.
    "מייקל הרפז שר אלטון ג'ון" shortened to "מייקל הרפז" alongside the real
    "מייקל הרפז"). Winner = the key that IS the normalized display (else the
    shortest); the rest merge into it. Skips pairs already merged in overrides.
    Returns {loser_key: winner_key} to send with /notify."""
    artists = storage.load("artists.json", {})
    groups: dict[str, list] = {}
    for k, info in artists.items():
        if info.get("is_artist") is False:
            continue
        groups.setdefault(normalize_artist(info["display"]), []).append(k)
    merges = {}
    for disp, keys in groups.items():
        if len(keys) < 2 or not disp:
            continue
        winner = disp if disp in keys else min(keys, key=len)
        for k in keys:
            if k != winner and existing.get(k) != winner:
                merges[k] = winner
    if merges:
        print(f"[merge] proposing {len(merges)} duplicate merges: {merges}")
    return merges


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

    merges = fetch_merges()
    new_shows, new_keys, _stats = run_scan(all_scrapers(), merges=merges)
    print(f"New shows: {len(new_shows)} | New artists: {len(new_keys)}")
    mark_non_artists()                          # deterministic junk, no AI quota
    force_standup()
    classify_artists()                          # classify + safe name-shortening

    # Report/alert only real artists: drop anything flagged is_artist=false
    # (deterministically or by Gemini in this very run) from both the new-artist
    # list and the new-show alerts, and use the (possibly shortened) display.
    artists = storage.load("artists.json", {})
    real = lambda key: artists.get(key, {}).get("is_artist") is not False
    new_artists = [artists[k]["display"] for k in new_keys if k in artists and real(k)]
    new_shows = [s for s in new_shows if real(s["artist_key"])]
    dropped = len(new_keys) - len(new_artists)
    if dropped:
        print(f"[junk] {dropped} new non-artist entries kept out of the summary")
    notify_worker(new_shows, new_artists, propose_merges(existing=merges))


if __name__ == "__main__":
    main()
