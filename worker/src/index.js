/**
 * Concert-alerts Telegram bot — Cloudflare Worker (always-on, instant).
 *
 * Routes:
 *   POST /tg      Telegram webhook (verified via X-Telegram-Bot-Api-Secret-Token)
 *   POST /notify  GitHub Actions scanner posts new shows -> we push to followers
 *   GET  /        health check
 *
 * Reads the artist/show catalogue from the public repo's raw JSON; follows live
 * in KV (binding SUBS, one key "subscribers" = { chat_id: { follows: [...] } }).
 * Secrets: BOT_TOKEN, WEBHOOK_SECRET, NOTIFY_SECRET.
 */
const REPO = "AYALSO/concert-alerts";
const RAW = `https://raw.githubusercontent.com/${REPO}/main/data`;
const WEBAPP_URL = "https://ayalso.github.io/concert-alerts/";
const SEARCH_LIMIT = 12;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/tg") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET)
        return new Response("unauthorized", { status: 401 });
      try { await handleUpdate(await request.json(), env); }
      catch (e) { console.log("update error", e); }
      return new Response("ok");
    }
    if (request.method === "POST" && url.pathname === "/notify") {
      const body = await request.json().catch(() => ({}));
      if (body.secret !== env.NOTIFY_SECRET) return new Response("unauthorized", { status: 401 });
      // Each step guarded: a push failure must not lose the daily accumulation
      // or the auto-merge recording (and vice versa).
      let n = 0, m = 0;
      try { n = await pushNewShows(body.shows || [], env); }
      catch (e) { console.log("pushNewShows error", e); }
      try { await bumpDaily(body, env); }       // accumulate for the ~22:00 daily summary
      catch (e) { console.log("bumpDaily error", e); }
      try { m = await recordAutoMerges(body.auto_merges, env); }
      catch (e) { console.log("recordAutoMerges error", e); }
      return Response.json({ alerts: n, merges: m });
    }
    // Mini App API (authenticated by Telegram initData): GET current follows, POST to save.
    if (url.pathname === "/api/follows") return handleApi(request, url, env);
    // Admin override store (artist name/category fixes): GET public, POST admin-only.
    if (url.pathname === "/api/overrides") return handleOverrides(request, env);
    // Full artist catalogue (for the admin panel; CORS-enabled passthrough of raw JSON).
    if (url.pathname === "/api/catalogue") {
      const a = await fetchJSON("artists.json");
      if (a === null) return Response.json({ error: "data unavailable" }, { status: 503, headers: CORS });
      return Response.json(a, { headers: CORS });
    }
    // Developer control-center: active users + their follows (admin-only).
    if (url.pathname === "/api/users") return handleUsers(request, url, env);
    // Title classifier for the scan (Workers AI, cached). Secret-protected.
    if (url.pathname === "/classify" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      if (b.secret !== env.NOTIFY_SECRET) return new Response("unauthorized", { status: 401 });
      // __v lets the scan verify prompt/cache version before stamping cat_v.
      return Response.json({ __v: CLS_VERSION, ...(await classifyTitles(b.titles || [], env)) });
    }
    return new Response("concert-alerts bot up");
  },

  // Cloudflare cron (hourly): send the ~22:00 daily summary, then dispatch the scan
  // (active hours only). Reliable, unlike GitHub's own schedule cron.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCron(env));
  },
};

async function runCron(env) {
  const hr = Number(new Intl.DateTimeFormat("en-US",
    { timeZone: "Asia/Jerusalem", hour: "numeric", hour12: false }).format(new Date()));
  if (hr === 22) await sendDailySummary(env);      // one evening summary (~22:00 Israel)
  if (hr >= 1 && hr <= 6) return;                  // skip 01:00–06:00 Israel (matches scan.py)
  if (!env.GH_TOKEN) { console.log("no GH_TOKEN; skip dispatch"); return; }
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/scan.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json",
               "User-Agent": "concert-alerts-cron", "X-GitHub-Api-Version": "2022-11-28" },
    body: JSON.stringify({ ref: "main", inputs: { force: "true" } }),
  });
  if (!r.ok) console.log("dispatch scan failed", r.status, (await r.text()).slice(0, 160));
}

const DAILY0 = { scans: 0, new_shows: 0, new_artists: 0, shows: [], artists: [] };

