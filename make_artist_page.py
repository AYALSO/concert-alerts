#!/usr/bin/env python3
"""Generate docs/index.html — a searchable artist page that doubles as a
Telegram Mini App.

- Inside Telegram (opened from the bot's keyboard button): instant client-side
  search + multi-select with clear ✅/⬜ marks; tap again to remove; the native
  Telegram button confirms and sends all picks at once (sendData -> bot).
- In a normal browser: same search, each artist is a per-artist follow deep link.

Note: keyboard-button Mini Apps do NOT receive initData, so we detect "inside
Telegram" via tg.platform (not initData). Hebrew artists are listed first,
Latin-named ones last. Regenerated each scan. Public page (no secrets).
"""
from __future__ import annotations

import json
from pathlib import Path

from bot import _artist_hash          # keep hashing identical to the bot
from core import storage

BOT_USERNAME = "Tunaconcerts_bot"     # public bot username (deep-link target)
DOCS = Path(__file__).with_name("docs")

_SRC_LABEL = {"barby": "בארבי", "eventim": "איוונטים/זאפה", "grayclub": "גריי"}


def _sort_key(a):
    c = a["n"][:1]
    latin = c.isascii() and c.isalpha()      # Latin-named artists go last
    return (1 if latin else 0, a["n"].lower())


def _page(artists: dict) -> str:
    data = sorted(
        ({"n": info["display"],
          "h": _artist_hash(key),
          "s": " · ".join(_SRC_LABEL.get(s, s) for s in info.get("sources", []))}
         for key, info in artists.items()),
        key=_sort_key,
    )
    blob = json.dumps(data, ensure_ascii=False)
    return r"""<!doctype html>
<html dir="rtl" lang="he"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>הופעות — מעקב אמנים</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
  background:#0b1020;color:#e7e9ee}
header{position:sticky;top:0;background:#0b1020;padding:14px 16px 8px;
  box-shadow:0 2px 12px rgba(0,0,0,.4)}
h1{font-size:18px;margin:0 0 10px}
#q{width:100%;padding:12px 14px;font-size:17px;border-radius:12px;border:1px solid #2a3350;
  background:#141b30;color:#fff;outline:none}
#count{color:#8b93a7;font-size:13px;margin:8px 2px 0}
#hint{color:#8b93a7;font-size:13px;margin:2px 2px 0}
ul{list-style:none;margin:0;padding:8px 12px 96px}
li{display:flex;align-items:center;gap:12px;padding:12px 10px;border-bottom:1px solid #1c2438}
li.sel{cursor:pointer;-webkit-tap-highlight-color:transparent}
li.on{background:#16234a}
.check{font-size:20px;flex:none;width:24px;text-align:center}
.name{flex:1;min-width:0}
.name b{font-size:16px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name span{font-size:12px;color:#8b93a7}
a.follow{flex:none;background:#2563eb;color:#fff;text-decoration:none;
  padding:9px 16px;border-radius:999px;font-size:15px;font-weight:600}
.empty{color:#8b93a7;text-align:center;padding:30px}
#bar{position:fixed;left:0;right:0;bottom:0;padding:12px 16px;background:#0b1020;
  border-top:1px solid #1c2438;display:none}
#go{width:100%;padding:14px;font-size:16px;font-weight:700;border:0;border-radius:12px;
  background:#2563eb;color:#fff}
</style></head><body>
<header>
  <h1>🎵 בחר אמנים לעקוב אחריהם</h1>
  <input id="q" placeholder="🔎 חיפוש אמן…" autocomplete="off">
  <div id="count"></div>
  <div id="hint"></div>
</header>
<ul id="list"></ul>
<div id="bar"><button id="go"></button></div>
<script>
const A = __DATA__;
const BOT = "__BOT__";
const tg = window.Telegram && window.Telegram.WebApp;
// keyboard-button Mini Apps have no initData, so detect via platform.
const inApp = !!(tg && tg.platform && tg.platform !== "unknown");
if (inApp) { tg.ready(); tg.expand(); }

const list=document.getElementById('list'), q=document.getElementById('q'),
      count=document.getElementById('count'), hint=document.getElementById('hint'),
      bar=document.getElementById('bar'), go=document.getElementById('go');
const selected=new Set();
// artists already followed are passed in the URL (?f=hash.hash...) so we pre-tick them
const pre = new URLSearchParams(location.search).get("f");
if (pre) pre.split(".").forEach(h => { if (h) selected.add(h); });
hint.textContent = inApp ? "סמנו/בטלו אמנים (מי שכבר עוקב מסומן) ואז אשרו למטה"
                         : "לחצו עקוב ליד אמן (נפתח בטלגרם)";

function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function rowHtml(a){
  const meta='<div class=name><b>'+esc(a.n)+'</b><span>'+esc(a.s)+'</span></div>';
  if(inApp){
    const on=selected.has(a.h);
    return '<li class="sel'+(on?' on':'')+'" data-h="'+a.h+'"><span class=check>'+(on?'✅':'⬜')+'</span>'+meta+'</li>';
  }
  return '<li><a class=follow target=_blank rel=noopener href="https://t.me/'+BOT+'?start=f_'+a.h+'">+ עקוב</a>'+meta+'</li>';
}
function render(f){
  f=(f||'').trim().toLowerCase();
  const m = f ? A.filter(a=>a.n.toLowerCase().includes(f)) : A;
  count.textContent = m.length + (f?' אמנים תואמים':' אמנים בקטלוג');
  list.innerHTML = m.length ? m.slice(0,800).map(rowHtml).join('')
                           : '<div class=empty>לא נמצאו אמנים</div>';
}
function updateBtn(){
  if(!inApp) return;
  const label = 'עקוב אחרי '+selected.size+' אמנים';
  if(tg.MainButton){
    if(selected.size){ tg.MainButton.setText(label); tg.MainButton.show(); }
    else tg.MainButton.hide();
  } else { // fallback in-page button
    go.textContent=label; bar.style.display = selected.size? 'block':'none';
  }
}
function submit(){ if(selected.size) tg.sendData(JSON.stringify([...selected])); }
if(inApp){
  if(tg.MainButton) tg.MainButton.onClick(submit);
  go.addEventListener('click', submit);
  list.addEventListener('click', e=>{
    const li=e.target.closest('li.sel'); if(!li) return;
    const h=li.dataset.h;
    if(selected.has(h)){selected.delete(h);li.classList.remove('on');li.querySelector('.check').textContent='⬜';}
    else{selected.add(h);li.classList.add('on');li.querySelector('.check').textContent='✅';}
    updateBtn();
  });
}
q.addEventListener('input', e=>render(e.target.value));
render('');
updateBtn();   // show the confirm button if artists are already pre-selected
</script></body></html>
""".replace("__DATA__", blob).replace("__BOT__", BOT_USERNAME)


def main() -> None:
    artists = storage.load("artists.json", {})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(_page(artists), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {DOCS/'index.html'} with {len(artists)} artists")


if __name__ == "__main__":
    main()
