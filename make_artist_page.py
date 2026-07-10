#!/usr/bin/env python3
"""Generate docs/index.html — a searchable artist page that doubles as a
Telegram Mini App.

Inside Telegram (opened from the bot's inline button) it authenticates with
Telegram initData and asks the Worker for the user's LIVE follows, so it can:
  - pre-tick artists already followed (✅) and list them at the top,
  - let you tick/untick (untick = remove) and "clear all",
  - confirm to save the complete set (the Worker replaces the follow list).
In a normal browser it falls back to per-artist follow deep links.

Each row also shows the artist's upcoming-dates count and nearest date (from
shows.json). Hebrew artists first, Latin-named ones last. Regenerated each scan.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from bot import _artist_hash          # keep hashing identical to the bot/Worker
from core import storage

BOT_USERNAME = "Tunaconcerts_bot"
WORKER_URL = "https://concert-alerts-bot.tunaconcerts.workers.dev"
DOCS = Path(__file__).with_name("docs")

_SRC_LABEL = {"barby": "בארבי", "eventim": "איוונטים/זאפה", "grayclub": "גריי",
              "kupat": "קופת ת\"א", "comy": "קומי", "comedybar": "קומדי בר",
              "ticketmaster": "טיקטמאסטר"}


def _sort_key(a):
    c = a["n"][:1]
    latin = c.isascii() and c.isalpha()      # Latin-named artists go last
    return (1 if latin else 0, a["n"].lower())


def _upcoming(shows: dict) -> dict:
    """artist_key -> {"d": upcoming dates count, "x": nearest date "DD.MM"}."""
    today = date.today().isoformat()
    per: dict[str, dict] = {}
    for s in shows.values():
        if s.get("sold_out") or (s.get("date_iso") and s["date_iso"] < today):
            continue
        k = s.get("artist_key")
        if not k:
            continue
        e = per.setdefault(k, {"d": 0, "iso": None})
        e["d"] += 1
        iso = s.get("date_iso")
        if iso and (e["iso"] is None or iso < e["iso"]):
            e["iso"] = iso
    out = {}
    for k, e in per.items():
        x = f"{e['iso'][8:10]}.{e['iso'][5:7]}" if e["iso"] else ""
        out[k] = {"d": e["d"], "x": x}
    return out


def _fetch_overrides() -> dict:
    """Snapshot of the KV admin overrides, baked into the page as a FALLBACK:
    if the live /api/overrides fetch fails at load time, the page degrades to
    slightly-stale overrides instead of none (junk un-hiding itself)."""
    try:
        import requests
        r = requests.get(WORKER_URL + "/api/overrides", timeout=15)
        ov = r.json() if r.ok else {}
        return ov if isinstance(ov, dict) else {}
    except Exception as e:                     # build must not fail on a worker hiccup
        print(f"[page] overrides snapshot failed: {e}")
        return {}


def _page(artists: dict, shows: dict) -> str:
    # Manual name/category fixes are made ONLINE via the admin Mini App (open it with
    # /id in the bot) and stored in Cloudflare KV ("overrides", keyed by artist_key).
    # The page fetches /api/overrides at load and applies them live (rename / recat /
    # hide / merge) — see init() below; a build-time snapshot is baked in as fallback.
    standup_src = {"comy", "comedybar"}              # stand-up-only sources

    def cat_of(key, info):
        # The stored category (AI / admin / cleanup, stamped with cat_v) wins;
        # bare stand-up-source entries default to standup. Blindly forcing
        # standup here used to mislabel Beit-Lessin plays sold via COMY.
        if info.get("category"):
            return info["category"]
        return "standup" if standup_src.intersection(info.get("sources", [])) else "music"

    def keep(key, info):
        return info.get("is_artist", True) is not False

    up = _upcoming(shows)
    data = sorted(
        ({"n": info["display"],
          "h": _artist_hash(key),
          "k": key,                                  # for live KV-override matching
          "c": cat_of(key, info),
          "d": up.get(key, {}).get("d", 0),          # upcoming dates count
          "x": up.get(key, {}).get("x", ""),         # nearest date DD.MM
          "s": " · ".join(_SRC_LABEL.get(s, s) for s in info.get("sources", []))}
         for key, info in artists.items()
         if keep(key, info)),                       # drop AI-flagged (or overridden) non-artists
        key=_sort_key,
    )
    blob = json.dumps(data, ensure_ascii=False)
    ov_blob = json.dumps(_fetch_overrides(), ensure_ascii=False)
    return r"""<!doctype html>