// Daily developer summary (replaces the per-scan report): the day's scan count +
// new shows + new artists, WITH the detail of what was added. Accumulated by
// bumpDaily() on every /notify, sent ~22:00 then reset.
async function sendDailySummary(env) {
  const admin = env.ADMIN_CHAT_ID || (await env.SUBS.get("admin_chat"));
  if (!admin) return;
  const d = (await env.SUBS.get("daily", "json")) || DAILY0;
  // Retro-filter: a name that entered the day's record BEFORE it was classified
  // may have been flagged junk (or hidden by the admin) since — drop it now.
  let junk = () => false;
  try {
    const cat = await fetchJSON("artists.json");
    const ov = (await env.SUBS.get("overrides", "json")) || {};
    junk = (key) => {
      if (!key) return false;
      const o = ov[key] || {};
      if (o.merge_into) return true;                       // absorbed duplicate
      if ("is_artist" in o) return o.is_artist === false;
      return (cat && cat[key]) ? cat[key].is_artist === false : false;
    };
  } catch (e) { console.log("summary retro-filter", e); }
  const artists = (d.artists || []).filter((a) => !junk(normalize(typeof a === "string" ? a : a.n)));
  const shows = (d.shows || []).filter((s) => !junk(s.k || normalize(s.a)));
  const lines = ["\u{1F4C5} <b>סיכום יומי</b>",
    `\u{1F501} ${d.scans || 0} סריקות`,
    `\u{1F195} ${shows.length || d.new_shows || 0} הופעות חדשות · \u{1F3A4} ${artists.length} אמנים חדשים`];
  if (artists.length) {
    lines.push("", "\u{1F3A4} <b>אמנים חדשים:</b>");
    artists.slice(0, 20).forEach((a) => lines.push(`• ${esc(typeof a === "string" ? a : a.n)}`));
    if (artists.length > 20) lines.push(`\u{2026}ועוד ${artists.length - 20}`);
  }
  if (shows.length) {
    lines.push("", "\u{1F195} <b>הופעות חדשות:</b>");
    shows.slice(0, 25).forEach((s) =>
      lines.push(`• <b>${esc(s.a)}</b> — ${esc(s.d || "")} · ${esc(s.v || "")} (${esc(s.src || "")})`));
    if (shows.length > 25) lines.push(`\u{2026}ועוד ${shows.length - 25}`);
  }
  // Reset ONLY after a confirmed delivery — a transient failure at 22:00 must
  // not destroy the whole day's report (it simply rolls into tomorrow's).
  try {
    const r = await send(env, admin, lines.join("\n"));
    if (r && r.ok) await env.SUBS.put("daily", JSON.stringify(DAILY0));
    else console.log("daily summary rejected", JSON.stringify(r).slice(0, 200));
  } catch (e) { console.log("daily summary", e); }
}

async function bumpDaily(body, env) {
  const d = (await env.SUBS.get("daily", "json")) || {};
  const shows = body.shows || [], arts = body.new_artists || [];
  d.scans = (d.scans || 0) + 1;
  d.new_shows = (d.new_shows || 0) + shows.length;
  d.new_artists = (d.new_artists || 0) + arts.length;
  d.shows = d.shows || []; d.artists = d.artists || [];
  for (const s of shows) if (d.shows.length < 80)
    d.shows.push({ a: s.artist, d: s.date_raw, v: s.venue, src: s.source, k: s.artist_key });
  for (const a of arts) if (d.artists.length < 60) d.artists.push(a);
  await env.SUBS.put("daily", JSON.stringify(d));
}

// ---- Telegram + data helpers -------------------------------------------------
async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json().catch(() => ({}));
}
const send = (env, chat, text, extra = {}) =>
  tg(env, "sendMessage", { chat_id: chat, text, parse_mode: "HTML",
    disable_web_page_preview: true, ...extra });

// Data lives in the repo; raw.githubusercontent sometimes rate-limits/blocks
// Cloudflare egress IPs (seen 2026-07-07: every fetch failed while browsers were
// fine — catalogue/search went empty). The scan therefore ALSO publishes
// data/*.json to GitHub Pages (docs/data/, Fastly CDN) as a fallback source.
const PAGES_DATA = `${WEBAPP_URL}data`;
// Returns the parsed JSON, or NULL when both sources failed — callers must
// distinguish "outage" from "empty" (an empty {} made search/upcoming reply
// "no results" during the 2026-07-07 outage, confidently lying to users).
async function fetchJSON(path) {
  let r = await fetch(`${RAW}/${path}`, { cf: { cacheTtl: 60, cacheEverything: true } });
  if (!r.ok) {
    console.log("raw fetch failed", path, r.status, "-> pages fallback");
    r = await fetch(`${PAGES_DATA}/${path}`, { cf: { cacheTtl: 60, cacheEverything: true } });
  }
  if (!r.ok) { console.log("pages fetch failed too", path, r.status); return null; }
  return r.json().catch(() => null);
}
const DATA_DOWN = "⚠️ תקלה זמנית בטעינת הנתונים — נסה שוב בעוד דקה.";
// One SHA-1 pass over the catalogue (hash -> key), shared by every hash lookup.
async function hashMap(artists) {
  const m = new Map();
  for (const k of Object.keys(artists)) m.set(await artistHash(k), k);
  return m;
}
// Today's date in Israel (UTC leaves yesterday's shows "upcoming" until 02:00-03:00).
const israelToday = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Jerusalem" }).format(new Date());
const getSubs = (env) => env.SUBS.get("subscribers", "json").then((v) => v || { subscribers: {} });
const saveSubs = (env, s) => env.SUBS.put("subscribers", JSON.stringify(s));

// Duplicate merges proposed by the scan (same display name under two catalogue
// keys, e.g. a shortened marketing title next to the plain artist key). Stored
// in the same KV overrides as manual merges, so follows keep resolving and the
// next scan collapses the catalogue entries. Guarded against cycles.
async function recordAutoMerges(m, env) {
  if (!m || typeof m !== "object") return 0;
  const ov = (await env.SUBS.get("overrides", "json")) || {};
  let n = 0;
  for (const [loser, winner] of Object.entries(m).slice(0, 50)) {
    if (typeof winner !== "string" || !winner || loser === winner) continue;
    if (ov[loser] && ov[loser].merge_into) continue;   // already merged (manually or before)
    let w = winner;
    const seen = new Set();
    while (ov[w] && ov[w].merge_into && !seen.has(w)) { seen.add(w); w = ov[w].merge_into; }
    if (w === loser) continue;                         // would close a cycle
    ov[loser] = { ...(ov[loser] || {}), merge_into: winner, auto: true };
    n++;
  }
  if (n) await env.SUBS.put("overrides", JSON.stringify(ov));
  return n;
}

