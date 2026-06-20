#!/usr/bin/env python3
"""Generate docs/index.html — a fast, searchable artist page for GitHub Pages.

Lists every artist in data/artists.json with instant in-browser (client-side)
search. Tapping an artist opens the Telegram bot through a deep link that
follows them (`?start=f_<hash>`, handled in bot.py). Regenerated each scan so
the catalogue stays current. The page is public (just concert info).
"""
from __future__ import annotations

import json
from pathlib import Path

from bot import _artist_hash          # keep hashing identical to the bot
from core import storage

BOT_USERNAME = "Tunaconcerts_bot"     # public bot username (deep-link target)
DOCS = Path(__file__).with_name("docs")

_SRC_LABEL = {"barby": "בארבי", "eventim": "איוונטים/זאפה", "grayclub": "גריי"}


def _page(artists: dict) -> str:
    data = sorted(
        ({"n": info["display"],
          "h": _artist_hash(key),
          "s": " · ".join(_SRC_LABEL.get(s, s) for s in info.get("sources", []))}
         for key, info in artists.items()),
        key=lambda a: a["n"].lower(),
    )
    blob = json.dumps(data, ensure_ascii=False)
    return """<!doctype html>
<html dir="rtl" lang="he"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>הופעות — מעקב אמנים</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
  background:#0b1020;color:#e7e9ee}
header{position:sticky;top:0;background:#0b1020;padding:16px 16px 8px;
  box-shadow:0 2px 12px rgba(0,0,0,.4)}
h1{font-size:20px;margin:0 0 10px}
#q{width:100%;padding:12px 14px;font-size:17px;border-radius:12px;border:1px solid #2a3350;
  background:#141b30;color:#fff;outline:none}
#count{color:#8b93a7;font-size:13px;margin:8px 2px 0}
ul{list-style:none;margin:0;padding:8px 12px 40px}
li{display:flex;align-items:center;gap:12px;padding:12px 10px;border-bottom:1px solid #1c2438}
.name{flex:1;min-width:0}
.name b{font-size:16px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name span{font-size:12px;color:#8b93a7}
a.follow{flex:none;background:#2563eb;color:#fff;text-decoration:none;
  padding:9px 16px;border-radius:999px;font-size:15px;font-weight:600}
a.follow:active{background:#1d4ed8}
.empty{color:#8b93a7;text-align:center;padding:30px}
</style></head><body>
<header>
  <h1>\U0001F3B5 בחר אמנים לעקוב אחריהם</h1>
  <input id="q" placeholder="\U0001F50E חיפוש אמן…" autocomplete="off" autofocus>
  <div id="count"></div>
</header>
<ul id="list"></ul>
<script>
const A = __DATA__;
const BOT = "__BOT__";
const list = document.getElementById('list');
const q = document.getElementById('q');
const count = document.getElementById('count');
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(f){
  f = (f||'').trim().toLowerCase();
  const m = f ? A.filter(a => a.n.toLowerCase().includes(f)) : A;
  count.textContent = m.length + ' אמנים' + (f ? ' תואמים' : ' בקטלוג');
  if(!m.length){ list.innerHTML = '<div class=empty>לא נמצאו אמנים</div>'; return; }
  list.innerHTML = m.slice(0,800).map(a =>
    '<li><a class=follow target=_blank rel=noopener href="https://t.me/'+BOT+'?start=f_'+a.h+'">+ עקוב</a>'
    + '<div class=name><b>'+esc(a.n)+'</b><span>'+esc(a.s)+'</span></div></li>').join('');
}
q.addEventListener('input', e => render(e.target.value));
render('');
</script></body></html>
""".replace("__DATA__", blob).replace("__BOT__", BOT_USERNAME)


def main() -> None:
    artists = storage.load("artists.json", {})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(_page(artists), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")   # serve as-is
    print(f"wrote {DOCS/'index.html'} with {len(artists)} artists")


if __name__ == "__main__":
    main()
