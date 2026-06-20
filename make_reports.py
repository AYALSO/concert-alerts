#!/usr/bin/env python3
"""Generate an HTML report of every scraper's extracted shows, for manual review.

Runs each registered scraper live and writes reports/<source>.html plus a
reports/index.html. Open them in a browser to eyeball data quality (clean artist
name vs full title, dates, venues, links). Review-only; not used in production.

Run:  python make_reports.py
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

os.environ.setdefault("EXAMPLE_SCRAPER", "off")   # real sites only

import scrapers  # noqa: F401  (registers all scrapers)
from scrapers.base import all_scrapers

REPORTS = Path(__file__).with_name("reports")

_CSS = """body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:#111}
h1{margin:0 0 4px} .meta{color:#666;margin:0 0 16px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:6px 8px;text-align:right;font-size:14px;vertical-align:top}
th{background:#1f2937;color:#fff;position:sticky;top:0}
tr:nth-child(even){background:#f7f7f7}
td.t{color:#555;font-size:13px} a{color:#2563eb;text-decoration:none}"""


def render(source: str, shows: list) -> str:
    shows = sorted(shows, key=lambda s: (s.date_iso or "", s.artist))
    distinct = len({s.artist_key for s in shows})
    rows = []
    for i, s in enumerate(shows, 1):
        title = "" if (not s.title or s.title == s.artist) else html.escape(s.title)
        rows.append(
            f"<tr><td>{i}</td><td><b>{html.escape(s.artist)}</b></td>"
            f"<td class=t>{title}</td><td>{html.escape(s.date_raw)}</td>"
            f"<td>{html.escape(s.venue)}</td>"
            f"<td><a href='{html.escape(s.url)}' target=_blank>↗</a></td></tr>"
        )
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"<!doctype html><html dir=rtl lang=he><head><meta charset=utf-8>"
        f"<title>{html.escape(source)} — {len(shows)} shows</title><style>{_CSS}</style>"
        f"</head><body><h1>{html.escape(source)}</h1>"
        f"<p class=meta>{len(shows)} הופעות · {distinct} אמנים שונים · נוצר {now}</p>"
        f"<table><thead><tr><th>#</th><th>אמן</th><th>כותרת מלאה</th>"
        f"<th>תאריך</th><th>אולם</th><th>קישור</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    links = []
    for scr in all_scrapers():
        try:
            shows = scr.fetch()
        except Exception as e:
            print(f"[{scr.name}] ERROR: {e}")
            continue
        if not shows:
            print(f"[{scr.name}] 0 shows — skipped")
            continue
        out = REPORTS / f"{scr.name}.html"
        out.write_text(render(scr.name, shows), encoding="utf-8")
        distinct = len({s.artist_key for s in shows})
        print(f"[{scr.name}] {len(shows)} shows, {distinct} artists -> {out}")
        links.append((scr.name, len(shows), distinct))

    index = ["<!doctype html><html dir=rtl lang=he><head><meta charset=utf-8>",
             "<title>Scraper reports</title><style>body{font-family:Arial;margin:24px}"
             "li{margin:6px 0;font-size:16px}</style></head><body><h1>דוחות סקרייפרים</h1><ul>"]
    for name, n, d in links:
        index.append(f"<li><a href='{name}.html'>{name}</a> — {n} הופעות, {d} אמנים</li>")
    index.append("</ul></body></html>")
    (REPORTS / "index.html").write_text("\n".join(index), encoding="utf-8")
    print(f"\nOpen: {REPORTS / 'index.html'}")


if __name__ == "__main__":
    main()