<html dir="rtl" lang="he"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>הופעות — מעקב אמנים</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  color-scheme:dark;
  --bg:#0b1020; --panel:#131a2e; --panel2:#182136; --line:#232e4a;
  --text:#eef1f7; --dim:#93a0b8; --accent:#3b82f6; --accent2:#2563eb;
  --ok:#22c55e; --chipbg:#141d33; --headbg:rgba(11,16,32,.92);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
  background:var(--bg);color:var(--text)}
header{position:sticky;top:0;z-index:5;background:var(--headbg);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  padding:12px 14px 6px;border-bottom:1px solid var(--line)}
.row1{display:flex;align-items:center;justify-content:space-between;gap:10px}
h1{font-size:17px;margin:0 0 10px;font-weight:800;letter-spacing:.2px}
#clear{display:none;flex:none;background:#3a2030;color:#ffb4c4;border:1px solid #5a2a3a;
  border-radius:999px;padding:6px 12px;font-size:13px}
#q{width:100%;padding:11px 14px;font-size:16px;border-radius:14px;border:1px solid var(--line);
  background:var(--panel);color:var(--text);outline:none;transition:border-color .15s}
#q:focus{border-color:var(--accent)}
.chips{display:flex;gap:7px;margin:10px 0 4px;overflow-x:auto;scrollbar-width:none;
  -webkit-overflow-scrolling:touch;padding-bottom:4px}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:flex;align-items:center;gap:6px;padding:7px 13px;border-radius:999px;
  border:1px solid var(--line);background:var(--chipbg);color:#cdd5e4;font-size:14px;
  white-space:nowrap;transition:all .15s}
