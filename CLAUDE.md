# CLAUDE.md — Concert Alerts

Persistent project context. Claude Code reads this at the start of every session.
This file is the handoff from the original design conversation: it records what the
project is, the decisions already made, what's done, and what's left.

## Goal
Alert the user on Telegram whenever artists they follow announce **new** shows on
Israeli ticketing/venue sites. Runs free, automatically, in the cloud.

## How it works (pipeline)
Every hour (GitHub Actions cron, **skips Saturday** by Israel time):
1. `bot.process_updates()` — apply any `/follow` `/unfollow` the user sent on Telegram.
2. `engine.run_scan()` — run every registered scraper, dedupe, detect shows not seen
   in the previous scan, and grow the artist catalogue (deduped by normalized name).
3. For each new show, send a Telegram alert **only** to users following that artist.
4. The workflow commits the updated `data/*.json` back to the repo (this is the memory).

## Locked decisions (do not relitigate without asking the user)
- **Notifications: Telegram only.** Works identically on iPhone + Android, free push.
  (Push-app and email were intentionally dropped to stay simple/free.)
- **Hosting: GitHub Actions only**, free. Hourly cron + manual "Run workflow" button.
- **One user-facing setting:** follow/unfollow artists via the Telegram bot. The artist
  list grows automatically as new artists are discovered; no other config.
- **Artist grouping:** each artist is stored once (by `artist_key` = normalized name);
  every show is a separate record under it. So an artist with 2 dates appears once in
  the list, and the user gets alerts for every new date. This is already implemented.

## Data model (`core/models.py`)
`Show`: artist, date_raw, venue, url, source, date_iso (optional), scraped_at.
- `artist_key` = lowercased, punctuation-stripped name → used for grouping/dedupe.
- `show_id` = stable hash of (artist_key, date, venue, source) → used to detect "new".
Storage (`data/`): `shows.json` (known shows), `artists.json` (catalogue),
`favorites.json` (subscribers + their follows), `state.json` (telegram offset).

## Scrapers (`scrapers/`)
Each site = one module subclassing `Scraper` (see `scrapers/base.py`), returning
`list[Show]`, registered via `@register` and imported in `scrapers/__init__.py`.

| Source | Status | Notes |
|---|---|---|
| `example` | demo, **OFF by default** | Fake data to prove the flow. Now off (workflow default `EXAMPLE_SCRAPER` flipped to `off` since a real scraper exists). Turn on with repo variable `EXAMPLE_SCRAPER=on`. |
| `barby` | ✅ **built & verified** | `scrapers/barby.py`. Not HTML — hits the JSON API `GET https://barby.co.il/api/shows/find` → `returnShow.show[]` (`showId`, `showName`, `showDate` DD/MM/YYYY, `showTime`). Behind Cloudflare: must send full browser headers (UA + **Origin/Referer** are the key). URL = `https://www.barby.co.il/show/<showId>`. Artist name is cleaned via `core/artist_names.clean_artist`; full title kept in `Show.title`. Verified **78 shows / 52 artists** live on 2026-06-20. |
| `eventim` | ✅ **built & verified** | `scrapers/eventim.py`. Covers zappa-club **and all Eventim Israel venues** (זאפה, היכל התרבות, אמפי קיסריה, …). zappa-club is an Eventim white-label; both block plain HTTP at the TLS layer → use `curl_cffi` `impersonate="chrome"`. Reads the Eventim API (recipe below); `attractions[0].name` is already a clean artist. Verified **269 shows / 155 artists** live on 2026-06-20. |
| `grayclub` | ✅ **built & verified** | `scrapers/grayclub.py`. Card-based: iterates `div.article-list` (each card = `<h3>` title + DD.MM.YYYY date + one `/event/<a>/<b>/` link). Drives off `<h3>` (the title), NOT the city-section `<h2>` (תלאביב/יהוד/מודיעין) — that was the old bug. Dedupe by event path; `clean_artist` applied. Verified **75 shows / 69 artists** live on 2026-06-20. |
| `kupot_ta` + others | later | Kupot Tel Aviv etc., after the first three are solid. |