// Manual artist merges (loser_key -> winner_key) from the admin overrides, so a
// follow of an absorbed artist still matches the surviving one.
async function getMerges(env) {
  const ov = (await env.SUBS.get("overrides", "json")) || {};
  const m = {};
  for (const k in ov) if (ov[k] && ov[k].merge_into) m[k] = ov[k].merge_into;
  return m;
}
function mergeKey(k, m) {
  const seen = new Set();
  while (m[k] && !seen.has(k)) { seen.add(k); k = m[k]; }
  return k;
}

async function artistHash(key) {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(key));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}
const normalize = (s) =>
  (s || "").toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, "").replace(/\s+/g, " ").trim();

// ---- Mini App API (authenticated via Telegram initData) ---------------------
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
async function hmac(keyBytes, msgBytes) {
  const k = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, msgBytes));
}
// Returns the chat_id (as string) if initData is genuine, else null.
async function validateInitData(initData, botToken) {
  const p = new URLSearchParams(initData || "");
  const hash = p.get("hash");
  if (!hash) return null;
  p.delete("hash");
  const dcs = [...p.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .map(([k, v]) => `${k}=${v}`).join("\n");
  const enc = new TextEncoder();
  const secret = await hmac(enc.encode("WebAppData"), enc.encode(botToken));
  const sig = await hmac(secret, enc.encode(dcs));
  const hex = [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (hex !== hash) return null;
  // Freshness: Telegram mints new initData on every Mini App open, so a valid
  // string older than 24h is a replayed/leaked credential, not a live session
  // (the GET path put it in the query string, where logs can capture it).
  const age = Date.now() / 1000 - Number(p.get("auth_date") || 0);
  if (!(age >= -300 && age < 86400)) return null;
  try { return String(JSON.parse(p.get("user")).id); } catch { return null; }
}
// Admin authorization comes ONLY from the ADMIN_CHAT_ID secret. (The old KV
// "admin_chat" bootstrap meant that with the secret unset, the first stranger
// to send /id became the admin — panel, user list, everything.)
const isAdmin = (env, chat) => !!env.ADMIN_CHAT_ID && String(chat) === String(env.ADMIN_CHAT_ID);
async function handleApi(request, url, env) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  let initData = "", hashes = null;
  if (request.method === "GET") {
    initData = url.searchParams.get("initData") || "";
  } else {
    const b = await request.json().catch(() => ({}));
    initData = b.initData || ""; hashes = b.hashes;
  }
  const chat = await validateInitData(initData, env.BOT_TOKEN);
  if (!chat) return Response.json({ error: "unauthorized" }, { status: 401, headers: CORS });

  if (request.method === "GET") {                 // return current follows as hashes
    const merges = await getMerges(env);
    const follows = [...new Set(followsOf(await getSubs(env), chat).map((k) => mergeKey(k, merges)))];
    const hs = await Promise.all(follows.map(artistHash));
    return Response.json({ hashes: hs }, { headers: CORS });
  }
  if (!Array.isArray(hashes)) return Response.json({ error: "bad" }, { status: 400, headers: CORS });
  const artists = await fetchJSON("artists.json");
  if (artists === null)                            // outage: saving now would wipe follows
    return Response.json({ error: "data unavailable" }, { status: 503, headers: CORS });
  const byHash = await hashMap(artists);
  const follows = [];
  for (const h of hashes) { const k = byHash.get(h); if (k && !follows.includes(k)) follows.push(k); }
  const subs = await ensureSub(env, chat);
  subs.subscribers[chat].follows = follows;
  try {                                            // capture name for the control center
    const u = JSON.parse(new URLSearchParams(initData).get("user"));
    const nm = u.first_name || u.username;
    if (nm) subs.subscribers[chat].name = nm;
  } catch (e) { /* no user info */ }
  await saveSubs(env, subs);
  const names = follows.map((k) => artists[k]?.display || k);
  await send(env, chat, follows.length
    ? `✅ עוקב אחרי ${follows.length} אמנים:\n` + names.map((n) => `• ${esc(n)}`).join("\n")
    : "רשימת המעקב נוקתה — לא עוקב אחרי אף אמן כרגע.");
  if (follows.length) {
    const txt = await upcomingText(new Set(follows));
    await send(env, chat, txt ||
      "אין כרגע תאריכים זמינים לאמנים שבחרת (ייתכן שאזלו הכרטיסים) — תקבל פוש ברגע שייפתח תאריך חדש \u{1F514}");
  }
  return Response.json({ ok: true, count: follows.length }, { headers: CORS });
}

// Inline button that opens the Mini App. Inline (not reply-keyboard) => Telegram
// passes initData, so the app can fetch the user's LIVE follows and pre-tick them.
// Admin overrides: { "<artist_key>": {name?, category?, is_artist?} }. GET is public
// (the Mini App applies them live); POST is restricted to the admin chat (the one
// stored by /id). The admin panel sends the full map each save.
async function handleOverrides(request, env) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (request.method === "GET")
    return Response.json((await env.SUBS.get("overrides", "json")) || {}, { headers: CORS });
  if (request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const chat = await validateInitData(b.initData || "", env.BOT_TOKEN);
    if (!chat || !isAdmin(env, chat))
      return Response.json({ error: "forbidden" }, { status: 403, headers: CORS });
    const ov = b.overrides && typeof b.overrides === "object" ? b.overrides : {};
    // The panel posts a full map built from what it loaded at OPEN time, so a
    // blind replace would wipe merge redirects recorded since (the hourly
    // scan's auto-merges) — losing them silently breaks follows. Carry forward
    // any stored merge_into the posted map doesn't mention; an explicit
    // {merge_into: null} from the panel is a deliberate un-merge (tombstone).
    const stored = (await env.SUBS.get("overrides", "json")) || {};
    for (const [k, v] of Object.entries(stored)) {
      if (v && v.merge_into && !(k in ov)) ov[k] = v;
    }
    for (const k of Object.keys(ov)) {
      if (ov[k] && ov[k].merge_into === null) {
        delete ov[k].merge_into;
        if (!Object.keys(ov[k]).length) delete ov[k];
      }
    }
    await env.SUBS.put("overrides", JSON.stringify(ov));
    return Response.json({ ok: true, count: Object.keys(ov).length }, { headers: CORS });
  }
  return new Response("method", { status: 405, headers: CORS });
}

