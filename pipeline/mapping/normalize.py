"""mapping.normalize — deterministic title/label normalization for the mapping
layer. NO LLM, no fuzzy matching: this is the function that turns a printed
exhibit title into the `alias_norm` looked up in `table_registry_alias`, and a
printed row label into the `row_label_norm` anchor of `bank_line_map`.

Design rules (see docs/specs/MAPPING_LAYER.md §1.4):

  STRIPPED — noise that drifts quarter to quarter and must not fragment the key:
    * period clauses      'for the financial year ended 31 december 2025',
                          'as at 31 dec 2025', '— Year 2025, Year 2024',
                          '1Q25', '2nd Half 2025', 'FY25'
    * footnote markers    trailing digits glued to a word ('Earnings2')
    * unit parentheticals '($m)', '(%)', '(S$)'
    * continuation        '(continued)', "(cont'd)"
    * assurance           'audited', 'unaudited'
    * consolidation       'consolidated'  (a legal-entity qualifier — it is an
                          AXIS MEMBER on the fact, never part of the exhibit's
                          identity)
    * leading numbering   '10. Deposits and balances…'

  PRESERVED — anything that changes WHAT the exhibit is:
    * statement names, including near-misses that must not collapse:
      'income statement' vs 'statement of comprehensive income'
    * dimensional qualifiers: by_geography, by_business_segments, by_industry,
      by_currency, by_maturity, by_collateral_type, by_loan_grading.
      `performance_by_geography_selected_income_statement_items` is a DIFFERENT
      exhibit from `selected_income_statement_items` — it carries a geo axis.
      Merging them would stamp geography cuts as group totals, which is the
      silent wrong-tag this whole layer exists to prevent.

Footnote handling: when the geometry stage matched a table, `table_title_clean`
already has footnote superscripts removed TYPOGRAPHICALLY (superscript size +
baseline, no digit regex). Prefer it; the regex below is the fallback for
`hierarchy_source='model'` tables only.
"""
from __future__ import annotations

import re
import unicodedata

# --- period vocabulary -----------------------------------------------------
# Ordered longest-first so 'for the financial year ended <date>' is consumed
# before the bare-year rule can bite a fragment of it.
_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
          r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
          r"nov(?:ember)?|dec(?:ember)?")

_PERIOD_PATTERNS = [
    # clause forms first
    rf"for\s+the\s+(?:financial\s+)?(?:year|half[\s-]?year|period|quarter)\s+ended\s+\d{{1,2}}\s+(?:{_MONTH})\s+\d{{4}}",
    rf"for\s+the\s+(?:six|three|nine|twelve)\s+months\s+ended\s+\d{{1,2}}\s+(?:{_MONTH})\s+\d{{4}}",
    rf"as\s+at\s+\d{{1,2}}\s+(?:{_MONTH})\s+\d{{4}}",
    rf"\d{{1,2}}\s+(?:{_MONTH})\s+\d{{4}}",
    # compact tokens
    r"\b[1-4]q\s?\d{2}\b",
    r"\bfy\s?\d{2}\b",
    r"\b[12]h\s?\d{2}\b",
    r"\b9m\s?\d{2}\b",
    r"\b(?:1st|2nd|3rd|4th)\s+(?:half|qtr|quarter)\s+\d{4}\b",
    r"\b(?:1st|2nd|3rd|4th)\s+(?:half|qtr|quarter)\b",
    # SPELLED-OUT ordinals + month-count forms. OCBC's media releases title the
    # SAME income summary 'First Half 2025 Performance', 'Second Quarter 2025
    # Performance', 'Nine Months 2025 Performance', 'Full Year 2025
    # Performance', '9M25 Year-on-Year Performance'. Stripping only the YEAR
    # left the period WORD in the key, so one exhibit fragmented into eight
    # alias keys (first_half_performance, second_quarter_performance, …) and all
    # eight went UNCLASSIFIED — the exact quarter-to-quarter key drift this
    # module exists to prevent, just spelled out instead of numeric.
    # 'full year' must precede the bare 'year YYYY' rule so 'Full Year 2025'
    # is consumed whole rather than leaving a dangling 'full'.
    r"\b(?:full\s+)?year[\s-]on[\s-]year\b",
    r"\bfull\s+year(?:\s+\d{4})?\b",
    r"\b(?:first|second|third|fourth)\s+(?:half|qtr|quarter)(?:\s+\d{4})?\b",
    r"\b(?:three|six|nine|twelve)\s+months(?:\s+\d{4})?\b",
    r"\byear\s+\d{4}\b",
    r"\byearly\b",
    r"\b(?:19|20)\d{2}\b",
]
_PERIOD_RE = re.compile("|".join(_PERIOD_PATTERNS), re.I)

