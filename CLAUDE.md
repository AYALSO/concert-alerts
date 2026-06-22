# CLAUDE.md — Concert Alerts

Persistent project context. Claude Code reads this at the start of every session.
This file is the handoff from the original design conversation: it records what the
project is, the decisions already made, what's done, and what's left.

## Goal
Alert the user on Telegram whenever artists they follow announce **new** shows on
Israeli ticketing/venue sites. Runs free, automatically, in the cloud.

## How it works (architecture) — two free cloud pieces
**1. Hourly scan — GitHub Actions** (`.github/workflows/scan.yml`; cron, skips Saturday
unless a manual run passes `force=true`). Runs `scan.py`:
- `engine.run_scan()` runs every registered scraper, dedupes by `show_id`, detects shows
  not seen in the previous scan, grows the artist catalogue, commits `data/*.json`.
- New shows are POSTed to the Cloudflare Worker `/notify`, which alerts followers.
- `make_artist_page.py` regenerates `docs/index.html` (the Mini App), committed too.

**2. Real-time bot — Cloudflare Worker** (`worker/src/index.js`, a Telegram webhook —
instant, because an Actions cron can't reply in real time):
- Instant `/start`, in-chat search, and the **Mini App** (opened via an inline button)
  for picking artists. `/clear` resets follows.
- Follows live in Cloudflare **KV** (key `subscribers`), NOT the repo. The Mini App reads
  / writes them via `/api/follows` (GET/POST), authenticated by Telegram `initData` (HMAC).
- `/notify` (called by the scan) pushes alerts to followers.
- The catalogue is read from the public repo's raw `data/*.json`. Mini App is served by
  **GitHub Pages** (`/docs`, repo is public). Bot username: `@Tunaconcerts_bot`.

## Locked decisions (do not relitigate without asking the user)
- **Notifications: Telegram only.**
- **Hosting: free tier only.** Scan = GitHub Actions; bot = Cloudflare Worker (always-on
  webhook); Mini App = GitHub Pages; follows = Cloudflare KV.
- **One user flow:** open the bot → pick artists in the Mini App (or type to search) →
  get a push for each new show. Catalogue grows automatically.
- **Artist grouping:** stored once per `artist_key` (normalized, *cleaned* name); every
  show is a separate record. `core/artist_names.clean_artist` extracts the artist from
  marketing titles (e.g. "טיפקס- מופע צהרים" → "טיפקס"); full title kept in `Show.title`.

## Data model (`core/models.py`)
`Show`: artist (clean), date_raw, venue, url, source, date_iso, `title` (full show name),
scraped_at.
- `artist_key` = normalized cleaned name → grouping + the bot's follow keys.
- `show_id` = stable hash of **(source, event URL)** → detects "new" (URL is unique +
  stable per show). Kupat multi-date shows append `#<date>` to the url so each date is
  distinct. (Hash in `core/models.py` must match the JS `artistHash` in the Worker.)
Storage: repo `data/shows.json` + `data/artists.json` (committed by the scan). Follows
live in **Cloudflare KV** — the repo's `favorites.json`/`state.json` are legacy/unused.

## Scrapers (`scrapers/`)
Each site = one module subclassing `Scraper` (see `scrapers/base.py`), returning
`list[Show]`, registered via `@register` and imported in `scrapers/__init__.py`.

| Source | Status | Notes |
|---|---|---|
| `example` | demo, **OFF by default** | Fake data to prove the flow. Now off (workflow default `EXAMPLE_SCRAPER` flipped to `off` since a real scraper exists). Turn on with repo variable `EXAMPLE_SCRAPER=on`. |
| `barby` | ✅ **built & verified** | `scrapers/barby.py`. Not HTML — hits the JSON API `GET https://barby.co.il/api/shows/find` → `returnShow.show[]` (`showId`, `showName`, `showDate` DD/MM/YYYY, `showTime`). Behind Cloudflare: must send full browser headers (UA + **Origin/Referer** are the key). URL = `https://www.barby.co.il/show/<showId>`. Artist name is cleaned via `core/artist_names.clean_artist`; full title kept in `Show.title`. Verified **78 shows / 52 artists** live on 2026-06-20. |
| `eventim` | ✅ **built & verified** | `scrapers/eventim.py`. Covers zappa-club **and all Eventim Israel venues** (זאפה, היכל התרבות, אמפי קיסריה, …). zappa-club is an Eventim white-label; both block plain HTTP at the TLS layer → use `curl_cffi` `impersonate="chrome"`. Reads the Eventim API (recipe below); `attractions[0].name` is already a clean artist. Verified **269 shows / 155 artists** live on 2026-06-20. |
| `grayclub` | ✅ **built & verified** | `scrapers/grayclub.py`. Card-based: iterates `div.article-list` (each card = `<h3>` title + DD.MM.YYYY date + one `/event/<a>/<b>/` link). Drives off `<h3>` (the title), NOT the city-section `<h2>` (תלאביב/יהוד/מודיעין) — that was the old bug. Dedupe by event path; `clean_artist` applied. Verified **75 shows / 69 artists** live on 2026-06-20. |
| `kupat` | ✅ **built & verified** | `scrapers/kupat.py`. Kupat Tel Aviv (WordPress aggregator). Homepage cards (`article.item-show`, skip `external_url` promos) → each show's detail page (`ul.show-details-list`). Handles single-date (labelled "תאריך"/"מיקום") and multi-date layouts (one "DD/MM venue" row per date). A tour under ONE url with many dates/cities → a Show per date+venue (e.g. כשאמא באה הנה = 10). **Every** date is keyed `url#<iso>` (even single-date) — so when a popular show opens a new date, that adds exactly ONE new `show_id` and never re-keys the existing dates. (Bug fixed 2026-06-21: single-date shows used a bare url, so opening a 2nd date flipped them to `url#iso` and re-alerted every date — e.g. עדן בן זקן showed as 3 new instead of 1.) Years inferred; fetched fresh each scan (~46 detail fetches). Verified **70 date-events / 44 artists** on 2026-06-20. |
| `comy` | ✅ **built & verified** | `scrapers/comy.py`. COMY (comy.co.il) — **stand-up only**. Homepage `.event` cards give each act's `<h3>` name + `a.event-inner` event url; we then fetch each event's detail page and emit **one Show per date** (`.single-event-details` rows → `.date` DD.MM + `.single-light` day/time + `.single-place-string` venue). Year inferred (nearest future); url gets `#<iso>` so each date is a distinct `show_id` (same-date rows dedupe). Per-date is deliberate: comedians open new dates one at a time, so each new date must raise its own alert (`url#iso` keys it). ~89 detail fetches/scan, curl_cffi `impersonate="chrome"`. **Every COMY artist is forced to category `standup`** (see `scan.force_standup` / `make_artist_page`). Verified **456 date-shows / 85 artists** on 2026-06-21. |
| `comedybar` | ✅ **built & verified** | `scrapers/comedybar.py`. Comedy Bar sells via SmartTicket at **tickets.comedybar.net**. The homepage is only a rolling window of the soonest ~100 cards, so a far date looked "new" the day it entered the window (re-alert bug). Fixed: the homepage is used **only to discover** single-comedian shows (`.show_cube` `.h2` title; recurring club nights — open-mic, marathons, "ערב סטנד אפ עם כוכבי…", "קומדי בר <city>", "במה פתוחה…" — skipped via `_SKIP`; Arabic-script dupes skipped; "<name> במופע סטנד אפ" → suffix stripped → `clean_artist`). Then we fetch each show's **detail page** and read its FULL date table (`table tbody tr`: date "ביום … 3 ביולי 2026" + venue + `?id`), so we always have the complete schedule (e.g. שחר חסון 24 dates to Aug 29, not the 16 the homepage showed). **One Show per date**, url `…/#<iso>` (since `show_id` strips the `?query` and the path repeats per date; ids stay stable). **Forced `standup`** like COMY. curl_cffi impersonation. Verified **48 date-shows / 8 artists** on 2026-06-22. |
| (others) | not planned | **Cross-source dedup deferred:** a live check found 0 same-artist+same-date overlaps across barby/eventim/gray/kupat. Stand-up acts *can* span COMY / Comedy Bar / Kupat → a follower might get one alert per source for the same act; revisit dedup only if this proves noisy. Kupat's other linked sites (brennerock/amphi) are still NOT scraped. **`STANDUP_SOURCES`** (in `scan.py`) = `{comy, comedybar}` drives the forced-standup logic; add future stand-up-only sites there. |

### Eventim API recipe (as built in `scrapers/eventim.py`)
Fetch with `curl_cffi` (`requests.get(url, impersonate="chrome")`) — plain `requests` is reset at the edge.
- **Individual events**: `GET https://public-api.eventim.com/websearch/search/api/exploration/v1/products`
  params: `webId=web__eventim-co-il`, `language=he`, `categories=הופעות חיות` (the category **name**, NOT the URL's "51"), `page=N` (1-indexed, ~20/page). `totalPages` is unreliable — paginate until a page adds no new `productId`.
- Each product: `attractions[0].name` (clean **artist**), `name` (event title → `Show.title`), `link` (event URL), `productId` (stable id), `typeAttributes.liveEntertainment.location.{name,city}` (**venue**), `typeAttributes.liveEntertainment.startDate` (ISO datetime).
- `web__eventim-co-il` = **all Eventim Israel** live shows (we chose this over zappa-only for max coverage). To narrow to just zappa-club, add `retail_partner=ZPE` — confirm it actually narrows before relying on it.

### Artist-name unification (`core/artist_names.py`)
`clean_artist(raw)` extracts the core artist from a marketing title (e.g. "טיפקס- מופע צהרים" → "טיפקס", "ג'ירפות - חוגגים…" → "ג'ירפות"). Splits on dashes/`|`/`:` adjacent to space or Hebrew (keeps "T-Puse"), cuts at description keywords (מופע/אורח/חוגג/השקת/לייב…), drops venue/filler (בבארבי…), and preserves intra-word geresh (ג'ירפות, ג׳ימבו) + abbreviations (חו״ל). Scrapers set `Show.artist` = clean name, `Show.title` = full title (when richer). eventim uses the API's clean `attractions` name directly. `show_id` is now keyed on the per-source **URL** (stable, unique) so matinee/evening same-day shows don't collide.

### AI classification (Google Gemini, web-grounded — free; Workers AI fallback)
The Worker's `/classify` endpoint turns each artist name into `{is_artist, name,
category}` (category ∈ music/standup/theater). It prefers **Google Gemini**
(`gemini-2.5-flash` with **Google-Search grounding** — actually verifies each
performer on the web, so e.g. bare "קובי מימון" → standup correctly) when the
`GEMINI_API_KEY` secret is set; otherwise it falls back to Cloudflare **Workers AI**
(`@cf/meta/llama-3.3-70b-instruct-fp8-fast`, no live web). Results cache in KV
(`cls3:<hash>`, successes only; titles it can't classify are **omitted**, not
defaulted, so a quota hiccup never sticks a wrong label).
- **Free-quota reality:** Gemini's free *grounded-search* allowance is small
  (~70 artists/day; `gemini-2.0-flash` had 0 free quota — 2.5-flash works). So
  `scan.classify_artists(cap=60)` upgrades the catalogue **incrementally** — it
  re-classifies only artists whose `cat_v` ≠ `CLS_VERSION` (in `scan.py`), keeping
  the existing category until Gemini re-does it (**no regression**). The ~290-artist
  bulk fills in over a few daily scans; ongoing only adds 0–2 new artists/scan, well
  within quota. Bump `CLS_VERSION` (+ the `cls3:` Worker cache prefix) to force a
  full re-classify after a model/prompt change.
- `make_artist_page` hides `is_artist:false` (e.g. "אקספו מכביה סיטי", festivals) and
  adds music/standup/theater filter chips. The build only forces stand-up sources;
  all manual fixes are **online** (below). The scan report shows Gemini's non-artist
  flags distinctly (`🚫 סומנו N לא-אמנים`) so they're not mistaken for mis-categorised
  artists. `engine.run_scan` **prunes** an `is_artist:false` entry once it has no live
  show, so flagged junk doesn't linger; obvious Eventim promo pages ("הטבות לעובדי …",
  "המיוחדים שלנו") are also dropped at the scraper.
- **Online admin panel (manual name/category fixes):** `docs/admin.html` is a Telegram
  Mini App the developer opens by sending **`/id`** to the bot (its reply has a
  "🛠 פאנל ניהול אמנים" button). It lists every artist with an editable name, a
  category dropdown (music/standup/theater) and a hide toggle, and **Save** POSTs the
  full map to the Worker `/api/overrides`. Overrides live in **KV** (key `overrides`,
  `{ "<artist_key>": {name?, category?, is_artist?} }`), keyed by stable `artist_key`
  so renames don't break follows. `/api/overrides` GET is public, POST is restricted
  to the admin chat (the one `/id` stored in `admin_chat`), validated by Telegram
  `initData`. `/api/catalogue` is a CORS passthrough of `artists.json` for the panel.
  The **public Mini App fetches `/api/overrides` at load** and applies them live
  (rename / recategorize / hide), so edits show up without a rebuild. (There is no
  local override file — KV is the single source.)

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
SKIP_SATURDAY=false PYTHONPATH=. python scan.py   # local scan (POSTs new shows to the Worker if WORKER_NOTIFY_URL+NOTIFY_SECRET are set)
python make_reports.py        # reports/<source>.html — eyeball scraped data (gitignored)
python make_artist_page.py    # regenerate docs/index.html (the Mini App)
```
GitHub Actions (repo → Settings → Secrets and variables → Actions):
- secret `NOTIFY_SECRET` (shared with the Worker) + variable `WORKER_NOTIFY_URL` (Worker `/notify`).
- variable `EXAMPLE_SCRAPER=on` to re-enable demo data (default off).
- The Telegram **bot token lives on the Worker**, not Actions.

### Cloud deployment / Worker ops
- **Worker** `concert-alerts-bot` (Cloudflare acct `Ayalsolav@gmail.com`, id `0d00c270…`),
  URL `https://concert-alerts-bot.tunaconcerts.workers.dev`. Deploy: `cd worker && npx wrangler deploy`
  with `CLOUDFLARE_API_TOKEN` in env. KV namespace `SUBS` holds follows (key `subscribers`).
- **Worker secrets** (`wrangler secret put NAME`, pipe value via `printf '%s'` — a trailing
  newline breaks them!): `BOT_TOKEN`, `WEBHOOK_SECRET`, `NOTIFY_SECRET`.
- **Telegram webhook** → `…/tg` with `secret_token=WEBHOOK_SECRET` (Bot API `setWebhook`).
- **GitHub Pages** serves `/docs` (Mini App). Bot opens it via an **inline** web_app button
  AND a persistent **menu button** (set via `setChatMenuButton`) — both pass `initData`
  (a reply-keyboard web_app does NOT, which is why those are avoided). The page calls
  `/api/follows` (GET pre-tick / POST save) authenticated by that initData.
- **Dev scan report (every scan):** sending `/id` (or `/admin`) to the bot stores the
  sender's chat_id in KV key `admin_chat`. `scan.py` POSTs to `/notify` on **every**
  run (even with zero new shows) with `{shows, stats, classify, ts}`; the Worker's
  `scanDigest()` pings `admin_chat` a report: a per-source line (`<source>: N הופעות ·
  אין/🆕 חדשות`, or `⚠️ שגיאה` if a scraper threw — so failures & true scan frequency
  are visible since the Actions cron is unreliable), a `🤖 ג'מיני` line listing what
  Gemini just classified, then the new shows (artist · date · venue · source). Clear
  `admin_chat` to disable. (`stats` come from `engine.run_scan`'s 3rd return value;
  `classify` from `scan.classify_artists`.) (`GEMINI_API_KEY` is also a Worker secret.)

### Local dev (this Windows machine)
- `python` not on PATH → `C:\Users\Solav\AppData\Local\Programs\Python\Python312\python.exe`.
  Node `C:\Program Files\nodejs`; `gh` `C:\Program Files\GitHub CLI\gh.exe`.
- Run with `PYTHONUTF8=1` + `PYTHONPATH`=repo root (Hebrew output + `scrapers`/`core` imports).
- **The PowerShell tool is blocked by antivirus** (`EPERM uv_spawn powershell.exe`) — use Bash.
- Local secrets in a gitignored `.env`: `TELEGRAM_BOT_TOKEN`, `CLOUDFLARE_API_TOKEN`.

## Next steps (optional)
1. **Artist-list cleanup via Claude** (the planned cheap pass): drop non-artist events
   (plays/festivals, "מי רצח את…") and perfect tricky names. Needs an Anthropic API key.
2. Cross-source dedup only if real overlaps appear (a live check found none today).
3. More sources only if they add dated, non-duplicate coverage.