// Developer control-center data (admin-only): how many users, what each follows,
// and the most-followed artists. Reads subscribers from KV.
async function handleUsers(request, url, env) {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  const chat = await validateInitData(url.searchParams.get("initData") || "", env.BOT_TOKEN);
  if (!chat || !isAdmin(env, chat))
    return Response.json({ error: "forbidden" }, { status: 403, headers: CORS });
  const subs = (await getSubs(env)).subscribers || {};
  const artists = (await fetchJSON("artists.json")) || {};
  const merges = await getMerges(env);
  const users = Object.entries(subs).map(([id, sub]) => {
    const follows = [...new Set((sub.follows || []).map((k) => mergeKey(k, merges)))];
    return { id, name: sub.name || "", count: follows.length,
             names: follows.map((k) => artists[k]?.display || k).sort((a, b) => a.localeCompare(b, "he")) };
  }).sort((a, b) => b.count - a.count);
  const tally = {};
  for (const u of users) for (const n of u.names) tally[n] = (tally[n] || 0) + 1;
  const topArtists = Object.entries(tally).sort((a, b) => b[1] - a[1]);
  return Response.json({
    total: users.length,
    active: users.filter((u) => u.count > 0).length,
    follows: users.reduce((s, u) => s + u.count, 0),
    topArtists, users,
  }, { headers: CORS });
}

function webappKeyboard() {
  // Telegram caches the Mini App page, so a freshly-scanned artist list can look
  // stale. Bump the URL each hour (≈ the scan cadence) to force a fresh load.
  const v = new Date().toISOString().slice(0, 13).replace(/[-T]/g, "");   // YYYYMMDDHH
  return { inline_keyboard: [[{ text: "\u{1F3A4} פתח את רשימת האמנים", web_app: { url: `${WEBAPP_URL}?v=${v}` } }]] };
}
const followsOf = (subs, chat) => subs.subscribers[chat]?.follows || [];

function formatShow(s) {
  const lines = [`\u{1F3B5} <b>${esc(s.artist)}</b> — הופעה חדשה!`];
  if (s.title && s.title !== s.artist) lines.push(esc(s.title));
  lines.push(`\u{1F4C5} ${esc(s.date_raw || "")}`, `\u{1F4CD} ${esc(s.venue || "")}`, `\u{1F517} ${esc(s.url || "")}`);
  return lines.join("\n");
}
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

const UPCOMING_MAX_CHARS = 3800;       // stay under Telegram's 4096-char message limit
const PER_ARTIST_URL_CAP = 10;
// Per followed artist, group their dates by BASE URL (the url without the #date
// fragment). When all dates share one page (comy / comedybar / kupat) -> ONE link.
// When each date is its own page (barby / eventim) -> a link per date, so a "4
// תאריכים" never opens just one. New dates still arrive later as push alerts.
async function upcomingText(followSet) {
  const shows = await fetchJSON("shows.json");
  if (shows === null) return DATA_DOWN;
  const today = israelToday();
  const mine = Object.values(shows).filter(
    (s) => followSet.has(s.artist_key) && !s.sold_out && (!s.date_iso || s.date_iso >= today));
  if (!mine.length) return null;

  const byArtist = new Map();
  for (const s of mine) {
    let a = byArtist.get(s.artist_key);
    if (!a) { a = { artist: s.artist, soonest: s, urls: new Map() }; byArtist.set(s.artist_key, a); }
    a.artist = s.artist;
    if ((s.date_iso || "9999") < (a.soonest.date_iso || "9999")) a.soonest = s;
    const base = (s.url || "").split("#")[0];
    let u = a.urls.get(base);
    if (!u) { u = { url: base, count: 0, soonest: s }; a.urls.set(base, u); }
    u.count++;
    if ((s.date_iso || "9999") < (u.soonest.date_iso || "9999")) u.soonest = s;
  }
  const arts = [...byArtist.values()]
    .sort((a, b) => (a.soonest.date_iso || "").localeCompare(b.soonest.date_iso || ""));

  const label = (u) => (u.count > 1 ? `${u.count} תאריכים` : esc(u.soonest.date_raw || ""));
  const block = (a) => {
    const urls = [...a.urls.values()]
      .sort((x, y) => (x.soonest.date_iso || "").localeCompare(y.soonest.date_iso || ""));
    if (urls.length === 1)                              // all dates on one page -> single link
      return `\u{1F3A4} <b>${esc(a.artist)}</b> — ${label(urls[0])}\n${esc(urls[0].url)}`;
    const rows = urls.slice(0, PER_ARTIST_URL_CAP).map((u) => `• ${label(u)} — ${esc(u.url)}`);
    if (urls.length > PER_ARTIST_URL_CAP) rows.push(`\u{2026}ועוד ${urls.length - PER_ARTIST_URL_CAP}`);
    return `\u{1F3A4} <b>${esc(a.artist)}</b>\n` + rows.join("\n");   // separate page per date
  };

  const out = ["\u{1F3B6} <b>האמנים שלך וההופעות שלהם:</b>"];
  let len = out[0].length, dropped = 0;
  for (let i = 0; i < arts.length; i++) {
    const b = block(arts[i]);
    if (len + b.length + 2 > UPCOMING_MAX_CHARS) { dropped = arts.length - i; break; }
    out.push(b); len += b.length + 2;
  }
  if (dropped) out.push(`\u{2026}ועוד ${dropped} אמנים`);
  out.push("\u{1F514} תקבל פוש על כל תאריך חדש שייפתח.");
  return out.join("\n\n");
}

