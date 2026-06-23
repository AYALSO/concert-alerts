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
      const n = await pushNewShows(body.shows || [], env);
      await bumpDaily(body, env);               // accumulate for the ~22:00 daily summary
      return Response.json({ alerts: n });
    }
    // Mini App API (authenticated by Telegram initData): GET current follows, POST to save.
    if (url.pathname === "/api/follows") return handleApi(request, url, env);
    // Admin override store (artist name/category fixes): GET public, POST admin-only.
    if (url.pathname === "/api/overrides") return handleOverrides(request, env);
    // Full artist catalogue (for the admin panel; CORS-enabled passthrough of raw JSON).
    if (url.pathname === "/api/catalogue")
      return Response.json(await fetchJSON("artists.json"), { headers: CORS });
    // Developer control-center: active users + their follows (admin-only).
    if (url.pathname === "/api/users") return handleUsers(request, url, env);
    // Title classifier for the scan (Workers AI, cached). Secret-protected.
    if (url.pathname === "/classify" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      if (b.secret !== env.NOTIFY_SECRET) return new Response("unauthorized", { status: 401 });
      return Response.json(await classifyTitles(b.titles || [], env));
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

// Daily developer summary (replaces the per-scan report): counts accumulated by
// bumpDaily() on every /notify, sent ~22:00 then reset.
async function sendDailySummary(env) {
  const admin = env.ADMIN_CHAT_ID || (await env.SUBS.get("admin_chat"));
  if (!admin) return;
  const d = (await env.SUBS.get("daily", "json")) || { scans: 0, new_shows: 0, new_artists: 0 };
  const msg = "\u{1F4C5} <b>סיכום יומי</b>\n"
    + `\u{1F501} ${d.scans} סריקות\n`
    + `\u{1F195} ${d.new_shows} הופעות חדשות נוספו\n`
    + `\u{1F3A4} ${d.new_artists} אמנים חדשים נוספו`;
  try { await send(env, admin, msg); } catch (e) { console.log("daily summary", e); }
  await env.SUBS.put("daily", JSON.stringify({ scans: 0, new_shows: 0, new_artists: 0 }));
}

async function bumpDaily(body, env) {
  const d = (await env.SUBS.get("daily", "json")) || { scans: 0, new_shows: 0, new_artists: 0 };
  d.scans += 1;
  d.new_shows += (body.shows || []).length;
  d.new_artists += Number(body.new_artists) || 0;
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

async function fetchJSON(path) {
  const r = await fetch(`${RAW}/${path}`, { cf: { cacheTtl: 60, cacheEverything: true } });
  return r.ok ? r.json() : {};
}
const getSubs = (env) => env.SUBS.get("subscribers", "json").then((v) => v || { subscribers: {} });
const saveSubs = (env, s) => env.SUBS.put("subscribers", JSON.stringify(s));

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
  try { return String(JSON.parse(p.get("user")).id); } catch { return null; }
}
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
  const byHash = new Map();
  for (const k of Object.keys(artists)) byHash.set(await artistHash(k), k);
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
  const txt = await upcomingText(new Set(follows));
  if (txt) await send(env, chat, txt);
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
    const admin = env.ADMIN_CHAT_ID || (await env.SUBS.get("admin_chat"));
    if (!chat || !admin || String(chat) !== String(admin))
      return Response.json({ error: "forbidden" }, { status: 403, headers: CORS });
    const ov = b.overrides && typeof b.overrides === "object" ? b.overrides : {};
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
  const admin = env.ADMIN_CHAT_ID || (await env.SUBS.get("admin_chat"));
  if (!chat || !admin || String(chat) !== String(admin))
    return Response.json({ error: "forbidden" }, { status: 403, headers: CORS });
  const subs = (await getSubs(env)).subscribers || {};
  const artists = await fetchJSON("artists.json");
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
  lines.push(`\u{1F4C5} ${esc(s.date_raw || "")}`, `\u{1F4CD} ${esc(s.venue || "")}`, `\u{1F517} ${s.url || ""}`);
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
  const today = new Date().toISOString().slice(0, 10);
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
      return `\u{1F3A4} <b>${esc(a.artist)}</b> — ${label(urls[0])}\n${urls[0].url}`;
    const rows = urls.slice(0, PER_ARTIST_URL_CAP).map((u) => `• ${label(u)} — ${u.url}`);
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
const CLS_CATS = ["music", "standup", "theater"];
const CLS_SYS =
  "You classify Israeli live-event titles (mostly Hebrew) for a concert-alert app. " +
  "Use what you know about the actual performer/show. For EACH numbered title return " +
  "one JSON object with: " +
  '"is_artist" (true if it is a show by a specific named performer — a musician/singer/' +
  "band, a stand-up comedian, or a named theater/dance/ballet/children's act; false for " +
  'festivals, expos, conferences, parties, generic or venue events like "אקספו מכביה סיטי"), ' +
  '"name" (the real performer/show name ONLY, cleaned of tour/album/venue/extra words, ' +
  'in the original language), ' +
  '"category" exactly one of: "music" (concerts, singers, bands), "standup" (stand-up ' +
  'comedy), "theater" (plays, musicals, ballet, dance, and children\'s shows). ' +
  "Reply with ONLY a JSON array of these objects, in the same order, nothing else.";
const GEMINI_MODEL = "gemini-2.5-flash";

// Classify one batch -> array of {is_artist,name,category}. Prefers Gemini with
// Google-Search grounding (free tier) when GEMINI_API_KEY is set; else Workers AI.
async function classifyBatch(batch, env) {
  const user = batch.map((t, j) => `${j + 1}. ${t.replace(/\s+/g, " ").slice(0, 140)}`).join("\n");
  if (env.GEMINI_API_KEY) {
    const r = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${env.GEMINI_API_KEY}`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: CLS_SYS + " Use Google Search to verify each performer." }] },
          contents: [{ parts: [{ text: user }] }],
          tools: [{ google_search: {} }],
          generationConfig: { temperature: 0 },
        }) });
    const j = await r.json();
    if (!r.ok) { console.log("gemini err", JSON.stringify(j).slice(0, 200)); return []; }
    const text = (j.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("");
    return JSON.parse((text.match(/\[[\s\S]*\]/) || ["[]"])[0]);
  }
  const r = await env.AI.run(AI_MODEL, {
    messages: [{ role: "system", content: CLS_SYS }, { role: "user", content: user }],
    max_tokens: 2400, temperature: 0,
  });
  const resp = r.response;
  if (Array.isArray(resp)) return resp;
  if (resp && Array.isArray(resp.result)) return resp.result;
  if (typeof resp === "string") return JSON.parse((resp.match(/\[[\s\S]*\]/) || ["[]"])[0]);
  return [];
}

async function classifyTitles(titles, env) {
  const out = {};
  const todo = [];
  for (const t of titles) {
    if (!t) continue;
    const cached = await env.SUBS.get("cls3:" + (await artistHash(t)), "json");
    if (cached) out[t] = cached;
    else if (!todo.includes(t)) todo.push(t);
  }
  for (let i = 0; i < todo.length; i += 8) {
    const batch = todo.slice(i, i + 8);
    let arr = [];
    try { arr = await classifyBatch(batch, env); }
    catch (e) { console.log("classify error", e); }
    for (let j = 0; j < batch.length; j++) {
      const a = arr[j];
      if (a && typeof a === "object") {           // valid AI result -> cache it
        const c = {
          is_artist: a.is_artist !== false,
          name: (a.name || batch[j]).toString().trim() || batch[j],
          category: CLS_CATS.includes(a.category) ? a.category : "music",
        };
        out[batch[j]] = c;
        await env.SUBS.put("cls3:" + (await artistHash(batch[j])), JSON.stringify(c));
      }                                            // AI failed/omitted -> leave out (caller retries later)
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
    const f = followsOf(await getSubs(env), chat);
    if (!f.length) return send(env, chat, "עוד לא בחרת אמנים.", { reply_markup: await webappKeyboard([]) });
    const artists = await fetchJSON("artists.json");
    const names = f.map((k) => artists[k]?.display || k);
    return send(env, chat, "אתה עוקב אחרי:\n" + names.map((n) => `• ${esc(n)}`).join("\n"),
      { reply_markup: await webappKeyboard(f) });
  }
  if (low === "/upcoming") {
    const f = followsOf(await getSubs(env), chat);
    if (!f.length) return send(env, chat, "עוד לא בחרת אמנים.", { reply_markup: await webappKeyboard([]) });
    return send(env, chat, (await upcomingText(new Set(f))) || "אין כרגע הופעות קרובות לאמנים שלך.");
  }
  if (low === "/follow" || low === "/unfollow") {
    return resolveAndToggle(chat, arg, low === "/follow", env);
  }
  if (low === "/id" || low === "/admin") {
    const admin = env.ADMIN_CHAT_ID || (await env.SUBS.get("admin_chat"));
    if (admin && String(chat) !== String(admin))      // NOT the developer — id only, no admin
      return send(env, chat, `\u{1F194} chat_id: <code>${chat}</code>`);
    await env.SUBS.put("admin_chat", String(chat));   // the developer (or first-time bootstrap)
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

async function searchAndReply(chat, query, env) {
  const artists = await fetchJSON("artists.json");
  const q = normalize(query);
  if (!q) return send(env, chat, "הקלד שם של אמן לחיפוש.", { reply_markup: await webappKeyboard([]) });
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
    const artists = await fetchJSON("artists.json");
    let key = null;
    for (const k of Object.keys(artists)) if ((await artistHash(k)) === hash) { key = k; break; }
    if (!key) return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "נסה שוב" });
    const subs = await ensureSub(env, chat);
    const f = subs.subscribers[chat].follows;
    const i = f.indexOf(key);
    let note;
    if (i >= 0) { f.splice(i, 1); note = "הוסר"; }
    else { f.push(key); note = "עוקב"; }
    await saveSubs(env, subs);
    return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: `${note}: ${artists[key].display}` });
  }
  return tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
}

async function followByHash(chat, hash, env) {
  const artists = await fetchJSON("artists.json");
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
  const artists = await fetchJSON("artists.json");
  const target = normalize(arg);
  let key = null;
  for (const [k, info] of Object.entries(artists))
    if (k === target || normalize(info.display) === target) { key = k; break; }
  if (!key) for (const k of Object.keys(artists)) if (target && k.includes(target)) { key = k; break; }
  if (!key) return send(env, chat, `לא מצאתי: ${esc(arg)}`, { reply_markup: await webappKeyboard([]) });
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
  const byHash = new Map();
  for (const k of Object.keys(artists)) byHash.set(await artistHash(k), k);
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
    { reply_markup: await webappKeyboard(follows) });
  const txt = await upcomingText(new Set(follows));
  if (txt) await send(env, chat, txt);
}

// ---- scanner -> push new shows ----------------------------------------------
async function pushNewShows(shows, env) {
  if (!shows.length) return 0;
  const subs = await getSubs(env);
  const merges = await getMerges(env);
  let sent = 0;
  for (const s of shows) {
    for (const [chat, sub] of Object.entries(subs.subscribers)) {
      const follows = (sub.follows || []).map((k) => mergeKey(k, merges));
      if (follows.includes(s.artist_key)) {
        await send(env, chat, formatShow(s));
        sent++;
      }
    }
  }
  return sent;
}

