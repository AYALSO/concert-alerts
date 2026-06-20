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
| `barby` | ✅ **built & verified** | `scrapers/barby.py`. Not HTML — hits the JSON API `GET https://barby.co.il/api/shows/find` → `returnShow.show[]` (`showId`, `showName`, `showDate` DD/MM/YYYY, `showTime`). Behind Cloudflare: must send full browser headers (UA + **Origin/Referer** are the key). URL = `https://www.barby.co.il/show/<showId>`. Verified **78 shows** live on 2026-06-20. |
| `grayclub` | ⚠️ built but **temporarily disabled** | `scrapers/grayclub.py` works but its heading-walk mislabels ~30/80 entries with **city names** (תלאביב/יהוד/מודיעין) and a newsletter heading instead of the artist. Import is commented out in `scrapers/__init__.py` until the title selector is fixed against the live DOM. ~50 of 80 entries are correct. |
| `zappa` | 🔬 **API cracked, paused** | zappa-club.co.il runs on **Eventim** infra and blocks plain curl/requests/WebFetch at the TLS layer (conn reset / tarpit). Bypass: `curl_cffi` with `impersonate="chrome"`. Don't scrape HTML (SSR only renders page 1 = 31 of ~170). Use the Eventim API — see recipe below. Build paused at user's request. |
| `kupot_ta` + others | later | Kupot Tel Aviv etc., after the first three are solid. |

### zappa / Eventim API recipe (for when we resume)
Fetch with `curl_cffi` (`pip install curl_cffi`, `requests.get(url, impersonate="chrome")`) — plain `requests` is reset at the edge.
- **Individual events** (what we want): `GET https://public-api.eventim.com/websearch/search/api/exploration/v1/products`
  params: `webId=web__eventim-co-il`, `language=he`, `categories=הופעות חיות` (the category **name**, NOT the URL's "51"), `page=N` (1-indexed). ~20/page, `totalPages`/`totalResults` in the response.
- Each product has: `attractions[0].name` (clean **artist**), `name` (event title), `link` (event URL on eventim.co.il), `productId` (stable id), `typeAttributes.liveEntertainment.location.{name,city}` (**venue**), `startDate` (confirm field when building).
- `webId=web__eventim-co-il` is **all Eventim Israel** live shows (superset of zappa-club). To scope to just zappa-club, filter by `retail_partner=ZPE` (the site's affiliate) — **confirm it actually narrows results** before relying on it. Broadening to all Eventim Israel = more coverage but a scope change → ask the user first.

## Known gotchas
- **grayclub returned HTTP 403** when fetched from some datacenter networks (anti-bot).
  If Actions logs show 403, adjust request headers (realistic User-Agent / Accept-Language)
  before assuming the scraper is broken. The engine already isolates per-scraper errors,
  so one failing site never stops the others.
- Tribute/cover shows (מחווה / "FEVER", "Moonlight", etc.) come through as the **show
  name**, not the underlying artist. Grouping still works; a later pass can clean names.
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