// ---- title classification (Workers AI, cached in KV) ------------------------
const AI_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
const CLS_CATS = ["music", "standup", "theater", "lecture"];
const CLS_VERSION = 4;                 // must match scan.py's CLS_VERSION + the cache prefix
const CLS_SYS =
  "You classify Israeli live-event acts (mostly Hebrew) for a concert-alert app. " +
  "Each numbered line is an act name, optionally followed by ' ::: ' and a sample " +
  "event title @ venue as context. Judge the ACT (use the context and what you know " +
  "about the actual performer). For EACH line return one JSON object with: " +
  '"is_artist" — true ONLY for a specific named act people would follow for future ' +
  "shows: a musician/singer/band, a stand-up comedian, a named theater/dance/ballet/" +
  "children's show, or a named lecturer/speaker (author, academic, journalist) who " +
  "tours with talks. false for everything else, including: festivals and multi-artist " +
  "line-ups; organization/committee/employee events (ועד עובדים, הטבות לעובדי…); " +
  "conference/expo/webinar pages; parties, DJ/dance nights, karaoke, trivia and quiz " +
  "nights; film screenings and sports broadcasts (מונדיאל); voucher/benefit/marketing " +
  'pages; one-off date-or-occasion events (e.g. "לילה 1 באפריל", holiday specials); ' +
  "one-off tribute or cover evenings (מחווה ל…, הצדעה ל…) that are not a standing " +
  "named act. When unsure whether it names a real recurring act, use false. " +
  '"name" — the core performer/show name ONLY, stripped of tour/album/venue words and ' +
  'of hosting/guest phrases (מארחת את…, מציג…, feat.) — e.g. "נגה ארז מארחת את שקל " ' +
  '→ "נגה ארז", "מייקל הרפז שר אלטון ג\'ון" → "מייקל הרפז" — in the original language. ' +
  '"category" — exactly one of: "music" (concerts, singers, bands), "standup" (stand-up ' +
  'comedy), "theater" (plays, musicals, ballet, dance, children\'s shows), "lecture" ' +
  "(talks, lectures, live podcasts, literary/journalism evenings). " +
  '"i" — the input line number of this act (echo it back verbatim). ' +
  "Reply with ONLY a JSON array of these objects, nothing else.";
const GEMINI_MODEL = "gemini-2.5-flash";