.chip small{opacity:.65;font-size:11px;font-weight:600}
.chip.on{background:var(--accent2);border-color:var(--accent2);color:#fff;font-weight:700}
.chip.on small{opacity:.85}
#count{color:var(--dim);font-size:12.5px;margin:4px 2px 6px}
ul{list-style:none;margin:0;padding:6px 10px 100px}
li{display:flex;align-items:center;gap:11px;padding:11px 10px;margin:4px 0;
  border:1px solid transparent;border-radius:14px;background:transparent}
li.sel{cursor:pointer}
li.on{background:var(--panel2);border-color:#28406e}
.check{font-size:19px;flex:none;width:24px;text-align:center}
.name{flex:1;min-width:0}
.name b{font-size:15.5px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name span{font-size:12px;color:var(--dim);display:block;margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.date{flex:none;text-align:center;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:5px 9px;min-width:52px}
.date b{display:block;font-size:13px;color:var(--text)}
.date span{display:block;font-size:10.5px;color:var(--dim);margin-top:1px}
a.follow{flex:none;background:var(--accent2);color:#fff;text-decoration:none;
  padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600}
.empty{color:var(--dim);text-align:center;padding:36px 16px;font-size:15px}
.note{color:var(--dim);text-align:center;padding:14px 16px;font-size:13px}
#bar{position:fixed;left:0;right:0;bottom:0;padding:12px 16px;background:var(--headbg);
  backdrop-filter:blur(10px);border-top:1px solid var(--line);display:none}
#go{width:100%;padding:14px;font-size:16px;font-weight:700;border:0;border-radius:14px;
  background:var(--accent2);color:#fff}
#loadwarn{display:none;margin:8px 0 2px;padding:9px 12px;border-radius:12px;font-size:13.5px;
  background:#3a2030;color:#ffb4c4;border:1px solid #5a2a3a}
#loadwarn a{color:#ffd7e0;font-weight:700}
li.sel:focus-visible,.chip:focus-visible,#clear:focus-visible,#go:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
</style></head><body>
<header>
  <div class="row1"><h1>🎵 בחר אמנים לעקוב אחריהם</h1><button id="clear">🗑 נקה הכל</button></div>
  <input id="q" placeholder="🔎 חיפוש אמן…" autocomplete="off">
  <div class="chips" id="chips" role="group" aria-label="סינון קטגוריה">
    <button class="chip on" data-c="all" aria-pressed="true">הכל</button>
    <button class="chip" data-c="music" aria-pressed="false">🎵 מוזיקה</button>
    <button class="chip" data-c="standup" aria-pressed="false">😂 סטנדאפ</button>
    <button class="chip" data-c="theater" aria-pressed="false">🎭 הצגות</button>
    <button class="chip" data-c="lecture" aria-pressed="false">🎓 הרצאות</button>
  </div>
  <div id="loadwarn">⚠️ טעינת המעקב נכשלה — <a href="#" id="retry">נסה שוב</a></div>
  <div id="count" aria-live="polite"></div>
</header>
<ul id="list"></ul>
<div id="bar"><button id="go"></button></div>
<script>
let A = __DATA__;
const OV_FALLBACK = __OVERRIDES__;   // build-time snapshot of the admin overrides
const BOT = "__BOT__";
const WORKER = "__WORKER__";
const tg = window.Telegram && window.Telegram.WebApp;
const inApp = !!(tg && tg.initData);   // inline-button launch provides initData
if (inApp) { tg.ready(); tg.expand(); }

// Follow the user's Telegram theme (light users used to get a jarring dark
// page inside light chrome); the dark palette stays as the fallback.
try{
  const p=(tg&&tg.themeParams)||{};
  if(p.bg_color){
    const r=document.documentElement.style;
    r.setProperty('--bg',p.bg_color); r.setProperty('--headbg',p.bg_color);
    r.setProperty('--panel',p.secondary_bg_color||p.bg_color);
    r.setProperty('--panel2',p.secondary_bg_color||p.bg_color);
    r.setProperty('--chipbg',p.secondary_bg_color||p.bg_color);
    r.setProperty('--text',p.text_color||'#eef1f7');
    r.setProperty('--dim',p.hint_color||'#93a0b8');
    if(p.button_color){r.setProperty('--accent',p.button_color);r.setProperty('--accent2',p.button_color);}
    r.setProperty('--line',p.section_separator_color||p.hint_color||'#232e4a');
    if(tg.setHeaderColor)tg.setHeaderColor(p.bg_color);
    if(tg.setBackgroundColor)tg.setBackgroundColor(p.bg_color);
  }
}catch(e){}

const list=document.getElementById('list'), q=document.getElementById('q'),
      count=document.getElementById('count'),
      bar=document.getElementById('bar'), go=document.getElementById('go'),
      clearBtn=document.getElementById('clear'),
      loadwarn=document.getElementById('loadwarn');
const selected=new Set();
let loaded=null;          // Set of server-side followed hashes; null = not loaded (GET failed)
let order=A;
let cat="all";
let lastLen=A.length;
const MAX_ROWS=800;

function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
// Search must survive geresh/apostrophe keyboard variants (ג'ירפות vs ג׳ירפות).
function nrm(s){return (s||'').toLowerCase()
  .replace(/[׳‘’`]/g,"'").replace(/[״“”]/g,'"')
  .replace(/[־–—]/g,'-');}
function sortBase(a,b){
  const la=/^[A-Za-z]/.test(a.n), lb=/^[A-Za-z]/.test(b.n);
  if(la!==lb) return la?1:-1;                    // Latin-named artists last
  return a.n.toLowerCase().localeCompare(b.n.toLowerCase(),'he');
}
function metaOf(a){
  const bits=[];
  bits.push(a.d>0 ? (a.d===1?'תאריך אחד':a.d+' תאריכים')
                  : 'אין תאריכים כרגע — נעדכן כשיוכרז 🔔');
  if(a.s) bits.push(a.s);
  return bits.join(' · ');
}
function rowHtml(a){
  const dateBox = a.x ? '<div class=date><b>'+esc(a.x)+'</b><span>הקרוב</span></div>' : '';
  const meta='<div class=name><b>'+esc(a.n)+'</b><span>'+esc(metaOf(a))+'</span></div>';
  if(inApp){
    const on=selected.has(a.h);
    return '<li class="sel'+(on?' on':'')+'" data-h="'+a.h+'" role="checkbox" tabindex="0" '+
      'aria-checked="'+on+'" aria-label="'+esc(a.n)+'"><span class=check>'+(on?'✅':'⬜')+'</span>'+meta+dateBox+'</li>';
  }
  return '<li>'+meta+dateBox+'<a class=follow target=_blank rel=noopener href="https://t.me/'+BOT+'?start=f_'+a.h+'">+ עקוב</a></li>';
}
function chipCounts(){
  const n={all:A.length,music:0,standup:0,theater:0,lecture:0};
  for(const a of A) if(n[a.c]!==undefined) n[a.c]++;
  document.querySelectorAll('.chip').forEach(b=>{
    const c=b.dataset.c, base=b.textContent.replace(/\s*\d+$/,'').trim();
    b.innerHTML=esc(base)+(n[c]?' <small>'+n[c]+'</small>':'');
  });
}
function updateCount(len){
  if(len!==undefined) lastLen=len;
  count.textContent = lastLen + ' אמנים' + (inApp&&selected.size?` · ${selected.size} במעקב`:'');
}
function render(f){
  f=nrm((f||'').trim());
  let m = cat==='all' ? order : order.filter(a=>a.c===cat);
  if(f) m = m.filter(a=>nrm(a.n).includes(f));
  updateCount(m.length);
  let html = m.length ? m.slice(0,MAX_ROWS).map(rowHtml).join('')
                      : '<div class=empty>לא נמצאו אמנים 🤷</div>';
  if(m.length>MAX_ROWS)
    html += '<div class=note>מוצגים '+MAX_ROWS+' מתוך '+m.length+' — חפשו כדי לצמצם</div>';
  list.innerHTML = html;
}
// The save button appears only when the selection actually DIFFERS from what's
// saved on the server. That kills two bugs at once: no baffling "שמור (ללא
// מעקב)" CTA for a brand-new user, and no save at all while the follows GET
// failed (a full-replace save then would wipe every existing follow).
function dirty(){
  if(!inApp||loaded===null) return false;
  if(selected.size!==loaded.size) return true;
  for(const h of selected) if(!loaded.has(h)) return true;
  return false;
}
function updateBtn(){
  if(!inApp) return;
  clearBtn.style.display = selected.size ? 'inline-block' : 'none';
  const d=dirty();
  const label = selected.size ? 'שמור שינויים ('+selected.size+' במעקב)' : 'הסר את כל המעקבים';
  if(tg.MainButton){
    if(d){ tg.MainButton.setText(label); tg.MainButton.show(); }
    else tg.MainButton.hide();
  } else { go.textContent=label; bar.style.display=d?'block':'none'; }
}
let busy=false;
async function submit(){
  if(!inApp||busy||loaded===null) return; busy=true;
  if(tg.MainButton) tg.MainButton.showProgress();
  try{
    const r=await fetch(WORKER+'/api/follows',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({initData:tg.initData,hashes:[...selected]})});
    if(r.ok){ tg.close(); return; }
  }catch(e){}
  busy=false;
  if(tg.MainButton) tg.MainButton.hideProgress();
  tg.showAlert ? tg.showAlert('שמירה נכשלה, נסו שוב') : alert('שמירה נכשלה');
}
function toggle(h,li){
  const on=!selected.has(h);
  if(on) selected.add(h); else selected.delete(h);
  li.classList.toggle('on',on);
  li.setAttribute('aria-checked',on);
  li.querySelector('.check').textContent = on?'✅':'⬜';
  updateCount(); updateBtn();
}
function applyOverrides(ov){
  A = A.filter(a=>{const o=ov[a.k]||{}; return o.is_artist!==false && !o.merge_into;})
       .map(a=>{const o=ov[a.k]||{}; return {...a, n:o.name||a.n, c:o.category||a.c};});
  A.sort(sortBase);                      // renames must land in their new spot
  order=A;
}
async function loadFollows(){
  try{
    const r=await fetch(WORKER+'/api/follows?initData='+encodeURIComponent(tg.initData),
                        {signal:AbortSignal.timeout(8000)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    if(!Array.isArray(j.hashes)) throw new Error('bad payload');
    loaded=new Set(j.hashes);
    selected.clear(); loaded.forEach(h=>selected.add(h));
    order=[...A].sort((a,b)=>(loaded.has(b.h)?1:0)-(loaded.has(a.h)?1:0)||sortBase(a,b));
    loadwarn.style.display='none';
  }catch(e){
    loaded=null;                         // no baseline -> saving stays disabled
    loadwarn.style.display='block';
  }
}
function init(){
  // Paint instantly from the inline data and wire every control BEFORE any
  // network round-trip — the old page was blank and dead until two serial
  // fetches settled.
  q.addEventListener('input',e=>render(e.target.value));
  document.getElementById('chips').addEventListener('click',e=>{
    const b=e.target.closest('.chip'); if(!b) return;
    cat=b.dataset.c;
    document.querySelectorAll('.chip').forEach(c=>{
      c.classList.toggle('on',c===b); c.setAttribute('aria-pressed',c===b);});
    render(q.value);
  });
  if(inApp){
    if(tg.MainButton) tg.MainButton.onClick(submit);
    go.addEventListener('click',submit);
    clearBtn.addEventListener('click',()=>{
      const doClear=()=>{selected.clear();render(q.value);updateBtn();};
      if(tg.showConfirm) tg.showConfirm('לבטל את הסימון של כל האמנים?',ok=>{if(ok)doClear();});
      else if(confirm('לבטל את הסימון של כל האמנים?')) doClear();
    });
    list.addEventListener('click',e=>{const li=e.target.closest('li.sel');if(li)toggle(li.dataset.h,li);});
    list.addEventListener('keydown',e=>{
      if(e.key!==' '&&e.key!=='Enter') return;
      const li=e.target.closest('li.sel'); if(!li) return;
      e.preventDefault(); toggle(li.dataset.h,li);
    });
    document.getElementById('retry').addEventListener('click',async e=>{
      e.preventDefault(); await loadFollows(); render(q.value); updateBtn();
    });
  }
  applyOverrides(OV_FALLBACK);           // baked snapshot; live fetch refines below
  chipCounts(); render(''); updateBtn();
  (async()=>{
    const jobs=[(async()=>{              // live overrides (fallback already applied)
      try{
        const r=await fetch(WORKER+'/api/overrides',{signal:AbortSignal.timeout(6000)});
        if(r.ok){ const j=await r.json(); if(j&&typeof j==='object') applyOverrides(j); }
      }catch(e){}
    })()];
    if(inApp) jobs.push(loadFollows());
    await Promise.all(jobs);
    if(loaded)                           // overrides may have rebuilt `order` meanwhile
      order=[...A].sort((a,b)=>(loaded.has(b.h)?1:0)-(loaded.has(a.h)?1:0)||sortBase(a,b));
    chipCounts(); render(q.value); updateBtn();
  })();
}
init();
</script></body></html>
""".replace("__DATA__", blob).replace("__OVERRIDES__", ov_blob) \
   .replace("__BOT__", BOT_USERNAME).replace("__WORKER__", WORKER_URL)


def main() -> None:
    artists = storage.load("artists.json", {})
    shows = storage.load("shows.json", {})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(_page(artists, shows), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    # Publish the data to GitHub Pages too (docs/data/) — the Worker falls back
    # to it when raw.githubusercontent rate-limits Cloudflare egress IPs.
    ddir = DOCS / "data"
    ddir.mkdir(exist_ok=True)
    for name, obj in (("artists.json", artists), ("shows.json", shows)):
        (ddir / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {DOCS/'index.html'} with {len(artists)} artists (+ docs/data copies)")


if __name__ == "__main__":
    main()
