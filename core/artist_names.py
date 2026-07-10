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
    "ואורח", "ואורחת", "ואורחים",       # vav-conjunction defeats the plain prefix
    "בהשתתפות", "משתתפים", "מזמין", "מזמינה", "פוגש", "פוגשת", "בליווי",
    "feat", "Feat", "FEAT", "ft.",
    "חוגג", "השקת", "השקה", "סדרת", "לייב", "live", "Live", "LIVE",
    "סיבוב", "מארח", "מציג", "ערב", "חו״ל",
)
# Connector words that must not be left dangling at the end of a cut name
# ("ברי סחרוף עם ..." -> cut at אורח leaves "ברי סחרוף עם" -> drop the "עם").
_TRAILING = {"עם", "את", "של", "ו", "וגם", "וה"}
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
    while out and out[-1] in _TRAILING:      # no dangling "עם"/"את" after a cut
        out.pop()
    if out:                                  # keep original if cutting empties it
        s = " ".join(out)

    for phrase in _STRIP + tuple(extra_strip):
        s = re.sub(re.escape(phrase), " ", s)

    s = re.sub(r"\s+", " ", s).strip(" \t-–—|:!.,&" + _QUOTES)
    return s or re.sub(r"\s+", " ", (raw or "")).strip()


# Titles that are clearly NOT a performer: lecture-series pages, conferences,
# committee / employee-benefit events, expos, workshops, screenings, vouchers,
# quiz nights. Deterministic — these never reach the AI classifier (no quota
# spent) and never appear in the daily summary or the Mini App. Word-boundary
# matched, so e.g. "כנסיית השכל" (a band) is NOT hit by "כנס". Committee ("ועד")
# only counts at the START of the title — "מבאך ועד הביטלס" is a real show.
# NOTE: singular "הרצאה" is deliberately NOT here — "הרצאה של <שם>" may be a
# followable named lecturer; the AI classifies those (category "lecture").
_NON_ARTIST = re.compile(
    r"^ו?ועד\b"
    r"|\bהרצאות\b"
    r"|\bהטב(?:ה|ות)\b"
    r"|\bל?עובדי\b|\bל?ה?לקוחות\b|\bגמלאי\b|\bמנויי\b"   # corporate/customer-club events
    r"|^הופעות ל|^ארגון\b|^עמותת\b"
    r"|\bכנס\b|\bכינוס\b|\bועיד(?:ה|ת)\b|\bאקספו\b|\bוובינר\b"
    r"|\bסדנ(?:ה|ת|אות)\b"
    r"|\bהקרנ(?:ה|ת)\b"
    r"|שובר מתנה|מייל שירות|מכירה מוקדמת"
    r"|\bטריוויה\b|\bקריוקי\b|\bמונדיאל\b"
    r"|\bבמה פתוחה\b|\bמרתון סטנד ?אפ\b|\bפסטיבל\b",
)


def looks_non_artist(name: str) -> bool:
    """True when the title is self-evidently not an artist/band/play."""
    return bool(_NON_ARTIST.search(name or ""))