// Classify one batch -> { arr: [{is_artist,name,category,i?}], fallback: bool }.
// Prefers Gemini with Google-Search grounding (free tier); when Gemini is absent
// OR errors (e.g. the daily grounded quota is exhausted — a deterministic 429
// every afternoon), falls back to Workers AI so junk never goes public
// unclassified. Fallback results are tagged so callers don't cache/stamp them.
async function classifyBatch(batch, env) {
  const user = batch.map((t, j) => `${j + 1}. ${t.replace(/\s+/g, " ").slice(0, 140)}`).join("\n");
  if (env.GEMINI_API_KEY) {
    try {
      const r = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${env.GEMINI_API_KEY}`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            systemInstruction: { parts: [{ text: CLS_SYS + " Use Google Search to verify each performer." }] },
            contents: [{ parts: [{ text: user }] }],
            tools: [{ google_search: {} }],
            // No "thinking" for a mechanical 8-line classification — it only
            // burns the free tier's token budget and latency.
            generationConfig: { temperature: 0, thinkingConfig: { thinkingBudget: 0 } },
          }) });
      const j = await r.json();
      if (r.ok) {
        const text = (j.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("");
        return { arr: JSON.parse((text.match(/\[[\s\S]*\]/) || ["[]"])[0]), fallback: false };
      }
      console.log("gemini err", r.status, JSON.stringify(j).slice(0, 200));
    } catch (e) { console.log("gemini fetch error", e); }
  }
  const r = await env.AI.run(AI_MODEL, {
    messages: [{ role: "system", content: CLS_SYS }, { role: "user", content: user }],
    max_tokens: 2400, temperature: 0,
  });
  const resp = r.response;
  let arr = [];
  if (Array.isArray(resp)) arr = resp;
  else if (resp && Array.isArray(resp.result)) arr = resp.result;
  else if (typeof resp === "string") arr = JSON.parse((resp.match(/\[[\s\S]*\]/) || ["[]"])[0]);
  return { arr, fallback: !!env.GEMINI_API_KEY };   // fallback only when Gemini SHOULD have run
}

async function classifyTitles(titles, env) {
  const out = {};
  const todo = [];
  for (const t of titles) {
    if (!t) continue;
    // Cache prefix must track scan.py's CLS_VERSION (v4 = stricter is_artist prompt).
    const cached = await env.SUBS.get("cls4:" + (await artistHash(t)), "json");
    if (cached) out[t] = cached;
    else if (!todo.includes(t)) todo.push(t);
  }
  for (let i = 0; i < todo.length; i += 8) {
    const batch = todo.slice(i, i + 8);
    let arr = [], fallback = false;
    try { ({ arr, fallback } = await classifyBatch(batch, env)); }
    catch (e) { console.log("classify error", e); }
    // Align results to inputs by the echoed line number "i" — positional
    // alignment poisons the permanent cache the moment the model skips or
    // merges one line (every later result lands on the wrong title).
    const byLine = new Array(batch.length).fill(null);
    const positional = [];
    for (const a of arr) {
      if (!a || typeof a !== "object") continue;
      const li = Number(a.i);
      if (Number.isInteger(li) && li >= 1 && li <= batch.length && !byLine[li - 1]) byLine[li - 1] = a;
      else positional.push(a);
    }
    // No usable line numbers at all -> trust position only when counts match.
    if (byLine.every((x) => x === null) && positional.length === batch.length)
      positional.forEach((a, j) => { byLine[j] = a; });
    for (let j = 0; j < batch.length; j++) {
      const a = byLine[j];
      if (!a) continue;                            // omitted -> caller retries later
      if (!CLS_CATS.includes(a.category)) continue; // unknown category -> retry, never coerce
      const c = {
        // Strict: only an affirmative true counts (the prompt says "when
        // unsure, use false" — a string "false" must not go public as true).
        is_artist: a.is_artist === true || a.is_artist === "true",
        name: (a.name || batch[j]).toString().trim() || batch[j],
        category: a.category,
      };
      if (fallback) {
        // Workers-AI (no web grounding) stopgap: usable now, but not cached and
        // flagged so the scan applies it without stamping cat_v — Gemini
        // re-does it properly once quota returns.
        out[batch[j]] = { ...c, f: true };
      } else {
        out[batch[j]] = c;
        await env.SUBS.put("cls4:" + (await artistHash(batch[j])), JSON.stringify(c));
      }
    }
  }
  return out;
}

// ---- update handling ---------------------------------------------------------
async function handleUpdate(update, env) {
  if (update.callback_query) return handleCallback(update.callback_query, env);
  const msg = update.message || update.edited_message;
  if (!msg) return;
  const chat = String(msg.chat.id);
  if (msg.web_app_data) return handleWebAppData(chat, msg.web_app_data.data, env);
  const text = (msg.text || "").trim();
  if (text) return handleCommand(chat, text, env);
}

async function ensureSub(env, chat) {
  const subs = await getSubs(env);
  if (!subs.subscribers[chat]) subs.subscribers[chat] = { follows: [] };
  return subs;
}

async function handleCommand(chat, text, env) {
  const [cmd, ...rest] = text.split(" ");
  const arg = rest.join(" ").trim();
  const low = cmd.toLowerCase();

  if (low === "/start") {
    if (arg.startsWith("f_")) return followByHash(chat, arg.slice(2), env);
    return send(env, chat,
      "\u{1F3B5} <b>ברוכים הבאים ל-Tuna Concerts!</b>\n\n" +
      "הבוט שולח לך התראה בטלגרם <b>ברגע שאמן שאתה עוקב אחריו מכריז על הופעה חדשה</b> " +
      "באתרי הכרטיסים בישראל (בארבי, זאפה/איוונטים, קומדי בר, קופת תל אביב ועוד).\n\n" +
      "\u{1F449} לחץ על הכפתור למטה, סמן את האמנים שמעניינים אותך ואשר — " +
      "ומאותו רגע תקבל פוש על כל הופעה חדשה שלהם. אפשר גם פשוט להקליד שם של אמן לחיפוש.",
      { reply_markup: webappKeyboard() });
  }
  if (low === "/clear" || low === "/unfollowall") {
    const subs = await ensureSub(env, chat);
    subs.subscribers[chat].follows = [];
    await saveSubs(env, subs);
    return send(env, chat, "\u{1F5D1} רשימת המעקב נוקתה. פתח את הרשימה כדי לבחור מחדש:",
      { reply_markup: webappKeyboard() });
  }
  if (low === "/help") {
    return send(env, chat,
      "\u{1F3A4} «פתח את רשימת האמנים» — לסמן/לבטל אמנים (מי שכבר עוקב מסומן)\n" +
      "\u{1F50E} הקלד שם של אמן לחיפוש מהיר\n\n" +
      "פקודות:\n" +
      "/following — האמנים שאתה עוקב אחריהם\n" +
      "/upcoming — ההופעות של האמנים שלך\n" +
      "/clear — איפוס רשימת המעקב",
      { reply_markup: webappKeyboard() });
  }
  if (low === "/following") {
    // Follows may be stored under merged-away (loser) keys — resolve through
    // the merge chain and show the override-corrected display names.
    const merges = await getMerges(env);
    const f = [...new Set(followsOf(await getSubs(env), chat).map((k) => mergeKey(k, merges)))];
    if (!f.length) return send(env, chat, "עוד לא בחרת אמנים.", { reply_markup: webappKeyboard() });
    const artists = (await fetchJSON("artists.json")) || {};
    const ov = (await env.SUBS.get("overrides", "json")) || {};
    const names = f.map((k) => ov[k]?.name || artists[k]?.display || k);
    return send(env, chat, "אתה עוקב אחרי:\n" + names.map((n) => `• ${esc(n)}`).join("\n"),
      { reply_markup: webappKeyboard() });
  }
  if (low === "/upcoming") {
    const merges = await getMerges(env);
    const f = [...new Set(followsOf(await getSubs(env), chat).map((k) => mergeKey(k, merges)))];
    if (!f.length) return send(env, chat, "עוד לא בחרת אמנים.", { reply_markup: webappKeyboard() });
    return send(env, chat, (await upcomingText(new Set(f))) || "אין כרגע הופעות קרובות לאמנים שלך.");
  }
  if (low === "/follow" || low === "/unfollow") {
    return resolveAndToggle(chat, arg, low === "/follow", env);
  }
  if (low === "/id" || low === "/admin") {
    if (!isAdmin(env, chat))                          // NOT the developer — id only, no admin
      return send(env, chat, `\u{1F194} chat_id: <code>${chat}</code>`);
    await env.SUBS.put("admin_chat", String(chat));   // daily-summary destination
    const v = new Date().toISOString().slice(0, 13).replace(/[-T]/g, "");
    return send(env, chat,
      `\u{1F194} chat_id: <code>${chat}</code>\n` +
      "✅ אתה רשום כמפתח — סיכום יומי (~22:00), פאנל ניהול ומרכז בקרה.",
      { reply_markup: { inline_keyboard: [
        [{ text: "\u{1F6E0} פאנל ניהול אמנים", web_app: { url: `${WEBAPP_URL}admin.html?v=${v}` } }],
        [{ text: "\u{1F4CA} מרכז בקרה — יוזרים", web_app: { url: `${WEBAPP_URL}dashboard.html?v=${v}` } }]] } });
  }
  // plain text -> instant search
  return searchAndReply(chat, text, env);
}

// The catalogue keeps AI-flagged non-artists (hidden in the Mini App) and the
// admin's KV overrides (rename/hide/merge). In-chat search must apply BOTH, or
// junk the Mini App hides is still reachable by typing its name.
async function visibleArtists(env) {
  const artists = await fetchJSON("artists.json");
  if (artists === null) return null;                          // outage ≠ empty catalogue
  const ov = (await env.SUBS.get("overrides", "json")) || {};
  const out = {};
  for (const [key, info] of Object.entries(artists)) {
    const o = ov[key] || {};
    if (o.merge_into) continue;                               // absorbed duplicate
    const isArtist = "is_artist" in o ? o.is_artist !== false : info.is_artist !== false;
    if (!isArtist) continue;                                  // flagged/hidden junk
    out[key] = o.name || o.category
      ? { ...info, display: o.name || info.display, category: o.category || info.category }
      : info;
  }
  return out;
}

async function searchAndReply(chat, query, env) {
  const artists = await visibleArtists(env);
  if (artists === null) return send(env, chat, DATA_DOWN);
  const q = normalize(query);
  if (!q) return send(env, chat, "הקלד שם של אמן לחיפוש.", { reply_markup: webappKeyboard() });
  const subs = await getSubs(env);
  const follows = new Set(subs.subscribers[chat]?.follows || []);
  const starts = [], has = [];
  for (const [key, info] of Object.entries(artists)) {
    const d = normalize(info.display);
    if (key.startsWith(q) || d.startsWith(q)) starts.push([key, info]);
    else if (key.includes(q) || d.includes(q)) has.push([key, info]);
  }
  const matches = [...starts, ...has].slice(0, SEARCH_LIMIT);
  if (!matches.length) return send(env, chat, `\u{1F50E} לא נמצאו אמנים עבור "${esc(query)}".`);
  const rows = [];
  for (const [key, info] of matches) {
    const mark = follows.has(key) ? "✅" : "⬜";
    rows.push([{ text: `${mark} ${info.display}`, callback_data: `t:${await artistHash(key)}` }]);
  }
  return send(env, chat, `\u{1F50E} תוצאות עבור "${esc(query)}" — לחץ כדי לעקוב/לבטל:`,
    { reply_markup: { inline_keyboard: rows } });
}

async function handleCallback(cq, env) {
  const chat = String(cq.message.chat.id);
  const data = cq.data || "";
  if (data.startsWith("t:")) {
    const hash = data.slice(2);
    // Resolve against the VISIBLE catalogue (overrides applied) so hidden junk
    // and merged-away keys can't be followed from stale search messages, and
    // the toast shows the corrected display name.
    const artists = await visibleArtists(env);
    if (artists === null)
      return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: DATA_DOWN });
    let key = null;
    for (const k of Object.keys(artists)) if ((await artistHash(k)) === hash) { key = k; break; }
    if (!key) return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "נסה שוב" });
    const subs = await ensureSub(env, chat);
    const f = subs.subscribers[chat].follows;
    const i = f.indexOf(key);
    let note, followed;
    if (i >= 0) { f.splice(i, 1); note = "הוסר"; followed = false; }
    else { f.push(key); note = "עוקב"; followed = true; }
    await saveSubs(env, subs);
    // Refresh the tapped row's ✅/⬜ so the message stays a live follow panel —
    // a stale mark makes users tap again and accidentally undo themselves.
    try {
      const kb = cq.message.reply_markup?.inline_keyboard;
      if (kb) {
        for (const row of kb) for (const btn of row) {
          if (btn.callback_data === data)
            btn.text = `${followed ? "✅" : "⬜"} ${btn.text.replace(/^[✅⬜]\s*/, "")}`;
        }
        await tg(env, "editMessageReplyMarkup", {
          chat_id: chat, message_id: cq.message.message_id,
          reply_markup: { inline_keyboard: kb },
        });
      }
    } catch (e) { console.log("markup refresh", e); }
    return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: `${note}: ${artists[key].display}` });
  }
  return tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
}

async function followByHash(chat, hash, env) {
  const artists = await visibleArtists(env);
  if (artists === null) return send(env, chat, DATA_DOWN);
  let key = null;
  for (const k of Object.keys(artists)) if ((await artistHash(k)) === hash) { key = k; break; }
  if (!key) return send(env, chat, "לא מצאתי את האמן — נסה שוב.");
  const subs = await ensureSub(env, chat);
  const f = subs.subscribers[chat].follows;
  if (!f.includes(key)) f.push(key);
  await saveSubs(env, subs);
  await send(env, chat, `✅ עוקב אחרי ${esc(artists[key].display)}`);
  const txt = await upcomingText(new Set(f));
  if (txt) await send(env, chat, txt);
}

async function resolveAndToggle(chat, arg, follow, env) {
  const artists = await visibleArtists(env);
  if (artists === null) return send(env, chat, DATA_DOWN);
  const target = normalize(arg);
  let key = null;
  for (const [k, info] of Object.entries(artists))
    if (k === target || normalize(info.display) === target) { key = k; break; }
  if (!key) for (const k of Object.keys(artists)) if (target && k.includes(target)) { key = k; break; }
  if (!key) return send(env, chat, `לא מצאתי: ${esc(arg)}`, { reply_markup: webappKeyboard() });
  const subs = await ensureSub(env, chat);
  const f = subs.subscribers[chat].follows;
  const i = f.indexOf(key);
  if (follow && i < 0) f.push(key);
  if (!follow && i >= 0) f.splice(i, 1);
  await saveSubs(env, subs);
  return send(env, chat, `${follow ? "✅ עוקב אחרי" : "⬜ הופסק מעקב אחרי"} ${esc(artists[key].display)}`);
}

async function handleWebAppData(chat, data, env) {
  let hashes;
  try { hashes = JSON.parse(data); } catch { return; }
  if (!Array.isArray(hashes)) return;
  const artists = await fetchJSON("artists.json");
  if (artists === null) return send(env, chat, DATA_DOWN);   // never replace-save on outage
  const byHash = await hashMap(artists);
  // The Mini App sends the COMPLETE desired set (pre-ticked + changes), so we
  // replace — unticking an artist removes them.
  const follows = [];
  for (const h of hashes) {
    const key = byHash.get(h);
    if (key && !follows.includes(key)) follows.push(key);
  }
  const subs = await ensureSub(env, chat);
  subs.subscribers[chat].follows = follows;
  await saveSubs(env, subs);
  const names = follows.map((k) => artists[k]?.display || k);
  await send(env, chat,
    follows.length ? `✅ עוקב אחרי ${follows.length} אמנים:\n` + names.map((n) => `• ${esc(n)}`).join("\n")
                   : "לא עוקב אחרי אף אמן כרגע.",
    { reply_markup: webappKeyboard() });
  if (follows.length) {
    const txt = await upcomingText(new Set(follows));
    await send(env, chat, txt ||
      "אין כרגע תאריכים זמינים לאמנים שבחרת (ייתכן שאזלו הכרטיסים) — תקבל פוש ברגע שייפתח תאריך חדש \u{1F514}");
  }
}

// ---- scanner -> push new shows ----------------------------------------------
// Per-chat loop with per-send isolation: one failed send (network hiccup, user
// blocked the bot) must never abort the rest of the broadcast — that used to
// 500 the whole /notify and silently skip every remaining follower. An artist
// opening many dates at once is grouped into ONE message per chat (no spam),
// and Telegram 429s are honored (retry_after) with a coarse global pace.
async function pushNewShows(shows, env) {
  if (!shows.length) return 0;
  const subs = await getSubs(env);
  const merges = await getMerges(env);
  let sent = 0, dirty = false;
  for (const [chat, sub] of Object.entries(subs.subscribers)) {
    if (sub.blocked) continue;                      // marked on a previous 403
    const follows = new Set((sub.follows || []).map((k) => mergeKey(k, merges)));
    const mine = shows.filter((s) => follows.has(s.artist_key));
    if (!mine.length) continue;
    const byArtist = new Map();
    for (const s of mine) {
      if (!byArtist.has(s.artist_key)) byArtist.set(s.artist_key, []);
      byArtist.get(s.artist_key).push(s);
    }
    for (const group of byArtist.values()) {
      const text = group.length === 1 ? formatShow(group[0]) : formatShowGroup(group);
      try {
        let r = await tgSend(env, chat, text);
        if (r?.error_code === 429) {
          await sleep(((r.parameters?.retry_after || 3) + 1) * 1000);
          r = await tgSend(env, chat, text);
        }
        if (r?.ok) { sent++; }
        else if (r?.error_code === 403 ||
                 /chat not found|deactivated/i.test(r?.description || "")) {
          sub.blocked = true; dirty = true;         // stop pushing to this chat
          break;
        } else if (r) console.log("push failed", chat, JSON.stringify(r).slice(0, 160));
      } catch (e) { console.log("push error", chat, e); }
      if (sent && sent % 25 === 0) await sleep(1100);   // ~Telegram broadcast pace
    }
  }
  if (dirty) await saveSubs(env, subs);
  return sent;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// send() variant that surfaces the raw Telegram response for error handling.
const tgSend = (env, chat, text) =>
  tg(env, "sendMessage", { chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true });

function formatShowGroup(group) {
  const g = [...group].sort((a, b) => (a.date_iso || "").localeCompare(b.date_iso || ""));
  const lines = [`\u{1F3B5} <b>${esc(g[0].artist)}</b> — ${g.length} תאריכים חדשים!`];
  for (const s of g.slice(0, 10))
    lines.push(`\u{1F4C5} ${esc(s.date_raw || "")} · ${esc(s.venue || "")}\n\u{1F517} ${esc(s.url || "")}`);
  if (g.length > 10) lines.push(`\u{2026}ועוד ${g.length - 10}`);
  return lines.join("\n");
}