# Noise words removed wholesale. 'consolidated' is here on purpose: it is a
# legal-entity member, carried on the fact, not part of exhibit identity.
_NOISE_RE = re.compile(
    r"\((?:continued|cont'?d|unaudited|audited)\)|"
    r"\b(?:continued|cont'?d|unaudited|audited|consolidated)\b", re.I)

# Unit / currency parentheticals: ($m), ($'000), (%), (S$), ($)
_UNIT_RE = re.compile(r"\(\s*(?:s?\$[^)]*|%|[^)]*\bmillion\b[^)]*)\s*\)", re.I)

# Leading section numbering: '10. Deposits…', '2 Off-balance…', and the
# HIERARCHICAL forms these filings actually use for notes and Pillar-3 items:
# '13.2 Geographical segments', '12.3.1 …', 'A.3 Overview of key prudential
# regulatory metrics', 'A.6.1 IRBA RWA flow statement'.
#
# Why the multi-level and lettered forms matter as much as the flat one: note
# numbering is a POSITION IN A DOCUMENT, not part of the exhibit's identity, and
# it renumbers between quarters (a bank inserts one note and 13.2 becomes 14.2).
# Leaving it in the key fragments the registry exactly as the module docstring
# forbids — 'a title that drifts between quarters must not fragment the key' —
# so the SAME exhibit resolves in one quarter and goes UNCLASSIFIED in the next.
#
# Deliberately requires trailing whitespace, so compact period tokens that open
# a title ('4Q25 performance highlights', '1Q25 key financial indicators') are
# NOT eaten here — they are period noise and _PERIOD_RE's job, not numbering.
# The optional letter prefix only fires when followed by digits, so an
# abbreviation ('e.g. …') cannot match.
_LEAD_NUM_RE = re.compile(r"^\s*(?:[a-z]\.)?\d+(?:\.\d+)*[.)]?\s+")

# Footnote marker: digits glued to the end of a word ('Earnings2', 'ratios2,3').
# Only fires when the digits FOLLOW a letter, so '9M' and '4Q' survive (they are
# handled by the period rules above) and a standalone number is left alone.
_FOOTNOTE_RE = re.compile(r"(?<=[a-z])\d+(?:\s*,\s*\d+)*\b", re.I)

# TITLE-ONLY footnote forms where the marker is separated from the word:
#   'Credit costs (bps) 1/'              -> OCBC's 'n/' style
#   'Performance by Business Segment 1'  -> trailing bare marker
#   'PERFORMANCE BY BUSINESS SEGMENTS1 - Selected …' -> marker before a clause
# NEVER applied to row labels: 'ECL Stage 3 (SP)' and 'ECL Stage 1 and 2 (GP)'
# carry meaningful standalone digits and would be destroyed by this rule.
_TITLE_FOOTNOTE_RE = re.compile(
    r"\s\d+(?:\s*,\s*\d+)*/"          # 'bps) 1/'  — the slash disambiguates
    r"|\s\d+(?:\s*,\s*\d+)*\s*(?=[-–—]|$)",   # trailing, or before an em-dash clause
)

# Trailing separators left behind after a clause is cut out.
_DANGLING_RE = re.compile(r"[\s—–\-,;:/&]+$")

# ROW-LABEL footnote whose marker is detached from the word by a NON-LETTER:
#   'Net asset value ("NAV") per ordinary share ($) 7'  -> the 7 is a footnote
#   'Liquidity coverage ratios ("LCR") 4,8'             -> 4,8 are footnotes
# The lookbehind requires the preceding non-space character to NOT be a letter,
# which is exactly what protects the meaningful cases:
#   'ECL Stage 3 (SP)'      -- 3 follows 'Stage'  (letter) -> KEPT
#   'ECL Stage 1 and 2 (GP)'-- both follow letters         -> KEPT
#   'Tier 1'                -- 1 follows 'Tier'            -> KEPT
_LABEL_TRAILING_FOOTNOTE_RE = re.compile(r"(?<=[^a-z\s])\s+\d+(?:\s*,\s*\d+)*\s*$", re.I)


