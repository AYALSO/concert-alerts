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
| `example` | demo, ON | Fake data to prove the flow. Turn off with repo variable `EXAMPLE_SCRAPER=off`. |
| `grayclub` | ✅ built | Parses grayclub.co.il homepage (carousels already contain all upcoming shows; the "load more" button only affects one list). Dedupe by `/event/<a>/<b>/` URL. **Verified 76 upcoming shows** against a live fetch on 2026-06-20. |
| `barby` | ⏳ TODO | barby.co.il is a JavaScript SPA — homepage HTML is empty, shows load from an **API that must be discovered**. Needs direct network access to inspect. |
| `zappa` | ⏳ TODO | "כל ההופעות החיות" page: `https://www.zappa-club.co.il/events/הופעות-חיות-51/`. Site **blocks automated access (robots)** from some fetchers; needs direct access / a real browser UA. |
| `kupot_ta` + others | later | Kupot Tel Aviv etc., after the first three are solid. |

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
- variable `EXAMPLE_SCRAPER=off` (optional, disables demo data)

## Next steps
1. Get direct access to barby + zappa (open network access locally, or verify from
   Actions logs), then build `scrapers/barby.py` and `scrapers/zappa.py` the same way
   as grayclub, verifying the extracted data each time.
2. Optional: normalize tribute-show names for cleaner artist grouping.
3. Add more sites (Kupot Tel Aviv, etc.).
