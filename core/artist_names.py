"""Extract a clean *artist* name from a messy *show* title.

Ticketing sites put the whole marketing title in the artist slot, e.g.
"טיפקס- מופע צהרים" or "ג'ירפות - חוגגים 20 שנה לאלבום 'גג'". For following and
grouping we want just "טיפקס" / "ג'ירפות". `clean_artist` strips the descriptive
tail with a few conservative rules, tuned and verified against live barby data.

It is intentionally cautious: when it can't find a confident artist (festivals,
tributes, "15 years of X" shows) it returns the tidied original rather than a
wrong guess. Scrapers that already get a clean artist from an API (e.g. zappa's
Eventim `attractions[].name`) should use that directly and skip this.
"""
from __future__ import annotations

import re

# From here on the text is show-description, not the artist. Matched per token
# by prefix, so "חוגג" also catches "חוגגת"/"חוגגים".
_CUT = (
    "מופע", "במופע", "הופעה", "בהופעה", "אורח", "אורחת", "אורחים",
    "חוגג", "השקת", "השקה", "סדרת", "לייב", "live", "Live", "LIVE",
    "סיבוב", "מארח", "ערב", "חו״ל",
)
# Venue / filler phrases dropped wherever they appear.
_STRIP = (
    "בבארבי", "בארבי", "והלהקה", "ולהקה",
    "לראשונה בישראל", "לראשונה", "בפעם הראשונה",
)
_QUOTES = "\"'`״׳“”‘’„"
_HE = "֐-׿"                       # Hebrew block
_ZW = "".join(chr(c) for c in (
    0xFEFF, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
    0x202D, 0x202E))

# First separator: | : , or a dash that touches a space or a Hebrew letter.
# (A dash between two Latin letters, e.g. "T-Puse", is left intact.)
_SEP = re.compile(r"[|:]|(?<=\s)[\-–—]|(?<=[%s])[\-–—]"
                  r"|[\-–—](?=[\s%s])" % (_HE, _HE))
# A quote only cuts when it STARTS a quoted phrase (preceded by a space); this
# leaves intra-word geresh (ג'ירפות, ג׳ימבו) and abbreviations (חו״ל) untouched.
_QCUT = re.compile(r"(?<=\s)[%s]" % re.escape(_QUOTES))


def clean_artist(raw: str, extra_strip: tuple[str, ...] = ()) -> str:
    s = (raw or "").translate({ord(c): None for c in _ZW})
    s = re.sub(r"\s+", " ", s).strip()

    # drop a leading anniversary prefix: "15 שנות אלון עדר" -> "אלון עדר"
    s = re.sub(r"^\d+\s+(?:שנות|שנים|שנה)\b\s*ל?\s*", "", s)

    m = _SEP.search(s)
    if m:
        s = s[:m.start()]
    m = _QCUT.search(s)
    if m:
        s = s[:m.start()]

    out: list[str] = []
    for tok in s.split():
        if any(tok.startswith(c) for c in _CUT):
            break
        out.append(tok)
    if out:                                  # keep original if cutting empties it
        s = " ".join(out)

    for phrase in _STRIP + tuple(extra_strip):
        s = re.sub(re.escape(phrase), " ", s)

    s = re.sub(r"\s+", " ", s).strip(" \t-–—|:!.,&" + _QUOTES)
    return s or re.sub(r"\s+", " ", (raw or "")).strip()
