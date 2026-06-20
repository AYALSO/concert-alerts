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
      return Response.json({ alerts: n });
    }
    // Mini App API (authenticated by Telegram initData): GET current follows, POST to save.
    if (url.pathname === "/api/follows") return handleApi(request, url, env);
    // Title classifier for the scan (Workers AI, cached). Secret-protected.
    if (url.pathname === "/classify" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      if (b.secret !== env.NOTIFY_SECRET) return new Response("unauthorized", { status: 401 });
      return Response.json(await classifyTitles(b.titles || [], env));
    }
    return new Response("concert-alerts bot up");
  },
};

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
    const follows = followsOf(await getSubs(env), chat);
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
function webappKeyboard() {
  return { inline_keyboard: [[{ text: "\u{1F3A4} פתח את רשימת האמנים", web_app: { url: WEBAPP_URL } }]] };
}
const followsOf = (subs, chat) => subs.subscribers[chat]?.follows || [];

function formatShow(s) {
  const lines = [`\u{1F3B5} <b>${esc(s.artist)}</b> — הופעה חדשה!`];
  if (s.title && s.title !== s.artist) lines.push(esc(s.title));
  lines.push(`\u{1F4C5} ${esc(s.date_raw || "")}`, `\u{1F4CD} ${esc(s.venue || "")}`, `\u{1F517} ${s.url || ""}`);
  return lines.join("\n");
}
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

async function upcomingText(followSet) {
  const shows = await fetchJSON("shows.json");
  const today = new Date().toISOString().slice(0, 10);
  const mine = Object.values(shows).filter(
    (s) => followSet.has(s.artist_key) && (!s.date_iso || s.date_iso >= today));
  if (!mine.length) return null;
  mine.sort((a, b) => (a.date_iso || "").localeCompare(b.date_iso || ""));
  const lines = ["\u{1F3B6} <b>הופעות קרובות של האמנים שלך:</b>"];
  for (const s of mine) {
    const extra = s.title && s.title !== s.artist ? `\n  ${esc(s.title)}` : "";
    lines.push(`• <b>${esc(s.artist)}</b>${extra}\n  ${esc(s.date_raw)} · ${esc(s.venue)}\n  ${s.url}`);
  }
  return lines.join("\n");
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

async function classifyTitles(titles, env) {
  const out = {};
  const todo = [];
  for (const t of titles) {
    if (!t) continue;
    const cached = await env.SUBS.get("cls2:" + (await artistHash(t)), "json");
    if (cached) out[t] = cached;
    else if (!todo.includes(t)) todo.push(t);
  }
  for (let i = 0; i < todo.length; i += 12) {
    const batch = todo.slice(i, i + 12);
    const list = batch.map((t, j) => `${j + 1}. ${t.replace(/\s+/g, " ").slice(0, 140)}`).join("\n");
    let arr = [];
    try {
      const r = await env.AI.run(AI_MODEL, {
        messages: [{ role: "system", content: CLS_SYS }, { role: "user", content: list }],
        max_tokens: 2400, temperature: 0,
      });
      const resp = r.response;                       // may be a parsed array OR a string
      if (Array.isArray(resp)) arr = resp;
      else if (resp && Array.isArray(resp.result)) arr = resp.result;
      else if (typeof resp === "string") arr = JSON.parse((resp.match(/\[[\s\S]*\]/) || ["[]"])[0]);
    } catch (e) {
      console.log("classify error", e);
    }
    for (let j = 0; j < batch.length; j++) {
      const a = arr[j];
      if (a && typeof a === "object") {           // valid AI result -> cache it
        const c = {
          is_artist: a.is_artist !== false,
          name: (a.name || batch[j]).toString().trim() || batch[j],
          category: CLS_CATS.includes(a.category) ? a.category : "music",
        };
        out[batch[j]] = c;
        await env.SUBS.put("cls2:" + (await artistHash(batch[j])), JSON.stringify(c));
      } else {                                     // AI failed -> default, do NOT cache (retry later)
        out[batch[j]] = { is_artist: true, name: batch[j], category: "music" };
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
    await send(env, chat,
      "ברוך הבא! \u{1F3B6}\nכאן תקבל התראה על כל הופעה חדשה של האמנים שתבחר.",
      { reply_markup: { remove_keyboard: true } });   // clear any old reply keyboard
    return send(env, chat,
      "\u{1F447} פתח את רשימת האמנים — מי שכבר עוקב יופיע מסומן בראש, אפשר לסמן/לבטל ולנקות הכל.\n" +
      "אפשר גם להקליד שם של אמן לחיפוש מהיר.",
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
    const f = followsOf(await getSubs(env), chat);
    return send(env, chat,
      "\u{1F3A4} כפתור «פתח את רשימת האמנים» — לסמן/לבטל אמנים (מי שכבר עוקב מסומן)\n" +
      "\u{1F50E} הקלד שם של אמן לחיפוש מהיר\n/following — מי שאתה עוקב אחריו\n" +
      "/upcoming — הופעות קרובות שלך\n/follow <שם> · /unfollow <שם>",
      { reply_markup: await webappKeyboard(f) });
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
  let sent = 0;
  for (const s of shows) {
    for (const [chat, sub] of Object.entries(subs.subscribers)) {
      if ((sub.follows || []).includes(s.artist_key)) {
        await send(env, chat, formatShow(s));
        sent++;
      }
    }
  }
  return sent;
}