### Eventim API recipe (as built in `scrapers/eventim.py`)
Fetch with `curl_cffi` (`requests.get(url, impersonate="chrome")`) — plain `requests` is reset at the edge.
- **Individual events**: `GET https://public-api.eventim.com/websearch/search/api/exploration/v1/products`
  params: `webId=web__eventim-co-il`, `language=he`, `categories=הופעות חיות` (the category **name**, NOT the URL's "51"), `page=N` (1-indexed, ~20/page). `totalPages` is unreliable — paginate until a page adds no new `productId`.
- Each product: `attractions[0].name` (clean **artist**), `name` (event title → `Show.title`), `link` (event URL), `productId` (stable id), `typeAttributes.liveEntertainment.location.{name,city}` (**venue**), `typeAttributes.liveEntertainment.startDate` (ISO datetime).
- `web__eventim-co-il` = **all Eventim Israel** live shows (we chose this over zappa-only for max coverage). To narrow to just zappa-club, add `retail_partner=ZPE` — confirm it actually narrows before relying on it.

### Artist-name unification (`core/artist_names.py`)
`clean_artist(raw)` extracts the core artist from a marketing title (e.g. "טיפקס- מופע צהרים" → "טיפקס", "ג'ירפות - חוגגים…" → "ג'ירפות"). Splits on dashes/`|`/`:` adjacent to space or Hebrew (keeps "T-Puse"), cuts at description keywords (מופע/אורח/חוגג/השקת/לייב…), drops venue/filler (בבארבי…), and preserves intra-word geresh (ג'ירפות, ג׳ימבו) + abbreviations (חו״ל). Scrapers set `Show.artist` = clean name, `Show.title` = full title (when richer). eventim uses the API's clean `attractions` name directly. `show_id` is now keyed on the per-source **URL** (stable, unique) so matinee/evening same-day shows don't collide.

## Known gotchas
- **grayclub returned HTTP 403** when fetched from some datacenter networks (anti-bot).
  If Actions logs show 403, adjust request headers (realistic User-Agent / Accept-Language)
  before assuming the scraper is broken. The engine already isolates per-scraper errors,
  so one failing site never stops the others.
- `clean_artist` (above) now extracts the artist from most show titles, but genuine
  edge cases (festivals, tributes like "35 שנים ל…", "15 שנות X") have no single
  artist and pass through as-is. Acceptable; the full title is preserved in `Show.title`.
- **Review tool:** `python make_reports.py` writes `reports/<source>.html` (one row per
  show: clean artist · full title · date · venue · link) so scraped data can be eyeballed.
  Run it whenever adding/fixing a scraper. (`reports/` is gitignored.)
- Sites change layout → scrapers break. When adding/fixing a scraper, fetch the live
  page first and confirm selectors before committing.

## Commands
```bash
pip install -r requirements.txt
SKIP_SATURDAY=false python scan.py     # local test; prints instead of sending if no token
```
Secrets/vars (GitHub → Settings → Secrets and variables → Actions):
- secret `TELEGRAM_BOT_TOKEN` (required)
- variable `EXAMPLE_SCRAPER=on` (optional, re-enables demo data; default is now off)

### Local dev environment (this Windows machine)
- `python` is NOT on PATH (Store stub only). Real interpreter:
  `C:\Users\Solav\AppData\Local\Programs\Python\Python312\python.exe`.
- Run scripts with `PYTHONUTF8=1` and `PYTHONPATH` = repo root so Hebrew prints and
  `scrapers`/`core` import. Deps installed: requests, bs4, lxml, **curl_cffi** (for zappa).
- Telegram token is only a GitHub Actions secret; not available locally. For a local
  end-to-end run, put it in a gitignored `.env` and export it before `scan.py`.

## Test / first-run notes
- First scan with an empty `shows.json` treats **every** show as new, but alerts go
  ONLY to followers — so with no subscribers it sends nothing and just populates data.
- To demo an alert in one run: seed `artists.json` (so the user can `/follow` first) and
  keep `shows.json` empty → the first scan's shows are "new" → the followed artist alerts.
- The bot is not always-on: a `/command` or button tap is only processed during the next
  workflow run (manual "Run workflow" = instant).

## Next steps
1. **End-to-end test in progress** (barby only, GitHub Actions). Confirm an alert lands.
2. Fix `grayclub` title selector (skip city-section headings) and re-enable its import.
3. Build `scrapers/zappa.py` from the Eventim API recipe above (curl_cffi). Decide with
   the user: zappa-only (`retail_partner=ZPE`) vs all Eventim Israel.
4. Optional: normalize tribute-show names for cleaner artist grouping.
5. Add more sites (Kupot Tel Aviv, etc.).