# Superscript/footnote glyphs. MUST be removed BEFORE NFKC normalization:
# NFKC maps '¹' -> '1', so folding first silently turns a footnote marker into
# an ordinary digit and the marker survives every later strip. Measured damage:
# "Shareholders' equity ¹" -> 'shareholders_equity_1' while the unmarked
# "Shareholders' equity" -> 'shareholders_equity', i.e. the SAME line in two
# quarters produced two different anchors — exactly the drift this layer exists
# to prevent.
_SUPERSCRIPT = "¹²³⁴⁵⁶⁷⁸⁹⁰ᵃᵇᶜᵈ*†‡"
_SUPERSCRIPT_RE = re.compile(f"[{re.escape(_SUPERSCRIPT)}]+(?:\\s*,\\s*[{re.escape(_SUPERSCRIPT)}]+)*")


def _fold(s: str) -> str:
    """Strip superscript footnote glyphs, then NFKC + curly punctuation + lower."""
    s = _SUPERSCRIPT_RE.sub("", s or "")
    s = unicodedata.normalize("NFKC", s)
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'),
                 ("”", '"'), ("–", "-"), ("—", "-"),
                 (" ", " ")):
        s = s.replace(a, b)
    return s.lower()


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def normalize_exhibit_title(title: str | None) -> str:
    """Printed exhibit title -> `alias_norm` for `table_registry_alias`.

    Deterministic and total: always returns a string (possibly ''). An empty
    result means the title carried no identifying content and the table MUST be
    queued as UNCLASSIFIED rather than matched to anything.
    """
    s = _fold(title)
    # A multi-line title is 'NAME\nFor the year ended …' — the clause after the
    # first newline is almost always the period; strip lines that reduce to
    # nothing but period text, keep the rest joined.
    parts = []
    for line in s.split("\n"):
        stripped = _PERIOD_RE.sub(" ", line).strip(" .,;:-—–")
        parts.append(line if stripped else "")
    s = " ".join(p for p in parts if p)

    s = _LEAD_NUM_RE.sub("", s)
    s = _UNIT_RE.sub(" ", s)
    for _ in range(3):                     # clauses can nest: 'audited … 2025'
        s = _PERIOD_RE.sub(" ", s)
    s = _NOISE_RE.sub(" ", s)
    s = _FOOTNOTE_RE.sub("", s)
    for _ in range(2):                     # 'Segment 1 - 2025' -> marker then clause
        s = _TITLE_FOOTNOTE_RE.sub(" ", s)
        s = _PERIOD_RE.sub(" ", s)
    s = _DANGLING_RE.sub("", s)
    return _slug(s)


def safe_clean(verbatim: str | None, clean: str | None) -> str:
    """Return the label to anchor on: `clean` when it is a plausible cleaning of
    `verbatim`, else `verbatim`.

    The geometry stage's `*_clean` labels are meant to be the verbatim label
    MINUS typographic footnote markers, so their alphanumeric content must be a
    SUBSEQUENCE of the verbatim's. Measured violation in
    `UOB_4Q25_condensed-financial-statements` (geometry branch):

        verbatim 'Total income'          -> clean 'Total income 1'
        verbatim 'Customer deposits'     -> clean 'Customer deposits 4 25'
        verbatim 'Total assets'          -> clean 'Total assets 5 72'
        verbatim "Shareholders' equity"  -> clean "Shareholders' equity 5"

    i.e. the cleaner ADDED tokens (value fragments bleeding in from the first
    numeric column). Anchoring on those would bake a VALUE into the map key and
    guarantee a miss next quarter. The guard is one-way and cheap: a clean label
    that only deletes characters is trusted; one that introduces new ones is
    discarded in favour of the verbatim. Same guard belongs anywhere `*_clean`
    is consumed as an identity, which is why it lives here and not in the caller.
    """
    v, c = (verbatim or ""), (clean or "")
    if not c:
        return v
    vs = [ch for ch in v.lower() if ch.isalnum()]
    cs = [ch for ch in c.lower() if ch.isalnum()]
    it = iter(vs)
    return c if all(ch in it for ch in cs) else v


def normalize_row_label(label: str | None) -> str:
    """Printed row label -> `row_label_norm` for `bank_line_map`.

    Deliberately gentler than the title normalizer: a row label's words ARE its
    identity. Only footnote markers, unit parentheticals and punctuation noise
    are removed. 'Of which: Net interest income' keeps 'of which', which is
    exactly what distinguishes the group NII line from the two book-level ones.
    """
    s = _fold(label)
    # BEFORE _UNIT_RE: 'share ($) 7' must still see the ')' to classify the 7 as
    # a footnote. Stripping the unit first would leave '...share   7', whose
    # preceding letter would protect the marker.
    s = _LABEL_TRAILING_FOOTNOTE_RE.sub("", s)
    s = _UNIT_RE.sub(" ", s)
    s = _FOOTNOTE_RE.sub("", s)
    s = _DANGLING_RE.sub("", s)
    return _slug(s)
