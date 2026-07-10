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
    them. Runs every scan, independent of Worker credentials.

    Only artists WITHOUT a current-version classification are stamped — a manual
    (admin/cleanup) or AI fix at cat_v == CLS_VERSION must stick (COMY also sells
    Beit-Lessin THEATER plays; blindly re-forcing standup used to revert their
    correction every hour). For new entries, a deterministic venue hint applies:
    an act whose every live show is at a "תיאטרון" venue is a play, not standup."""
    artists = storage.load("artists.json", {})
    venues: dict[str, list] = {}
    for s in storage.load("shows.json", {}).values():
        venues.setdefault(s.get("artist_key"), []).append(s.get("venue") or "")
    n = 0
    for key, info in artists.items():
        if not STANDUP_SOURCES.intersection(info.get("sources", [])):
            continue
        if info.get("cat_v") == CLS_VERSION:
            continue                                   # already classified/fixed
        vs = venues.get(key, [])
        cat = "theater" if vs and all("תיאטרון" in v for v in vs) else "standup"
        info.update(category=cat, is_artist=True, cat_v=CLS_VERSION)
        n += 1
    if n:
        storage.save("artists.json", artists)
        print(f"[standup] categorized {n} stand-up-source artists")
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


# Israel hours in which the stale-classification backlog (cat_v < CLS_VERSION
# after a version bump) is allowed to spend Gemini quota. Brand-new artists are
# classified EVERY run — retrying the same ~60 backlog names every hour used to
# exhaust the free grounded-search quota by mid-morning, leaving afternoon junk
# unclassified (and therefore publicly visible) until the next day.
BACKLOG_HOURS = {7, 13, 20}


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
    fresh = [d for d, k in by_disp.items() if "cat_v" not in artists[k]]
    stale = [d for d, k in by_disp.items()
             if "cat_v" in artists[k] and artists[k]["cat_v"] != CLS_VERSION]
    todo = fresh
    if datetime.now(ISRAEL_TZ).hour in BACKLOG_HOURS:
        todo = fresh + stale                 # backlog only in designated runs
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
        version_ok = res.pop("__v", None) == CLS_VERSION
        if not version_ok:
            # The Worker still runs the previous prompt/cache version — stamping
            # cat_v now would freeze OLD-prompt results as if they were new. BUT
            # junk-hiding is version-insensitive (junk under v3 is junk under v4),
            # so is_artist=false results are still applied (without cat_v) — a
            # deploy gap must not let fresh junk go public for hours.
            print(f"[classify] worker not on v{CLS_VERSION} yet; applying junk flags only")
        for req, c in res.items():
            disp = req_of.get(req)
            k = by_disp.get(disp)
            if not (k and isinstance(c, dict) and c.get("category")):
                continue
            is_art = c.get("is_artist") is True          # anything else = not an artist
            fallback = bool(c.get("f"))                  # Workers-AI fallback result:
            if not version_ok or fallback:               # apply, but keep cat_v stale so
                if not is_art:                           # Gemini re-does it properly later
                    artists[k]["is_artist"] = False
                    items.append((disp, artists[k].get("category", c["category"]), False))
                continue
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
    """Duplicate artists to auto-merge. Two detectors:

    1. Same display name under two keys (a marketing-title key whose display was
       shortened next to the plain key, e.g. "מייקל הרפז שר אלטון ג'ון" shortened
       to "מייקל הרפז" alongside the real "מייקל הרפז"). Winner = the key that IS
       the normalized display (else the shortest).
    2. Leading-word-prefix pairs: a longer key that starts with an established
       (≥2-word, so no "תמר" traps) shorter key AND has no live show of its own —
       the guest-title residue class ("שלמה ארצי ואורחים" next to "שלמה ארצי").

    Skips pairs already merged in overrides. Returns {loser_key: winner_key}."""
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
    live_keys = {s.get("artist_key") for s in storage.load("shows.json", {}).values()}
    for k in artists:
        if k in merges or artists[k].get("is_artist") is False or k in live_keys:
            continue
        kw = k.split()
        best = None
        for other in artists:
            ow = other.split()
            if (len(ow) >= 2 and len(ow) < len(kw) and kw[:len(ow)] == ow
                    and artists[other].get("is_artist") is not False):
                if best is None or len(ow) < len(best.split()):
                    best = other
        if best and existing.get(k) != best:
            merges[k] = best
    if merges:
        print(f"[merge] proposing {len(merges)} duplicate merges: {merges}")
    return merges


def curated_merges() -> dict:
    """Hand-curated duplicate merges committed to the repo
    (data/curated_merges.json, {loser_key: winner_key}). They ride the same
    auto-merge channel as propose_merges: /notify records them in the KV
    overrides, follows keep resolving, and the next scan collapses the entries."""
    m = storage.load("curated_merges.json", {})
    return {k: v for k, v in m.items() if isinstance(v, str) and k != v}


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

    curated = curated_merges()
    fetched = fetch_merges()
    merges = {**curated, **fetched}
    new_shows, new_keys, variant_merges, _stats = run_scan(all_scrapers(), merges=merges)
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
    # All auto-merge sources ride one channel: curated (repo) + orphaned variants
    # (engine) + display/prefix collisions. The Worker records them in the KV
    # overrides so follows resolve and the next scan collapses the entries.
    auto = {**curated, **variant_merges, **propose_merges(existing=merges)}
    auto = {k: v for k, v in auto.items() if fetched.get(k) != v}   # already in KV
    notify_worker(new_shows, new_artists, auto)


if __name__ == "__main__":
    main()
