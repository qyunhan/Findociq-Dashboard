"""findociq_app.py — FinDocIQ website-style app, v0.

Self-contained (does NOT import dashboard.py — importing it would run its own
page/tabs as a side effect). The ~30-line sqlite/bq backend switch below is
copied from dashboard.py lines 30-84, not shared, so this app and the
deployed dashboard stay independent.

Scope: the app shell (sidebar nav, cobalt theme) plus four built views —
Ingest (pick a source document, launch the pipeline, watch live per-stage
progress from `ingest_status`, then browse/download the result), Database
(browse the extracted schema), Table Registry (every extracted line item
with its concept attributes), and Dashboard (Key Financial Highlights +
Concept compare, reading `v_fact_metric_serving`, config in
`data/derived/dashboards/`).

Run locally:
    streamlit run findociq/app/findociq_app.py --server.headless true \
        --server.port 8599

Data source is chosen by env var FINDOCIQ_DB_SOURCE ("sqlite" default | "bq"),
same convention as dashboard.py.
"""
from __future__ import annotations

import csv
import re
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# DEPLOY-MIRROR PATCH (see README "Structure"): upstream lives at
# findociq/app/findociq_app.py and walks up two levels; here the app sits at the
# repo root, so both constants collapse to that root.
REPO = Path(__file__).resolve().parent               # repo root
FINDOCIQ_DIR = REPO
DB = FINDOCIQ_DIR / "db" / "compiled_v2.db"
# THE ONLY SOURCE for the Key Financial Highlights view. One CSV pair per bank:
#   *_anchors.csv        concept,row_order,bank,table_type_id,canonical_leaf_id,sign
#   *_formulaanchors.csv  ... + member_ordinal   (concept = SUM of members x sign)
DASHBOARDS_DIR = FINDOCIQ_DIR / "data" / "dashboards"   # DEPLOY-MIRROR PATCH: was data/derived/dashboards
SOURCE = os.environ.get("FINDOCIQ_DB_SOURCE", "sqlite").lower()
PROJECT = os.environ.get("FINDOCIQ_BQ_PROJECT", "igc2026-team08-6311")

DATASET = os.environ.get("FINDOCIQ_BQ_DATASET", "findociq")


# Fixed categorical color per institution for the Dashboard compare chart
# (never re-cycled by a filter) -- verbatim from the deployed dashboard.py's
# validated CVD-safe palette, not reinvented here.
_BANK_COLOR = {"DBS": "#2a78d6", "OCBC": "#eb6834", "UOB": "#1baf7a"}

# Fact_metric's sentinel values for "whole bank, no segment/region cut" --
# compare_frame()'s base_only filter treats these (or NULL/blank) as the base
# slice.
_BASE_SEGMENT = "SEG_TOTAL"
_BASE_GEO = "GLOBAL"

# ---------------------------------------------------------------- filter_by
# Spec 2026-08-09 §4.5 — how ONE fiscal period column claims its facts.
# A FLOW (income, ratio-over-a-window) belongs to the window it was measured
# over, so it matches on the full period LABEL: 1H26 income is 1H26 income and
# nothing else. A STOCK (balance sheet, point-in-time ratio) has no window at
# all -- it is a photograph taken on the period END DATE -- so it matches on
# that date alone and is equally true of every window closing there. Total
# assets at 2026-06-30 is the 1H26 figure AND the 2Q26 figure; the filing
# stamps it `as_at`, which under a span-partitioned axis matched neither.
#
# That mismatch is the whole of dashboard issue #2: the old basis radio made
# span a MUTUALLY EXCLUSIVE choice, so 'half' showed OCBC's income and blanked
# its entire balance-sheet block, 'as_at' did the exact reverse, and no setting
# could show a complete grid. Dispatching per anchor instead of per view lets
# both land in the same column.
FILTER_BY_PERIOD_LABEL = "period_label"
FILTER_BY_PERIOD_END_DATE = "period_end_date"
_FILTER_BY_VALUES = (FILTER_BY_PERIOD_LABEL, FILTER_BY_PERIOD_END_DATE)

# The default is derived from the anchor's `table_type_id` PREFIX, not from an
# enumerated list of ids: a balance sheet is a stock statement whatever variant
# a bank files it as (FS_BALANCE_SELECTED / _CONSOLIDATED / _STATUTORY, and the
# next bank's), so a new variant inherits the right rule with no edit here.
# Deliberately NOT a fact-level classifier -- filtering is anchor-driven, and
# nothing is written back to cell_fact.
_STOCK_TABLE_TYPE_PREFIXES = ("FS_BALANCE",)

# Flow spans, in the sense above: a span that names a WINDOW. Anything else
# ('as_at', NULL, blank) is a point-in-time stamp. Same vocabulary
# `period_label` already switches on, named once so the two cannot drift.
_FLOW_SPANS = frozenset({"1Q", "2Q", "3Q", "4Q", "1H", "2H", "9M", "FY"})

# Minimum share of a bank's anchor items a period column must carry to earn a
# place in the grid. A vintage that contributes two of twenty-six lines is a
# column of whitespace with a header on it -- it reads as "we have this period"
# when what we have is a fragment of it. 0.20 keeps any column carrying a real
# block (the 6-line ratios block of 26 = 0.23) and drops the stragglers.
MIN_COLUMN_DENSITY = 0.20


# ============================================================================
# Pure helpers — no `st` calls, so this module is import-safe without a
# Streamlit runtime (test_findociq_app.py imports these directly).
# ============================================================================


def _bank_of(name: str) -> str:
    """institution / source-file string -> 'DBS'/'OCBC'/'UOB'/'Other'.

    Matches both the ticker (present in Ingest's source-file keys, e.g.
    'financial_statements/OCBC_...') AND document.institution's full legal
    name (e.g. 'Oversea-Chinese Banking Corporation Ltd', 'United Overseas
    Bank Ltd') -- neither of those legal names contains its own ticker as a
    substring, so a ticker-only check misclassifies OCBC/UOB fact_metric
    rows as 'Other' and drops them from every bank-colored chart/table
    (discovered while building the Key Financial Highlights view, which
    depends on this to split fact_metric by bank)."""
    up = name.upper()
    if "OCBC" in up or "OVERSEA-CHINESE" in up or "OVERSEA CHINESE" in up:
        return "OCBC"
    if "UOB" in up or "UNITED OVERSEAS BANK" in up:
        return "UOB"
    if "DBS" in up:
        return "DBS"
    return "Other"


def period_label(period, period_span) -> str:
    """Canonical DISPLAY token for one (period, period_span) pair -- the one
    normalisation point for every period header shown anywhere in the app
    (banks print period columns every possible way: '1st Qtr 2026', '1Q25',
    '1st Half 2025¹', '2024 $m', '2H 2025 (1)', '2025'; the DB carries
    the machine truth as `period` (ISO end date) + `period_span`).

    Flow spans '1Q 2Q 3Q 4Q 1H 2H 9M FY' -> f"{span}{yy}" where yy is the
    last 2 digits of the period-end YEAR, e.g. ('2026-03-31','1Q') -> '1Q26',
    ('2025-12-31','FY') -> 'FY25'.

    'as_at', NULL/blank, or any unrecognised span -> the DATE form
    '31-Dec-25' (%d-%b-%y). This is deliberate, not a fallback of
    convenience: an as-at STOCK and an FY FLOW can share the same 31-Dec
    period-end date, and that exact collision is the reason `period_span`
    exists as a column at all -- so ('2025-12-31','as_at') and
    ('2025-12-31','FY') MUST render as different tokens ('31-Dec-25' vs
    'FY25'), never the same one.

    `period` missing/NaN/unparseable -> ''.
    """
    try:
        ts = pd.Timestamp(period)
    except (TypeError, ValueError):
        return ""
    if pd.isna(ts):
        return ""
    span = "" if pd.isna(period_span) else str(period_span).strip()
    yy = f"{ts.year % 100:02d}"
    if span in ("1Q", "2Q", "3Q", "4Q", "1H", "2H", "9M", "FY"):
        return f"{span}{yy}"
    return ts.strftime("%d-%b-%y")      # as_at / NULL / blank / unrecognised


def default_filter_by(table_type_id) -> str:
    """The `filter_by` an anchor gets when its CSV cell is blank (spec §4.5).

    Balance-sheet table types are STOCK statements -> `period_end_date`;
    everything else is a flow -> `period_label`. Matched on the
    `FS_BALANCE` prefix so an unseen variant is classified correctly without
    an edit here -- see `_STOCK_TABLE_TYPE_PREFIXES`.

    Pure. No DB, no `st`."""
    tt = (table_type_id or "").strip().upper()
    if tt.startswith(_STOCK_TABLE_TYPE_PREFIXES):
        return FILTER_BY_PERIOD_END_DATE
    return FILTER_BY_PERIOD_LABEL


def resolve_filter_by(declared, table_type_id) -> str:
    """DECLARED filter_by wins; blank/None/unrecognised falls back to the
    `table_type_id` default.

    Unrecognised is treated as blank on purpose. This value is authored by
    hand in a CSV, and a typo ('period_enddate') that silently became its own
    third mode would filter nothing and blank the row with no error anywhere.
    Falling back means the worst a typo can do is lose an intentional
    OVERRIDE, never the whole line -- and the override is exactly the case a
    reader is looking at when they check.

    Pure. No DB, no `st`."""
    d = (declared or "").strip()
    if isinstance(declared, float) and pd.isna(declared):
        d = ""
    return d if d in _FILTER_BY_VALUES else default_filter_by(table_type_id)


def is_flow_span(span) -> bool:
    """True for a span naming a WINDOW ('1H', 'FY', '2Q'); False for a
    point-in-time stamp ('as_at', NULL, blank, anything unrecognised)."""
    return _clean_span(span) in _FLOW_SPANS


def fiscal_period_axis(rows) -> list:
    """The dashboard's period axis: ordered [(label, period_end_date, span)].

    Built from FLOW rows only. A fiscal period is a WINDOW -- 1H26, FY25 --
    and stocks are placed onto it by `filter_by`, never allowed to mint a
    column of their own. Letting them mint one is what put '30-Jun-26' beside
    '1H26' as two separate columns describing the same close, each holding
    half the grid.

    Ordering is `period_axis_order`'s, so the axis and the rendered columns
    can never disagree about chronology.

    Pure. No DB, no `st`."""
    records = _as_records(rows)
    flows = [r for r in records if is_flow_span(_row_get(r, "period_span"))]
    if not flows:
        # STOCK-ONLY anchor set — the balance-sheet dashboards. The no-minting
        # rule above exists so a stock cannot raise '30-Jun-26' NEXT TO the
        # '1H26' flow column that closes on the same day, splitting one period
        # across two columns. With no flow rows at all there is no column to
        # duplicate and nothing to split: the stocks' own closes ARE the only
        # period axis the set has. Without this the axis came back empty, every
        # fact placed onto nothing, and a dashboard whose 304 facts all resolved
        # rendered as a grid with no columns — measured on
        # `breakdown_of_gross_nb_loans`, every member `as_at`.
        #
        # The labels are `period_label`'s date form ('30-Jun-26'), which is what
        # a balance column should say, and `filter_by='period_end_date'` places
        # each fact on the column closing on its own date.
        flows = records
    by_label: dict = {}
    for r in flows:
        span = _clean_span(_row_get(r, "period_span"))
        label = period_label(_row_get(r, "period"), span)
        if label:
            by_label.setdefault(label, (_row_get(r, "period"), span))
    return [(lb, by_label[lb][0], by_label[lb][1])
            for lb in period_axis_order(flows) if lb in by_label]


def period_column_headers(axis) -> dict:
    """{fiscal label -> display header carrying the column's EXACT close date},
    e.g. '1H26' -> '1H26 · 30-Jun-26'.

    WHY the date belongs in the header at all: a balance-sheet anchor is a
    STOCK. It is placed onto this axis by `filter_by='period_end_date'` (spec
    §4.5) — every column closing on the fact's own end date — so the number
    printed under '1H26' is the balance AS AT that column's close, not an
    average or a flow over the window. The fiscal label alone never says which
    date that is, and for loans and total assets that date IS the figure's
    meaning.

    Applied to the DISPLAY frame only. `period_order` and every grid key stay
    the bare fiscal label, so placement, density pruning and the item/period
    lookup are untouched — this renames columns at the last moment before
    rendering and nothing downstream reads a header string.

    A column whose end date is missing or unparseable keeps its bare label
    rather than rendering a half-empty header.

    Pure. No DB, no `st`."""
    out: dict = {}
    for entry in axis or []:
        label, end_date = entry[0], entry[1]
        try:
            ts = pd.Timestamp(end_date)
        except (TypeError, ValueError):
            continue
        if pd.isna(ts) or not label:
            continue
        out[label] = f"{label} · {ts.strftime('%d-%b-%y')}"
    return out


def target_period_labels(period, period_span, filter_by, axis) -> list:
    """Which fiscal columns one fact lands in, per spec §4.5.

    `period_label`    -> the single column whose (end date, span) is the
                         fact's own. A flow measured over 1H26 is 1H26 data.
    `period_end_date` -> EVERY column closing on the fact's end date. A stock
                         at 2026-06-30 is the closing figure for 1H26 and for
                         2Q26 alike; repeating it is correct, not duplication.
                         It is also why the fan-out is a list and not a value.

    `axis` is `fiscal_period_axis`'s [(label, end_date, span)]. A fact
    matching no column returns [] and simply does not render -- the grid never
    invents a column to hold an orphan.

    Pure. No DB, no `st`."""
    if not axis:
        return []
    if filter_by == FILTER_BY_PERIOD_END_DATE:
        key = _period_key(period)
        return [e[0] for e in axis if key and _period_key(e[1]) == key]
    own = period_label(period, period_span)
    return [e[0] for e in axis if e[0] == own]


def _period_key(period) -> str:
    """ISO date string for a period value, '' if unparseable. Compares two
    period stamps without caring whether either arrived as str or Timestamp."""
    try:
        ts = pd.Timestamp(period)
    except (TypeError, ValueError):
        return ""
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


# Ordered basis key -> (human label, frozenset of `period_span` values | None).
# None means "no filter" (today's mixed-span behaviour). Order here is also
# the tie-break order for `default_basis` and the display order offered to
# the user.
#
# NOTE on "quarter_cum" excluding "4Q": this basis is the INTRA-YEAR
# progression (1Q, 2Q, 3Q, 9M-cumulative-through-Q3) -- it deliberately stops
# short of year end. The natural year-end cumulative figure is FY, which
# lives in the separate "fy" basis; including 4Q here would put two
# different year-end aggregates (4Q discrete AND FY-via-9M-implied) on one
# axis and defeat the whole point of this feature (one period basis at a
# time). "quarter" (below) is the complementary rolling discrete-quarter
# series and keeps 4Q but excludes 9M for the same reason in reverse.
PERIOD_BASES = {
    "fy": ("Full year", frozenset({"FY"})),
    "half": ("Half-year", frozenset({"1H", "2H"})),
    "quarter_cum": ("Quarter + cumulative", frozenset({"1Q", "2Q", "3Q", "9M"})),
    "quarter": ("Quarter only", frozenset({"1Q", "2Q", "3Q", "4Q"})),
    "as_at": ("Point in time (as at)", frozenset({"as_at"})),
    "all": ("All periods (mixed spans)", None),
}


def _row_get(row, key):
    """dict-or-row-like field access, matching `compare_frame`'s `_get`."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, None)


def _clean_span(span) -> str:
    if span is None:
        return ""
    if isinstance(span, float) and pd.isna(span):
        return ""
    s = str(span).strip()
    return "" if s.lower() in ("nan", "none") else s


def _as_records(rows):
    """Normalise a DataFrame or list of dicts/row-likes to a list of dicts."""
    if isinstance(rows, pd.DataFrame):
        return rows.to_dict("records")
    return list(rows)


def available_bases(rows) -> list:
    """Which PERIOD_BASES keys actually have >=1 row in `rows`, in
    PERIOD_BASES order. "all" is included whenever `rows` is non-empty.

    Rows with a NULL/blank `period_span` match ONLY "all" -- they are
    periods the loader could not qualify, and folding them into a real
    basis would misrepresent them.
    """
    records = _as_records(rows)
    if not records:
        return []
    present = {_clean_span(_row_get(r, "period_span")) for r in records}
    present.discard("")
    out = [key for key, (_, spans) in PERIOD_BASES.items()
           if spans is not None and present & spans]
    out.append("all")
    return out


def filter_by_basis(rows, basis):
    """Subset `rows` to the (period_span) set for `basis`. "all" (or an
    unknown/None basis) returns `rows` unchanged. Accepts and returns a
    DataFrame if given one, else a list, so callers can filter either the
    tidy records used in tests or the DataFrames built on the Dashboard."""
    entry = PERIOD_BASES.get(basis)
    spans = entry[1] if entry is not None else None
    if spans is None:
        return rows
    if isinstance(rows, pd.DataFrame):
        return rows[rows["period_span"].apply(_clean_span).isin(spans)]
    return [r for r in rows if _clean_span(_row_get(r, "period_span")) in spans]


def _span_rank(span) -> int:
    s = _clean_span(span)
    if s in ("1Q", "2Q", "3Q", "4Q", "1H", "2H"):
        return 0
    if s == "9M":
        return 1
    if s == "FY":
        return 2
    return 3


def period_axis_order(rows, total_items: int | None = None,
                      min_density: float = MIN_COLUMN_DENSITY) -> list:
    """Chronological, de-duplicated x-axis category order built from
    `period_label`. Sort key is (period ISO date ASC, span_rank), where
    span_rank puts a DISCRETE span before a CUMULATIVE one sharing the same
    end date (e.g. 3Q25 before 9M25, 2H25 before FY25). Never sorts on the
    label string -- '31-Dec-25' and 'FY25' would scramble alphabetically.

    DENSITY RULE. With `total_items` given (the bank's full anchor item
    count), a period carrying fewer than `min_density` of them is dropped
    from the axis. A column holding 2 of 26 lines is not a period we have --
    it is a fragment of one, and rendering it as a full column overstates
    coverage to the only person who cannot check: the reader. Counted on
    DISTINCT item labels, not on rows, so a multi-member formula line cannot
    inflate its own period past the bar.

    `total_items=None` disables the rule entirely (every caller that just
    wants chronological order, e.g. `fiscal_period_axis`, passes nothing).
    Anchor ROWS are never touched -- this prunes columns only."""
    records = _as_records(rows)
    best = {}
    labels_seen: dict = {}
    for r in records:
        period = _row_get(r, "period")
        span = _row_get(r, "period_span")
        label = period_label(period, span)
        if not label:
            continue
        try:
            ts = pd.Timestamp(period)
        except (TypeError, ValueError):
            continue
        if pd.isna(ts):
            continue
        key = (ts, _span_rank(span))
        if label not in best or key < best[label]:
            best[label] = key
        item = _row_get(r, "label")
        if item is not None:
            labels_seen.setdefault(label, set()).add(item)
    ordered = [label for label, _ in sorted(best.items(), key=lambda kv: kv[1])]
    if not total_items:
        return ordered
    return [lb for lb in ordered
            if len(labels_seen.get(lb, ())) / total_items >= min_density]


def default_basis(rows, bases=None) -> str:
    """The basis with the most distinct periods in `rows`; ties fall back to
    PERIOD_BASES order (the order `bases` is iterated in).

    "all" is excluded from the contest itself -- it always has the most rows
    by construction (it is everything, unfiltered), so it would win every
    tie-free comparison and this rule would never pick a real basis. "all"
    is only returned when it's the sole candidate (rows with unqualified/
    NULL period_span, which match no real basis)."""
    candidates = bases if bases is not None else available_bases(rows)
    real = [k for k in candidates if k != "all"]
    if not real:
        return "all" if candidates else None
    best_key, best_count = None, -1
    for key in real:
        n = len(period_axis_order(filter_by_basis(rows, key)))
        if n > best_count:
            best_key, best_count = key, n
    return best_key


def compare_frame(rows, base_only: bool = True) -> pd.DataFrame:
    """Shape fact_metric-shaped rows for ONE concept into the Dashboard
    compare view's tidy records: (institution, period, period_span,
    value_num).

    `rows` is a list of dicts (or row-likes) with at least `institution`,
    `period`, `period_span`, `value_num`, `segment_key`, `geo_key`. When
    `base_only` (default), keeps only the whole-bank slice -- segment_key
    and geo_key that are NULL/blank or the pipeline's SEG_TOTAL/GLOBAL
    sentinels for "not segmented, not region-split" -- and drops rows
    carrying a real segment/geo cut (e.g. SEG_RETAIL, HK), so the headline
    number for a concept isn't summed across cuts. When `base_only=False`,
    every row for the concept is kept as-is.

    No `st` calls -- pure/testable without a Streamlit runtime.
    """
    def _get(row, key):
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            return getattr(row, key, None)

    out = []
    for row in rows:
        seg = _get(row, "segment_key")
        geo = _get(row, "geo_key")
        if base_only and not is_base_slice(seg, geo):
            continue
        out.append({
            "institution": _get(row, "institution"),
            "period": _get(row, "period"),
            "period_span": _get(row, "period_span"),
            "value_num": _get(row, "value_num"),
        })
    return pd.DataFrame(out, columns=["institution", "period", "period_span", "value_num"])


def is_base_slice(segment_key, geo_key) -> bool:
    """True when (segment_key, geo_key) is the whole-bank base slice -- the
    same NULL/blank-or-SEG_TOTAL/GLOBAL-sentinel convention `compare_frame`
    uses to drop segment/geo cuts so a concept's headline number isn't
    summed across cuts. Extracted out of `compare_frame` so
    `highlights_frame` (the Key Financial Highlights view) can reuse the
    exact same base-slice rule instead of reimplementing it.
    Pure/testable without Streamlit."""
    def _is_base(value, sentinel) -> bool:
        if value is None:
            return True
        if isinstance(value, float) and pd.isna(value):
            return True
        return str(value).strip() in ("", sentinel)

    return (_is_base(segment_key, _BASE_SEGMENT)
            and _is_base(geo_key, _BASE_GEO))


_HEADLINE_DASHBOARD = "highlights_dashboard"


def available_dashboards(dashboards_dir=None) -> list:
    """[(stem, display name)] for every anchor SET in the directory, in a stable
    order with the headline set first.

    A dashboard IS a CSV pair — `<stem>_anchors.csv` + `<stem>_formulaanchors.csv`
    — so adding one is dropping a pair in, with no code change. Discovered from
    the `_anchors.csv` file alone: a set may legitimately have no formula members.

    `highlights_dashboard` sorts first because it is the headline view; the rest
    follow alphabetically so the list does not reshuffle as pairs are added.

    Pure — no `st`, no DB."""
    d = Path(dashboards_dir or DASHBOARDS_DIR)
    stems = [p.name[: -len("_anchors.csv")] for p in d.glob("*_anchors.csv")
             if not p.name.endswith("_formulaanchors.csv")]
    stems.sort(key=lambda s: (s != _HEADLINE_DASHBOARD, s))
    return [(s, "Key Financial Highlights" if s == _HEADLINE_DASHBOARD
             else s.replace("_", " ").capitalize()) for s in stems]


def load_dashboard_anchors(bank: str, dashboards_dir=None, dashboard: str | None = None):
    """Read one bank's dashboard anchor CSVs -> (items, members).

    THE DASHBOARD'S ROW LIST IS NOW DATA, not a concept dictionary. Each anchor
    row says: this display line = this (table_type_id, canonical_leaf_id) in the
    stamped DB. The formula file adds rollups — a line that is the SUM of several
    leaves, signed — so `Net interest income = commercial book NII + markets NII`
    is DECLARED rather than inferred by a resolver tie-break.

    Returns:
      items   [{label, concept, unit_hint, section}] in `row_order` — the same
              shape `highlights_grid_frame` already consumes. `concept` is the
              anchor's own display name; there is no concept_key indirection.
              unit_hint is filled in later from the DB (see
              `anchor_highlights_frame`) so it does not have to be re-declared.

              `section` is DECLARED here, from the CSV's own column, because the
              grouping is a property of the ROW LIST and not of any one bank's
              data. Derived from the printed caption instead (see
              `attach_sections`) it was decided by whichever bank's row happened
              to reach the frame first: `Total equity` resolved only for OCBC,
              took OCBC's 'UNAUDITED BALANCE SHEETS' caption while its
              neighbours took DBS's 'Selected balance sheet items ($m)', and
              every bank's grid grew a spurious header row between
              `Total liabilities` and `Total equity`. A file with no `section`
              column still works — those items come back None and
              `attach_sections` fills them the old way.
      members {label: [(table_type_id, canonical_leaf_id, sign,
                        canonical_col_id, row_dim_key, filter_by)]}

              `filter_by` is spec §4.5's period-join mode, already resolved
              against the `table_type_id` default (see `resolve_filter_by`) —
              a CSV with no `filter_by` column at all loads unchanged and
              every member gets the default for its table type.

    Pure — no `st`, no DB.
    """
    d = Path(dashboards_dir or DASHBOARDS_DIR)
    order, members, sections = {}, {}, {}
    # ONE anchor SET is ONE dashboard, and sets are never merged.
    #
    # The glob used to take every `*_anchors.csv` in the directory at once,
    # which was right while the directory held exactly one pair. The moment a
    # second pair landed (`breakdown_of_gross_nb_loans_*`) that became a defect:
    # `row_order` is per-FILE and both files start at 1, so sorting the union by
    # it INTERLEAVES the two dashboards row by row — 'Gross loans', 'Net interest
    # income', 'Specific allowance', 'Net fee and commission income'... And since
    # `highlights_grid_frame` emits each section header at most once, the second
    # dashboard's rows then scatter through the first one's sections with no
    # header at all. Measured on the real files: 52 items, alternating from row 0.
    #
    # `dashboard` is the file stem before the suffix ('highlights_dashboard',
    # 'breakdown_of_gross_nb_loans'); None keeps the whole-directory behaviour
    # for callers that pre-date sets (tests, and any single-pair directory).
    # Selection by BANK is still the `bank` COLUMN filter below — that never had
    # to be in the filename, and per-bank files still match.
    for suffix in ("_anchors.csv", "_formulaanchors.csv"):
        pattern = f"{dashboard}{suffix}" if dashboard else f"*{suffix}"
        for path in sorted(d.glob(pattern)):
            with path.open(newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if (r.get("bank") or "").strip() != bank:
                        continue
                    label = (r["concept"] or "").strip()
                    if not label:
                        continue
                    order.setdefault(label, int(r["row_order"]))
                    if (r.get("section") or "").strip():
                        sections.setdefault(label, r["section"].strip())
                    members.setdefault(label, []).append((
                        (r["table_type_id"] or "").strip(),
                        (r["canonical_leaf_id"] or "").strip(),
                        int(r.get("sign") or 1),
                        # spec 2026-08-09 §7 — the address is a TUPLE.
                        # canonical_col_id blank = the period axis (every
                        # anchor authored before this column existed);
                        # row_dim_key blank = no dim slice. The anchor never
                        # says WHICH dim — that is fixed by table_type_id.
                        (r.get("canonical_col_id") or "").strip() or None,
                        (r.get("row_dim_key") or "").strip() or None,
                        # spec §4.5 — which period axis this member matches
                        # on. RESOLVED here, not stored raw, so every consumer
                        # sees a settled value and none of them has to know
                        # the default rule: a blank cell (or a file with no
                        # such column at all) becomes the table_type_id
                        # default rather than a None that each caller would
                        # interpret for itself.
                        resolve_filter_by(r.get("filter_by"),
                                          (r["table_type_id"] or "").strip())))
    items = [{"label": lb, "concept": lb, "unit_hint": None,
              "section": sections.get(lb)}
             for lb in sorted(order, key=lambda x: order[x])]
    return items, members


_ANCHOR_FRAME_COLS = ["bank", "institution", "label", "concept", "unit_hint",
                      "section", "period", "period_span", "value_num", "unit",
                      "resolved_by", "source_row_label", "is_derived"]


def _collapse_same_date_stocks(facts, filter_by):
    """One STOCK observation per (institution, slice, end date).

    A balance at a given date is filed under whatever span its column carried
    — DBS's `Total assets` at 2025-12-31 arrives three times, stamped `4Q`,
    `2H` and `FY`, and OCBC's arrives once as `as_at`. Under `period_end_date`
    every one of those matches every column closing on that date, so without
    this each column would receive the same balance once per span and SUM
    them: DBS 4Q25 total assets rendered 2,692,464 against a filed 897,488,
    exactly 3x. They are not three facts to add up, they are one fact
    recorded three times.

    A flow is left completely alone — 1H and FY income at the same year end
    are genuinely different measurements over different windows, and each
    lands in its own column anyway.

    Order-preserving, so the survivor is the first the query yielded (already
    vintage-resolved by `dedupe_by_latest_document`). Pure — no `st`, no DB.
    """
    if filter_by != FILTER_BY_PERIOD_END_DATE:
        return facts
    seen, out = set(), []
    for r in facts:
        k = (r.get("institution"), _period_key(r.get("period")),
             r.get("canonical_col_id"), r.get("geo_key"),
             r.get("segment_key"), r.get("industry_key"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def anchor_highlights_frame(rows, items, members, axis=None) -> pd.DataFrame:
    """Shape STAMPED rows into the long tidy frame the grid builders expect.

    `rows` are dicts from the anchor query (see `_ANCHOR_SQL`): one per
    (table_type_id, canonical_leaf_id, period, period_span) with value_num, unit,
    institution and table_title. No fact_metric, no concept_key, no
    bank_line_map — the join key is the leaf address the loader stamped.

    unit_hint and section are derived from the DATA, so neither has to be
    declared twice:
      * unit_hint = the unit on the row's own non-derived cells ('S$m', '%',
        'per_share'). Derived '% chg' columns are already excluded by the query,
        which is what makes this unambiguous.
      * section   = the printed caption of the table the leaf came from, so the
        grid's section headers read exactly as the filing prints them.

    A multi-member item is SUMMED with its signs and marked `is_derived`, which
    the view renders with the existing ' ᵈ' marker.

    `axis` is `fiscal_period_axis`'s [(label, end_date)]. Given it, every fact
    is placed onto that axis by its member's `filter_by` (spec §4.5) and the
    emitted `period`/`period_span` are the COLUMN's, not the fact's — so a
    balance-sheet figure stamped `as_at 2026-06-30` is emitted as 1H26 data
    and lands beside the income it belongs with. Composition happens after
    placement, keyed on the column, which is what lets a flow member and a
    stock member of one formula line meet at all.

    `axis=None` keeps the pre-§4.5 behaviour verbatim (one output row per
    fact, carrying the fact's own period and span) — every caller that has no
    axis yet, and every test written against the old shape.

    Pure — no `st`, no DB.
    """
    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault((r["table_type_id"], r["canonical_leaf_id"]), []).append(r)

    out = []
    for item in items:
        label = item["label"]
        mem = members.get(label) or []
        if not mem:
            continue
        composed: dict[tuple, dict] = {}
        for spec in mem:
            # (tt, leaf, sign) or (tt, leaf, sign, canonical_col_id,
            # row_dim_key). The short form is a member with no hard-axis slice
            # — identical in meaning to blank tuple fields, which is what §7
            # gives every anchor authored before the tuple existed. Tolerated
            # here so the member shape is not a breaking change for callers.
            tt, leaf, sign = spec[0], spec[1], spec[2]
            ccid = spec[3] if len(spec) > 3 else None
            rdk = spec[4] if len(spec) > 4 else None
            # §4.5. Absent from a short member tuple, so it is resolved from
            # the table type here too — a caller building members by hand
            # gets the same rule the CSV loader applies.
            fby = spec[5] if len(spec) > 5 else resolve_filter_by(None, tt)
            facts = []
            for r in by_key.get((tt, leaf), []):
                # Hard-axis slicing (spec §7). A declared column or row-dim key
                # narrows the fact set; blank means "don't slice on that axis",
                # which is every anchor authored before the tuple existed.
                if ccid and r.get("canonical_col_id") != ccid:
                    continue
                if rdk and rdk not in (r.get("geo_key"), r.get("segment_key"),
                                       r.get("industry_key")):
                    continue
                facts.append(r)
            facts = _collapse_same_date_stocks(facts, fby)
            for r in facts:
                # PLACEMENT (§4.5). Without an axis the fact keeps its own
                # period, which is the pre-§4.5 identity mapping. With one it
                # is placed on every column its filter_by claims -- exactly
                # one for a flow, one per window closing on that date for a
                # stock.
                if axis is None:
                    targets = [(r["period"], r["period_span"])]
                else:
                    hit = set(target_period_labels(
                        r["period"], r["period_span"], fby, axis))
                    targets = [(end, span) for lb, end, span in axis
                               if lb in hit]
                for tgt_period, tgt_span in targets:
                    k = (r["institution"], tgt_period, tgt_span)
                    slot = composed.setdefault(
                        k, {"total": 0.0, "unit": r.get("unit"),
                            "section": r.get("table_title"), "parts": []})
                    slot["total"] += sign * (r["value_num"] or 0.0)
                    slot["parts"].append(f"{'+' if sign > 0 else '-'} {leaf}")
        for (inst, period, span), slot in composed.items():
            out.append({
                "bank": _bank_of(inst or ""),
                "institution": inst,
                "label": label,
                "concept": item["concept"],
                "unit_hint": slot["unit"],
                "section": slot["section"],
                "period": period,
                "period_span": span,
                "value_num": slot["total"],
                "unit": slot["unit"],
                "resolved_by": "anchor" if len(mem) == 1 else "formula",
                "source_row_label": " ".join(slot["parts"]).lstrip("+ "),
                "is_derived": len(mem) > 1,
            })
    return pd.DataFrame(out, columns=_ANCHOR_FRAME_COLS)


# A printed caption carries a footnote marker that changes between vintages —
# DBS prints 'Key financial ratios (%)4' in one document and '(%)1,2' in another
# for the SAME table, which rendered as two separate sections. `table_title_clean`
# fixes this where the geometry stage ran; this strips the trailing marker for
# the documents where it did not. Display only — never touches identity.
_SECTION_FOOTNOTE = re.compile(r"[\s,]*\d+(?:\s*,\s*\d+)*\s*$")


def _section_title(title: str) -> str:
    return _SECTION_FOOTNOTE.sub("", str(title or "").strip()).strip() or str(title)


def _section_row_styles(header_flags):
    """Styler callback that bolds section-header rows, BY POSITION.

    Deliberately `axis=None` (whole-frame) rather than the obvious per-row
    `axis=1` with `flags.loc[row.name]`, which looks the row up by index LABEL.
    The grid's index is item labels, and a duplicated label makes `.loc` return
    a SERIES whose `bool()` raises "truth value of a Series is ambiguous" —
    escaping inside `Styler._compute()` and taking down the WHOLE PAGE.

    Uniqueness is guaranteed upstream by `highlights_grid_frame`, which emits a
    section header at most once; `Styler` rejects a non-unique index before any
    callback runs, so this alone is not a defence. It is the honest formulation
    regardless: the flags are positional facts about rows, and matching them by
    position says exactly that.
    """
    flags = list(header_flags)

    def _styles(df):
        return pd.DataFrame(
            [["font-weight: bold" if (i < len(flags) and flags[i]) else ""]
             * df.shape[1] for i in range(df.shape[0])],
            index=df.index, columns=df.columns)
    return _styles


def attach_sections(items, long_df):
    """FALLBACK: fill each item's `section` from the frame, in place, and return
    `items`.

    Only items that arrive with `section` unset are touched. The anchors CSVs
    now DECLARE the grouping (see `load_dashboard_anchors`), which is the only
    formulation that survives the cross-bank union: derived sections are read
    off whichever bank reached the frame first, so one bank's coverage gap
    silently regrouped every other bank's grid. This path remains for anchor
    files written before the `section` column existed.

    Sections derived here are the printed caption of the table each leaf came
    from, known only once the frame is built — but `highlights_grid_frame` reads
    them off `items` to place its bold header rows, so without either source
    every item keeps section=None and the grid renders a single `NaN` header.

    An item with no data keeps section None and is grouped under 'Unmapped', so a
    coverage gap is visible rather than silently mixed into the previous section
    (DBS 'Total equity' until FS_BALANCE_STATUTORY is authored)."""
    if long_df is None or long_df.empty:
        return items
    by_label = {}
    for r in long_df.to_dict("records"):
        if r.get("section"):
            by_label.setdefault(r["label"], _section_title(r["section"]))
    prev = None
    for it in items:
        if it.get("section"):
            prev = it["section"]
            continue
        sec = by_label.get(it["label"])
        # An item with no data INHERITS the previous item's section, so it renders
        # as a blank line inside the block it belongs to rather than splitting
        # that block with an 'Unmapped' header (DBS 'Total equity' sits between
        # Total liabilities and Shareholders' equity).
        it["section"] = sec or prev or "Unmapped"
        prev = it["section"]
    return items


# Every fact the highlights view can address, keyed by the identity the loader
# stamped. `col_role <> 'derived_skip'` drops the '% chg' columns declaratively.
_ANCHOR_SQL = """
-- table_type_id comes from row_dim, NOT table_t: one exhibit can carry rows
-- from several masterlist types, and table_t keeps only the last one to match,
-- which stranded correctly-stamped leaves under the losing type. The row-grain
-- column is stamped by the same masterlist entry that resolved the leaf, so the
-- two halves of the address can never disagree.
SELECT r.table_type_id, r.canonical_leaf_id,
       COALESCE(t.table_title_clean, t.table_title) AS table_title,
       d.institution,
       -- PERIOD IS READ FROM THE CELL, not re-derived from an axis. A hard-axis
       -- table puts its dimension on the columns and its period elsewhere --
       -- UOB's 'Performance by Geographical Segment' prints 'Singapore |
       -- Malaysia | ...' as column banners and takes its period from the table
       -- title. `c.col_period` is NULL on every such column, so keying off it
       -- returned nothing for the whole table type.
       -- The loader already ran the cascade (col -> row -> table_title -> doc)
       -- and recorded which rung answered in `period_source`, so the resolved
       -- value is a STAMP to be read -- not a COALESCE for the query layer to
       -- re-invent across two axes. Spec 2026-08-09 §1, second invariant.
       f.period      AS period,
       f.period_span AS period_span,
       c.canonical_col_id, r.geo_key, r.segment_key, r.industry_key,
       f.value_num, f.unit, d.doc_period
FROM cell_fact f
JOIN row_dim  r ON r.doc_id = f.doc_id AND r.table_id = f.table_id AND r.row_id = f.row_id
JOIN col_dim  c ON c.doc_id = f.doc_id AND c.table_id = f.table_id AND c.col_id = f.col_id
JOIN table_t  t ON t.doc_id = f.doc_id AND t.table_id = f.table_id
JOIN document d ON d.doc_id = f.doc_id
WHERE r.canonical_leaf_id IS NOT NULL
  AND r.table_type_id    IS NOT NULL
  -- ALLOWLIST, not a denylist. `col_role` is an enum (spec §7 adds
  -- 'unresolved' beside 'derived_skip'), and `<> 'derived_skip'` admits every
  -- value that is not that one -- so each new role would silently start
  -- serving. Naming what is ALLOWED means adding a role can never widen this.
  AND c.col_role IS NULL
  AND f.period IS NOT NULL
  AND f.value_num  IS NOT NULL
  -- CONSOLIDATION BASIS. A balance sheet prints the same line twice, once for
  -- the group and once for the bank alone: OCBC's 2Q26 'GROUP' and 'BANK'
  -- column banners each carry a 30 June 2026 child, so 'Total assets' arrives
  -- as both 729,887 and 477,550. Neither `dedupe_by_latest_document` (whose
  -- key has no entity, and whose doc_period tie-break ties) nor the anchor
  -- composition (which SUMS its members) can tell them apart — the group
  -- figure was surviving only because col_id 1 sorts before col_id 3. Feeding
  -- the same rows in reverse yielded the BANK number for a headline metric.
  -- The highlights dashboard is defined on the consolidated group, so it is
  -- stated here rather than left to row order.
  -- COALESCE, not `= 'CONSOLIDATED'`: a table with no entity banner leaves the
  -- column NULL, and schema_v7 defines that as consolidated (`legal_entity =
  -- COALESCE(col, 'CONSOLIDATED')`). Without it every single-entity table in
  -- the corpus — 1,444 of 1,609 columns — would drop out.
  AND COALESCE(c.legal_entity, 'CONSOLIDATED') = 'CONSOLIDATED'
"""


def dedupe_by_latest_document(rows):
    """One value per (institution, table_type_id, leaf, period, span).

    Vintages overlap — DBS 4Q25 and 2Q25 both print 1H25 — so without this the
    winner is whichever row the query happened to yield last. The most RECENT
    document wins: it carries the filing's latest restatement of that figure."""
    best: dict[tuple, dict] = {}
    for r in rows:
        # The hard-axis members are part of the KEY. Without them a geography
        # table's seven columns share one (institution, type, leaf, period,
        # span) and collapse to a single surviving slice -- Singapore's number
        # standing in for all seven.
        k = (r["institution"], r["table_type_id"], r["canonical_leaf_id"],
             r["period"], r["period_span"], r.get("canonical_col_id"),
             r.get("geo_key"), r.get("segment_key"), r.get("industry_key"))
        cur = best.get(k)
        if cur is None or (r.get("doc_period") or "") > (cur.get("doc_period") or ""):
            best[k] = r
    return list(best.values())


def format_highlight_value(value, unit_hint, is_derived: bool = False) -> str:
    """Display string for one Key Financial Highlights grid cell:
    thousands-separated, the value's OWN precision capped at 2 decimals,
    '' for None/NaN. Derived cells (resolved_by == 'formula') get a trailing
    ' ᵈ' marker -- see the per-bank "Derivations in this table" expander for
    how the value was computed (source_row_label). Pure/testable without
    Streamlit.

    ONE RULE FOR EVERY UNIT. `unit_hint` is accepted and deliberately not
    branched on. It used to select the precision, and that made the display
    only as trustworthy as the unit stamped on the row -- OCBC's per-share
    rows carry `S$m` and UOB's carry `%` (neither is a loader bug this layer
    may assume away), so EPS 0.81 hit the 0dp branch and rendered `1` while
    NAV 13.73 rendered `14`. A figure the filing prints to two decimals shown
    as an integer is not a rounded figure, it is a wrong one, and a formatter
    that can be wrong because a DIFFERENT column is wrong has the dependency
    in the wrong place.

    Capped at 2, not fixed at 2: `.2f` would invent a decimal the source never
    printed (3.7 -> '3.70'), and stripping alone would print float noise
    (0.1+0.2 -> '0.30000000000000004'). Round to 2, then strip what the value
    did not have."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    # Round FIRST (2dp cap), then strip trailing zeros so the value keeps its
    # own precision within that cap -- 3681.0 -> '3,681', 13.73 -> '13.73',
    # 3.7 -> '3.7', 1.234 -> '1.23'.
    s = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{s} ᵈ" if is_derived else s


def highlights_grid_frame(bank_rows, items, period_order) -> pd.DataFrame:
    """Build ONE bank's Key Financial Highlights display grid: item/section
    rows x period columns, from `highlights_frame`'s long-format rows
    already filtered to one bank (dicts with `label`, `period_label`,
    `value_num`, `unit_hint`, `is_derived`) plus the full `items` config
    (so item rows with no data for this bank still render, empty, exposing
    the coverage gap instead of hiding it) and `period_order` (chronological
    column order, e.g. from `period_label` + a period sort).

    A bold, value-less header row is inserted at every section boundary,
    in EXACTLY the order `items` defines (never alphabetized/reordered) --
    each header's label is the section name itself. The returned frame's
    extra leading column `_section_header` (bool) flags those rows so the
    Streamlit layer can bold them (e.g. via a pandas Styler) without
    re-deriving section boundaries itself.

    Cell values are pre-formatted strings from `format_highlight_value`
    (thousands separators / 2dp / the derived ' ᵈ' marker) -- this function
    only handles the row/column layout and section grouping.

    Pure/testable without Streamlit or a live DB."""
    cell_by_label: dict[str, dict[str, str]] = {}
    for r in bank_rows:
        label = r.get("label")
        period_label = r.get("period_label")
        cell_by_label.setdefault(label, {})[period_label] = format_highlight_value(
            r.get("value_num"), r.get("unit_hint"), bool(r.get("is_derived")))

    index: list[str] = []
    data: list[list[str]] = []
    section_header_flags: list[bool] = []
    prev_section = object()   # sentinel -- never equals a real section name
    # A section header is emitted at most ONCE. The item order comes from the
    # anchors' `row_order` while the section is DERIVED from the printed caption
    # of whichever table each leaf resolved to, so the list can leave a section
    # and come back to it — OCBC's balance-sheet block is interrupted the moment
    # `Total equity` resolves, because that leaf comes from a differently
    # captioned table. On the naive "emit whenever the section changes" rule
    # that repeats the header, and the frame's INDEX (item labels) stops being
    # unique.
    #
    # That is not cosmetic. `Styler.apply` refuses a non-unique index outright,
    # and the exception escapes inside `Styler._compute()` — so it takes down
    # the WHOLE PAGE rather than one table. A blank dashboard for all three
    # banks was this, and nothing else.
    seen_sections: set = set()
    for item in items:
        section = item.get("section")
        if section != prev_section and section not in seen_sections:
            index.append(section)
            data.append(["" for _ in period_order])
            section_header_flags.append(True)
            seen_sections.add(section)
        prev_section = section
        label = item["label"]
        row_cells = cell_by_label.get(label, {})
        index.append(label)
        data.append([row_cells.get(p, "") for p in period_order])
        section_header_flags.append(False)

    df = pd.DataFrame(data, index=index, columns=list(period_order))
    df.insert(0, "_section_header", section_header_flags)
    return df


def highlights_compare_grid_frame(long_df: pd.DataFrame, items, banks,
                                  period_label_choice: str) -> pd.DataFrame:
    """Build the 3-BANKS-SIDE-BY-SIDE Key Financial Highlights grid for ONE
    period: item/section rows x bank columns (the transpose of
    `highlights_grid_frame`'s item x period, one call per bank).

    `long_df` is `highlights_frame`'s long output with a `period_label`
    column already attached (same shape the per-bank loop uses). `banks` is
    the SHORT bank code order to use as columns (e.g. `_BANK_COLOR`'s keys,
    filtered to banks actually present).

    Same section-header convention as `highlights_grid_frame` (`_section_header`
    bool column, so the Streamlit layer can bold those rows via a pandas
    Styler without re-deriving section boundaries) plus a second flag,
    `_chartable` -- True only for real item rows that have a resolvable
    concept, so a click-to-chart interaction can skip section-header and
    coverage-gap (concept: null) rows rather than trying to chart them.

    Pure/testable without Streamlit or a live DB."""
    slice_df = long_df[long_df["period_label"] == period_label_choice]
    cell_by_label: dict[str, dict[str, str]] = {}
    for r in slice_df.to_dict("records"):
        cell_by_label.setdefault(r["label"], {})[r["bank"]] = format_highlight_value(
            r["value_num"], r["unit_hint"], bool(r["is_derived"]))

    index: list[str] = []
    data: list[list[str]] = []
    section_header_flags: list[bool] = []
    chartable_flags: list[bool] = []
    prev_section = object()   # sentinel -- never equals a real section name
    # ONCE per section — see the same guard in `highlights_grid_frame`. The item
    # list can leave a section and return to it, and a repeated header makes the
    # index non-unique, which `Styler.apply` rejects outright and which takes the
    # whole page down rather than one table.
    seen_sections: set = set()
    for item in items:
        section = item.get("section")
        if section != prev_section and section not in seen_sections:
            index.append(section)
            data.append(["" for _ in banks])
            section_header_flags.append(True)
            chartable_flags.append(False)
            seen_sections.add(section)
        prev_section = section
        label = item["label"]
        row_cells = cell_by_label.get(label, {})
        index.append(label)
        data.append([row_cells.get(b, "") for b in banks])
        section_header_flags.append(False)
        chartable_flags.append(item.get("concept") is not None)

    df = pd.DataFrame(data, index=index, columns=list(banks))
    df.insert(0, "_section_header", section_header_flags)
    df.insert(1, "_chartable", chartable_flags)
    return df


def pages_from_range(page_range, n_pages: int | None = None) -> list[int]:
    """table_t.page_range ('6', '3-5', '3,5-7') -> sorted unique 1-based page
    numbers. Tolerates None/blank/garbage (-> []) and inverted ranges; clamps
    to [1, n_pages] when n_pages is given. Pure/testable without Streamlit."""
    if page_range is None:
        return []
    pages: set[int] = set()
    for part in str(page_range).split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        try:
            start = int(lo)
            end = int(hi) if hi.strip() else start
        except ValueError:
            continue
        if end < start:
            start, end = end, start
        pages.update(range(start, end + 1))
    out = sorted(p for p in pages if p >= 1)
    if n_pages is not None:
        out = [p for p in out if p <= n_pages]
    return out


def raw_table_frame(row_records, col_records, cell_records,
                    indent: str = "    ",
                    drop_empty_cols: bool = True) -> pd.DataFrame:
    """Reconstruct ONE table in its ORIGINAL PDF shape from schema_v7 records.

    - row_records: dicts with row_id, row_hierarchy (indent depth 0/1/2…),
      row_leaf_label and (optional) row_leaf_label_clean — PDF row order =
      row_id order. FOOTNOTE MARKERS ARE DROPPED from the displayed label
      whenever row_leaf_label_clean is present ('Return on equity4, 5' shows
      as 'Return on equity'); rows with no clean form fall back to the
      verbatim label. See `resolve_title` — same rule, same reason.

      Note the deliberate asymmetry with the rest of this reconstruction:
      every OTHER aspect of the frame is byte-faithful to the printed page
      (row/column order, indent depth, '5,559' / '(7)' / 'NM' cell text,
      duplicate headers kept apart), because this view sits beside the PDF
      panel for eyeball verification. Labels are the exception, by request:
      the footnote numbering is renumbered every quarter and reading it in a
      browsing view is noise. The VERBATIM label is still one click away in
      the row-identity expander, and row_dim itself is never mutated.

      Only the geometry stage can tell a footnote marker from real digits
      (it decides typographically — superscript size + baseline, no digit
      regex), so tables whose hierarchy came from model levels keep their
      markers until they are re-loaded.
    - col_records: dicts with col_id, col_leaf_label — PDF column order =
      col_id order. Duplicate header labels (e.g. two '% chg' columns) are
      kept as separate columns; repeats get zero-width-space suffixes so the
      frame is Arrow-serializable while displaying identically.
    - cell_records: dicts with row_id, col_id, value_raw (original cell text,
      preferred — keeps '5,559', '(7)', 'NM') and value_num (fallback).

    First column '' holds the row label, indented per hierarchy depth with
    non-breaking spaces. Cells with no fact stay ''. Pure/testable without
    a Streamlit runtime.

    drop_empty_cols (default True): col_dim rows that NO cell references
    (loader artifacts — e.g. phantom col_id 100+ duplicates) are dropped,
    since a column with zero cells cannot exist in the PDF render. Pass
    False to see the table exactly as the DB defines it.
    """
    def _missing(v) -> bool:
        # SQL NULL arrives as None via sqlite3 but as float('nan') via a
        # pandas query path — both mean "no value", never the string 'nan'.
        return v is None or (isinstance(v, float) and pd.isna(v))

    cols = sorted(col_records, key=lambda c: c["col_id"])
    if drop_empty_cols and cell_records:
        used = {c["col_id"] for c in cell_records}
        cols = [c for c in cols if c["col_id"] in used]
    cell: dict[tuple, str] = {}
    for c in cell_records:
        v = c.get("value_raw")
        if _missing(v) or str(v).strip() == "":
            v = c.get("value_num")
            if _missing(v):
                v = None
            elif isinstance(v, float) and v == int(v):
                v = int(v)               # avoid '7.0' artifacts in a text grid
        cell[(c["row_id"], c["col_id"])] = "" if v is None else str(v)

    labels, data = [], []
    for r in sorted(row_records, key=lambda r: r["row_id"]):
        try:
            depth = int(r.get("row_hierarchy") or 0)
        except (TypeError, ValueError):
            depth = 0
        clean = r.get("row_leaf_label_clean")
        if _missing(clean) or not str(clean).strip():
            clean = r.get("row_leaf_label")
        labels.append(indent * depth + str(clean or ""))
        data.append([cell.get((r["row_id"], c["col_id"]), "") for c in cols])

    names, seen = [], {}
    for c in cols:
        name = str(c.get("col_leaf_label") or c["col_id"])
        n = seen.get(name, 0)
        seen[name] = n + 1
        names.append(name + "\u200b" * n)      # invisible de-dup suffix

    df = pd.DataFrame(data, columns=names, dtype=str)
    df.insert(0, "", labels)
    return df


def hierarchy_source_label(value) -> str:
    """table_t.hierarchy_source ('geometry' / 'model' / NULL) -> the human
    label shown in the Table Registry and Database views for which branch of
    the PASS2 routing tree derived this table's row hierarchy: 'geometry'
    (the new PDF-typography stage) -> "PDF geometry"; anything else --
    'model' (the historical default, the model's own `level` field) or a
    NULL/unrecognised value (pre-migration rows backfilled to 'model', or a
    future value this app doesn't know yet) -- falls back to "model levels",
    since that was the sole behavior before this stage existed. Never
    mutates the stored value -- display-layer only. Pure/testable without a
    Streamlit runtime."""
    if value == "geometry":
        return "PDF geometry"
    return "model levels"


def resolve_title(table_title, table_title_clean):
    """Pick the raw string a table's title should be built from: prefer
    table_title_clean (the footnote-superscript-stripped form) when it is
    non-NULL/non-empty; otherwise fall back to the verbatim table_title.
    Returns the chosen RAW string -- display_name() is applied by the caller
    on top of this, exactly as it is applied to table_title today. Treats an
    empty/whitespace-only clean value the same as NULL (geometry did not
    match). Never mutates either stored value -- display-layer only.
    Pure/testable without a Streamlit runtime."""
    if table_title_clean is not None and str(table_title_clean).strip():
        return table_title_clean
    return table_title


def display_name(s) -> str:
    """Human-facing title from a raw table_title/table_id/etc string:
    underscores -> spaces, whitespace collapsed/stripped, and SHOUTING-CAPS
    strings (>= 2 alphabetic chars, all uppercase) rewritten to sentence
    case ('NET FEE AND COMMISSION INCOME' -> 'Net fee and commission
    income'). Mixed-case strings are left as-is. None/empty -> "".
    Pure/testable without a Streamlit runtime."""
    if s is None:
        return ""
    s = str(s).replace("_", " ")
    s = " ".join(s.split())
    if not s:
        return ""
    alpha = [c for c in s if c.isalpha()]
    if len(alpha) >= 2 and all(c.isupper() for c in alpha):
        s = s.lower()
        for i, c in enumerate(s):
            if c.isalpha():
                s = s[:i] + c.upper() + s[i + 1:]
                break
    return s


def table_view_labels(table_records):
    """Build the Database drill-down's View options from table_t records
    (dicts with table_id, table_title, page_range), already in PDF order.

    Returns (options, by_label): options[0] is the full-exhibit label
    (mapped to None = show every table stacked); each table then appears
    as 'title (p.X)' — the human title, not the table_id — deduped with
    ' [2]', ' [3]'… when a title repeats within the document.
    Pure/testable without a Streamlit runtime.
    """
    full = f"Full view — all {len(table_records)} tables (PDF order)"
    options, by_label, seen = [full], {full: None}, {}
    for t in table_records:
        title_src = resolve_title(t.get("table_title"), t.get("table_title_clean"))
        base = display_name(title_src or t["table_id"])
        pr = t.get("page_range")
        label = f"{base} (p.{pr})" if pr else base
        n = seen.get(label, 0)
        seen[label] = n + 1
        if n:
            label = f"{label} [{n + 1}]"
        options.append(label)
        by_label[label] = t["table_id"]
    return options, by_label


def source_key_of(source_file) -> str | None:
    """document.source_file (repo-relative local path, e.g.
    'findociq/data/sources/financial_statements/X.pdf') -> the canonical
    source-store key K ('financial_statements/X.pdf'), or None when the path
    is not under data/sources/. Pure/testable without Streamlit."""
    if not source_file:
        return None
    parts = Path(str(source_file)).as_posix().split("data/sources/", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    return parts[1]


# ============================================================================
# Streamlit app. Guarded so `import findociq_app` (as the test module does)
# never triggers any `st.*` call or page render — only `streamlit run`
# executes this module as __main__.
# ============================================================================
if __name__ == "__main__":
    st.set_page_config(page_title="FinDocIQ", layout="wide")

    # --- backend: sqlite or bigquery, behind a uniform run(sql)/TBL(name) --
    # (copied from dashboard.py lines 30-84, not imported — see module docstring)
    if SOURCE == "bq":
        from google.cloud import bigquery

        @st.cache_resource
        def _backend():
            return bigquery.Client(project=PROJECT)

        def TBL(name: str) -> str:
            return f"`{PROJECT}.{DATASET}.{name}`"

        def _exec(sql: str) -> pd.DataFrame:
            return _backend().query(sql).to_dataframe()

        SRC_LABEL = f"BigQuery · {PROJECT}.{DATASET}"
    else:
        import sqlite3

        @st.cache_resource
        def _backend():
            if not DB.exists():
                st.error(f"DB not found: {DB}")
                st.stop()
            return sqlite3.connect(str(DB), check_same_thread=False)

        def TBL(name: str) -> str:
            return name

        def _exec(sql: str) -> pd.DataFrame:
            return pd.read_sql_query(sql, _backend())

        SRC_LABEL = f"SQLite · {DB.name}"

    @st.cache_data(ttl=600, show_spinner=False)
    def run(sql: str) -> pd.DataFrame:
        return _exec(sql)

    def run_opt(sql: str) -> pd.DataFrame:
        """run() for a table/view that may not exist — empty df instead of raising."""
        try:
            return run(sql)
        except Exception:
            return pd.DataFrame()

    def _esc(v: str) -> str:
        return str(v).replace("'", "''")

    def _raw_frame(doc_id: str, table_id: str):
        """Original-shape reconstruction of one table (see
        raw_table_frame): PDF row/column order, indented row labels,
        value_raw cell text, duplicate headers preserved. Returns
        (frame, n_phantom_cols_hidden)."""
        where = (f"WHERE doc_id = '{_esc(doc_id)}' "
                 f"AND table_id = '{_esc(table_id)}'")
        cols_r = run(f"SELECT col_id, col_leaf_label "
                     f"FROM {TBL('col_dim')} {where}").to_dict("records")
        df = raw_table_frame(
            run(f"SELECT row_id, row_hierarchy, row_leaf_label, "
                f"row_leaf_label_clean "
                f"FROM {TBL('row_dim')} {where}").to_dict("records"),
            cols_r,
            run(f"SELECT row_id, col_id, value_raw, value_num "
                f"FROM {TBL('cell_fact')} {where}").to_dict("records"),
        )
        n_hidden = len(cols_r) - (df.shape[1] - 1) if not df.empty else 0
        return df, n_hidden

    # ----------------------------------------------- original-PDF rendering
    @st.cache_data(show_spinner=False)
    def _pdf_local_path(source_file: str) -> str | None:
        """Resolve document.source_file to a local PDF path — the repo-relative
        path if it exists, else materialize the canonical key from the GCS
        source bucket. None when neither is available (e.g. pre-migration
        docs whose PDF never reached the bucket)."""
        p = REPO / str(source_file)
        if p.exists():
            return str(p)
        key = source_key_of(source_file)
        if key is None:
            return None
        try:
            sys.path.insert(0, str(FINDOCIQ_DIR / "pipeline"))
            import source_store  # noqa: E402
            return str(source_store.materialize(key))
        except Exception:
            return None

    @st.cache_data(show_spinner=False)
    def _pdf_n_pages(pdf_path: str) -> int:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()

    @st.cache_data(show_spinner=False)
    def _render_pdf_page(pdf_path: str, page_no: int, scale: float = 2.0) -> bytes:
        """One 1-based page -> PNG bytes (cached per (path, page, scale))."""
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_path)
        try:
            buf = io.BytesIO()
            doc[page_no - 1].render(scale=scale).to_pil().save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()

    # -------------------------------------------------------------- styling
    st.markdown(
        """
        <style>
        html, body, [class*="css"] { font-size: 18px; }
        #MainMenu, footer { visibility: hidden; }
        h1 { font-size: 40px !important; font-weight: 700 !important; }
        h2 { font-size: 28px !important; font-weight: 600 !important; }
        .findociq-title { font-size: 40px; font-weight: 700; color: #0047AB;
            margin-bottom: 0.2em; }
        .card { background: #FFFFFF; border-radius: 12px; padding: 28px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08); margin-bottom: 20px; }
        .card-title { font-size: 22px; font-weight: 600; color: #0047AB;
            margin-bottom: 10px; }
        .brand { font-size: 28px; font-weight: 700; color: #FFFFFF;
            padding: 14px 4px 24px 4px; }
        section[data-testid="stSidebar"] { background-color: #00308F; }
        section[data-testid="stSidebar"] * { color: #FFFFFF; }
        section[data-testid="stSidebar"] .stRadio > label { padding: 14px; }
        div.stButton > button {
            background-color: #0047AB; color: #FFFFFF; font-size: 18px;
            font-weight: 600; border-radius: 8px; padding: 14px 28px;
            border: none;
        }
        div.stButton > button:hover { background-color: #00308F; color: #FFFFFF; }
        .stage-done { color: #0047AB; font-weight: 600; }
        .stage-failed { color: #C0392B; font-weight: 600; }
        .stage-pending { color: #8A8A9A; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ------------------------------------------------------------- sidebar
    with st.sidebar:
        st.markdown('<div class="brand">FinDocIQ</div>', unsafe_allow_html=True)
        view = st.radio(
            # DEPLOY-MIRROR PATCH: the two views that work here.
            "Navigate", ["Dashboard", "Database"],
            label_visibility="collapsed",
        )
        st.caption(f"Source: {SRC_LABEL}")

    st.markdown(f'<div class="findociq-title">{view}</div>', unsafe_allow_html=True)

    # ------------------------------------------------------------ Dashboard
    if view == "Dashboard":
        # SHAPE, not name. This radio used to offer 'Key Financial Highlights'
        # beside 'Concept compare', which read as a dashboard NAME — and once
        # the anchor-set picker below started listing 'Key Financial Highlights'
        # too, the same words appeared twice on one screen meaning two different
        # things. It has only ever chosen a LAYOUT: one grid per bank, or the
        # banks side by side. Named for that, it composes with any anchor set.
        dash_mode = st.radio(
            "Dashboard view",
            ["Per bank", "Concept compare"],
            horizontal=True, key="dash_mode")

        # `fm` is for CONCEPT COMPARE ONLY. Key Financial Highlights no longer
        # reads it — it goes straight to the stamped DB via the anchor CSVs — so
        # it must NOT be gated on fact_metric being present. It was, and since
        # compiled_v2.db drops v_fact_metric_serving by design the highlights
        # view rendered as "fact_metric is empty or not present in this source"
        # and never ran at all.
        fm = run_opt(f"SELECT * FROM {TBL('v_fact_metric_serving')}")
        if dash_mode == "Per bank":
            st.caption(
                "One table per bank, across periods -- derived "
                "(formula-resolved) cells are marked ' ᵈ'.")

            # ANCHOR PATH. The row list and every value now come from the
            # stamped DB via data/derived/dashboards/*.csv, keyed on
            # (bank, table_type_id, canonical_leaf_id) — the identity load_v7
            # stamps. fact_metric / concept_map / bank_line_map are no longer in
            # the read path, so a figure can no longer be chosen by a resolver
            # tie-break: a multi-leaf line is DECLARED in the formula file.
            anchor_rows = dedupe_by_latest_document(
                run_opt(_ANCHOR_SQL).to_dict("records"))
            banks_anchored = sorted({_bank_of(r["institution"] or "")
                                     for r in anchor_rows}) or ["DBS"]
            # ONE ADDRESS PER BANK. Each anchor row declares the source for ONE
            # bank, so the members are composed PER BANK against that bank's own
            # rows. They must NOT be merged into a single label -> members dict:
            # 'Total assets' is declared once per bank, and DBS's and UOB's
            # declarations are the identical (FS_BALANCE_SELECTED, total_assets)
            # key. Merged, that key appeared twice, composition summed every
            # occurrence, and any bank with rows under it was DOUBLE-COUNTED —
            # OCBC's total assets rendered 1,459,774 against a filed 729,887.
            # It also flipped `is_derived` on for every line (len(mem) > 1),
            # printing a '+ total_assets + total_assets' derivation for what is
            # a plain one-line anchor.
            # `items` is still the UNION — the grid shows the same row list for
            # every bank, and a bank without a declaration just leaves it blank.
            #
            # TWO PASSES, and the order matters. The fiscal axis (spec §4.5)
            # is built from the FLOW facts of the whole corpus, and stocks are
            # then placed onto it — so the axis has to exist before any frame
            # can be composed. Pass 1 composes with `axis=None` purely to
            # collect the flow spans present; pass 2 rebuilds every frame
            # against the settled axis. Deriving the axis from the raw anchor
            # rows instead would count facts the anchors never address.
            # WHICH dashboard — discovered from the folder every rerun, never
            # declared in code. `available_dashboards()` globs the anchors dir,
            # so dropping a `<stem>_anchors.csv` (+ optional
            # `<stem>_formulaanchors.csv`) pair in adds a dashboard here with no
            # edit; deleting the pair removes it. The picker only appears once
            # there is more than one set, so a single-pair install looks exactly
            # as it did before.
            _dash_sets = available_dashboards()
            if len(_dash_sets) > 1:
                _by_name = {name: stem for stem, name in _dash_sets}
                _picked = st.radio("Dashboard", list(_by_name),
                                   horizontal=True, key="hl_set")
                _dash_stem = _by_name[_picked]
            else:
                _dash_stem = _dash_sets[0][0] if _dash_sets else None

            items, probe = [], []
            per_bank = []
            for _b in banks_anchored:
                _it, _mb = load_dashboard_anchors(_b, dashboard=_dash_stem)
                seen = {i["label"] for i in items}
                items += [i for i in _it if i["label"] not in seen]
                _rows = [r for r in anchor_rows
                         if _bank_of(r["institution"] or "") == _b]
                per_bank.append((_b, _it, _mb, _rows))
                probe.append(anchor_highlights_frame(_rows, _it, _mb))
            probe_df = (pd.concat(probe, ignore_index=True) if probe
                        else pd.DataFrame(columns=_ANCHOR_FRAME_COLS))
            fiscal_axis = fiscal_period_axis(probe_df)
            frames = [anchor_highlights_frame(_rows, _it, _mb, axis=fiscal_axis)
                      for _b, _it, _mb, _rows in per_bank]
            if not items:
                st.info(
                    f"No dashboard anchors found in {DASHBOARDS_DIR}. "
                    f"Expected an anchors CSV with a `bank` column.")
            long_df = (pd.concat(frames, ignore_index=True) if frames
                       else pd.DataFrame(columns=_ANCHOR_FRAME_COLS))
            items = attach_sections(items, long_df)

            if long_df.empty:
                st.info("No highlight rows for the configured concepts.")
            else:
                long_df = long_df.copy()
                long_df["period_label"] = long_df.apply(
                    lambda r: period_label(r["period"], r["period_span"]),
                    axis=1)

                # PERIOD SELECTOR, replacing the old "Period basis" radio.
                # The radio partitioned the grid by SPAN and made the spans
                # mutually exclusive, which is a question about the data's
                # shape that no reader of a bank dashboard has any reason to
                # answer: 'quarter' silently dropped UOB (a half-yearly filer)
                # from the page entirely and cut OCBC to its ratios block,
                # because a balance sheet is stamped `as_at` and matched no
                # flow basis at all. §4.5 removes the question — every anchor
                # declares how it joins a period, so one axis carries flows
                # and stocks together and the only choice left is WHICH
                # periods to look at.
                #
                # Default is the density-surviving axis, most recent last.
                dense_order = period_axis_order(
                    long_df, total_items=len(items) or None)
                pruned = [lb for lb, _, _ in fiscal_axis
                          if lb not in dense_order]
                period_order = st.multiselect(
                    "Periods", [lb for lb, _, _ in fiscal_axis],
                    default=dense_order, key="hl_periods",
                    help="Fiscal periods to show as columns. Balance-sheet "
                         "lines join a period by its END DATE, income and "
                         "ratio lines by the period itself, so both appear "
                         "under one column.")
                # The multiselect returns click order, not chronology.
                period_order = [lb for lb, _, _ in fiscal_axis
                                if lb in set(period_order)]
                long_df = long_df[long_df["period_label"].isin(period_order)]
                st.caption(
                    f"{len(period_order)} period"
                    f"{'s' if len(period_order) != 1 else ''}"
                    + (f" -- {len(pruned)} sparse period"
                       f"{'s' if len(pruned) != 1 else ''} hidden "
                       f"({', '.join(pruned)}); add from the box above"
                       if pruned else ""))

                banks_present = [b for b in _BANK_COLOR
                                  if b in long_df["bank"].unique()]

                compare_mode = st.toggle(
                    "Compare 3 banks side by side", key="hl_compare_mode",
                    help="One combined table (item rows x bank columns) for "
                         "a single period, instead of one table per bank. "
                         "Click a row to chart that item below.")

                if compare_mode:
                    period_choice = st.selectbox(
                        "Period", period_order, index=len(period_order) - 1,
                        key="hl_compare_period")

                    compare_df = highlights_compare_grid_frame(
                        long_df, items, banks_present, period_choice)
                    header_flags = compare_df["_section_header"]
                    chartable_flags = compare_df["_chartable"]
                    display_df = compare_df.drop(
                        columns=["_section_header", "_chartable"])

                    st.caption("Click an item row to chart its movement below.")
                    event = st.dataframe(
                        display_df.style.apply(
                            _section_row_styles(header_flags), axis=None),
                        use_container_width=True,
                        on_select="rerun", selection_mode="single-row",
                        key="hl_compare_table")

                    # A click sets the "Item over time" selectbox below, via
                    # session_state, BEFORE that widget is instantiated --
                    # section-header and coverage-gap (concept: null) rows are
                    # excluded via _chartable so clicking one is a silent no-op
                    # rather than an error. Streamlit's dataframe selection is
                    # STICKY across reruns (the clicked row stays "selected"
                    # until clicked again), so this only overrides on a NEW
                    # click (`_hl_last_click` changes) -- otherwise a manual
                    # change to the selectbox below would get silently
                    # stomped back to the stale table selection on its own
                    # rerun.
                    selected_rows = (event.selection.rows
                                     if event and event.selection else [])
                    if selected_rows:
                        idx = selected_rows[0]
                        clicked_label = display_df.index[idx]
                        if (bool(chartable_flags.iloc[idx])
                                and st.session_state.get("_hl_last_click") != clicked_label):
                            st.session_state["_hl_last_click"] = clicked_label
                            st.session_state["hl_item"] = clicked_label
                else:
                    for bank in banks_present:
                        st.markdown(f"#### {bank}")
                        bank_df = long_df[long_df["bank"] == bank]
                        bank_rows = bank_df.to_dict("records")

                        # Density is judged PER BANK, inside the user's
                        # selection. The banks do not file on the same
                        # calendar — UOB reports half-yearly — so a shared
                        # column set gives every half-yearly filer four empty
                        # quarter columns to prove it. Each grid shows the
                        # periods that bank actually populates.
                        bank_cols = [c for c in period_order
                                     if c in set(period_axis_order(
                                         bank_rows, total_items=len(items) or None))]
                        grid_df = highlights_grid_frame(bank_rows, items, bank_cols)
                        header_flags = grid_df["_section_header"]
                        display_df = grid_df.drop(columns="_section_header")
                        # EXACT CLOSE DATE in the header. The balance-sheet rows
                        # below are stocks placed by `filter_by='period_end_date'`,
                        # so their figure is the balance as at this column's close
                        # — the fiscal label alone does not say which date that is.
                        # Renamed on the display frame ONLY; bank_cols and every
                        # grid key remain the bare label.
                        display_df = display_df.rename(
                            columns=period_column_headers(fiscal_axis))

                        st.dataframe(
                            display_df.style.apply(
                                _section_row_styles(header_flags), axis=None),
                            use_container_width=True)

                        derived_notes = [
                            {"Item": r["label"], "Period": r["period_label"],
                             "Formula": r["source_row_label"] or ""}
                            for r in bank_rows if r["is_derived"]
                        ]
                        with st.expander("Derivations in this table"):
                            if derived_notes:
                                st.dataframe(
                                    pd.DataFrame(derived_notes),
                                    use_container_width=True, hide_index=True)
                            else:
                                st.caption(
                                    "No derived (formula-resolved) cells in "
                                    "this table.")

                st.markdown("**Item over time**")
                chartable_labels = [it["label"] for it in items
                                     if it.get("concept") is not None]
                item_choice = st.selectbox(
                    "Item", chartable_labels, key="hl_item")
                chart_df = long_df[long_df["label"] == item_choice].copy()
                if chart_df.empty:
                    st.info("No data for this item.")
                else:
                    chart_banks = [b for b in _BANK_COLOR
                                    if b in chart_df["bank"].unique()]
                    chart = (
                        alt.Chart(chart_df)
                        .mark_line(point=True, strokeWidth=2)
                        .encode(
                            x=alt.X("period_label:N", sort=period_order,
                                    title=None,
                                    axis=alt.Axis(labelFontSize=14,
                                                  titleFontSize=14)),
                            y=alt.Y("value_num:Q", title=item_choice,
                                    axis=alt.Axis(labelFontSize=14,
                                                  titleFontSize=14)),
                            color=alt.Color(
                                "bank:N", sort=chart_banks,
                                scale=alt.Scale(
                                    domain=chart_banks,
                                    range=[_BANK_COLOR[b] for b in chart_banks]),
                                legend=alt.Legend(title=None, labelFontSize=14)),
                            tooltip=["bank", "period_label", "value_num",
                                     "resolved_by", "source_row_label"],
                        )
                        .properties(height=400)
                        .interactive()
                    )
                    st.altair_chart(chart, use_container_width=True)

                st.download_button(
                    "Download highlights (CSV)",
                    long_df[["bank", "label", "concept", "period",
                             "value_num", "resolved_by",
                             "source_row_label"]].to_csv(index=False)
                    .encode("utf-8-sig"),
                    file_name="key_financial_highlights.csv",
                    mime="text/csv", key="hl_csv")
        elif fm.empty:
            st.info(
                "Concept compare needs `fact_metric`, which this source does not "
                "carry — compiled_v2.db drops the concept layer by design. "
                "Key Financial Highlights works: it reads the stamped leaf ids "
                "directly.")
        else:
            st.caption("Compare a concept across banks and periods.")
            concepts = sorted(fm["concept_key"].dropna().unique())
            spans_all = sorted(fm["period_span"].dropna().unique())

            f1, f2, f3 = st.columns([2, 2, 1])
            with f1:
                concept_choice = st.selectbox("Concept", concepts, key="dash_concept")
            with f2:
                span_choice = st.multiselect(
                    "Period span", spans_all, default=spans_all, key="dash_span")
            with f3:
                base_only = st.checkbox(
                    "Base only (exclude segment/geo cuts)", value=True, key="dash_base")

            concept_rows = fm[fm["concept_key"] == concept_choice].to_dict("records")
            tidy = compare_frame(concept_rows, base_only=base_only)
            if span_choice:
                tidy = tidy[tidy["period_span"].isin(span_choice)]

            if tidy.empty:
                st.warning("No rows for that concept/filter combination.")
            else:
                bases = available_bases(tidy)
                basis_choice = st.radio(
                    "Period basis",
                    bases,
                    format_func=lambda k: PERIOD_BASES[k][0],
                    index=bases.index(default_basis(tidy, bases)),
                    horizontal=True, key="cmp_period_basis")
                tidy = filter_by_basis(tidy, basis_choice)

                tidy = tidy.copy()
                tidy["bank"] = tidy["institution"].map(_bank_of)
                tidy["period_label"] = tidy.apply(
                    lambda r: period_label(r["period"], r["period_span"]), axis=1)
                period_order = period_axis_order(tidy)
                st.caption(
                    f"{PERIOD_BASES[basis_choice][0]} -- "
                    f"{len(period_order)} period"
                    f"{'s' if len(period_order) != 1 else ''}")

                unit_s = fm.loc[fm["concept_key"] == concept_choice, "unit"].dropna()
                unit = unit_s.iloc[0] if not unit_s.empty else ""

                banks_present = [b for b in _BANK_COLOR if b in tidy["bank"].unique()]

                st.markdown(
                    f'<div class="card-title">{concept_choice}'
                    f'{f" ({unit})" if unit else ""}</div>',
                    unsafe_allow_html=True,
                )
                chart = (
                    alt.Chart(tidy)
                    .mark_line(point=True, strokeWidth=2)
                    .encode(
                        x=alt.X("period_label:N", sort=period_order, title=None,
                                axis=alt.Axis(labelFontSize=14, titleFontSize=14)),
                        y=alt.Y("value_num:Q", title=unit or "value",
                                axis=alt.Axis(labelFontSize=14, titleFontSize=14)),
                        color=alt.Color(
                            "bank:N", sort=banks_present,
                            scale=alt.Scale(domain=banks_present,
                                             range=[_BANK_COLOR[b] for b in banks_present]),
                            legend=alt.Legend(title=None, labelFontSize=14)),
                        tooltip=["bank", "period_label", "value_num"],
                    )
                    .properties(height=400)
                    .interactive()
                )
                st.altair_chart(chart, use_container_width=True)

                st.markdown("**Compare table**")
                wide = tidy.pivot_table(index="period_label", columns="bank",
                                         values="value_num", aggfunc="first")
                wide = wide.reindex(period_order)
                wide = wide[[b for b in _BANK_COLOR if b in wide.columns]
                            + [b for b in wide.columns if b not in _BANK_COLOR]]
                st.dataframe(wide, use_container_width=True)

                present_banks = sorted({
                    _bank_of(inst) for inst in
                    fm.loc[fm["concept_key"] == concept_choice, "institution"].dropna()
                })
                st.caption(
                    f"Publishes {concept_choice}: "
                    f"{', '.join(present_banks) if present_banks else 'none'}")

    # ------------------------------------------------------------ Database
    elif view == "Database":
        st.caption("Browse the extracted data as stored in the schema.")

        counts = run(
            f"SELECT "
            f"(SELECT COUNT(*) FROM {TBL('document')}) AS documents, "
            f"(SELECT COUNT(*) FROM {TBL('table_t')}) AS tables, "
            f"(SELECT COUNT(*) FROM {TBL('row_dim')}) AS rows, "
            f"(SELECT COUNT(*) FROM {TBL('cell_fact')}) AS cells"
        ).iloc[0]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Documents", int(counts["documents"]))
        m2.metric("Tables", int(counts["tables"]))
        m3.metric("Rows", int(counts["rows"]))
        m4.metric("Cells", int(counts["cells"]))

        st.markdown(
            '<div class="card"><div class="card-title">Drill down</div>',
            unsafe_allow_html=True,
        )
        banks_df = run(
            f"SELECT DISTINCT institution FROM {TBL('document')} "
            "ORDER BY institution")
        bank_sel = st.selectbox("Bank", ["All"] + banks_df["institution"].tolist())

        docs_sql = f"SELECT doc_id, institution FROM {TBL('document')}"
        if bank_sel != "All":
            docs_sql += f" WHERE institution = '{_esc(bank_sel)}'"
        docs_sql += " ORDER BY doc_id"
        docs_df = run(docs_sql)

        if docs_df.empty:
            st.info("No documents for this bank.")
        else:
            doc_sel = st.selectbox("Document", docs_df["doc_id"].tolist())
            table_page_range = None    # set below once a table is selected

            # PDF order, not alphabetical — the full view below must stack
            # tables the way the original reads. The document's reading
            # order lives in section.seq (table_t.section_no is often NULL).
            tbls_df = run(
                f"SELECT t.table_id, t.table_type, t.table_title, "
                f"t.table_title_clean, t.hierarchy_source, t.period, "
                f"t.unit, t.page_range, s.seq "
                f"FROM {TBL('table_t')} t "
                f"LEFT JOIN {TBL('section')} s ON s.doc_id = t.doc_id "
                f"AND s.section_id = t.section_id "
                f"WHERE t.doc_id = '{_esc(doc_sel)}' "
                f"ORDER BY COALESCE(s.seq, 999999), t.section_no, t.table_id")

            if tbls_df.empty:
                st.info("No tables for this document.")
            else:
                # View: default = the FULL table exhibit (all sub-tables
                # stacked in PDF order); each sub-table, by its human
                # title, is a further drill-down.
                options, by_label = table_view_labels(
                    tbls_df.to_dict("records"))
                choice = st.selectbox("View", options)
                table_sel = by_label[choice]

                with st.expander("Table metadata"):
                    st.dataframe(tbls_df, use_container_width=True,
                                 hide_index=True)

                if table_sel is None:
                    # ---------------- full view: whole exhibit, PDF order
                    for tr in tbls_df.itertuples():
                        title_src = resolve_title(tr.table_title, tr.table_title_clean)
                        st.markdown(f"**{display_name(title_src or tr.table_id)}**")
                        full_df, n_hidden = _raw_frame(doc_sel, tr.table_id)
                        meta = " · ".join(
                            str(x) for x in (
                                tr.unit, f"p.{tr.page_range}",
                                f"row hierarchy: "
                                f"{hierarchy_source_label(tr.hierarchy_source)}")
                            if x)
                        if n_hidden:
                            meta += (f" · {n_hidden} unused column "
                                     f"definition(s) hidden (loader artifact)")
                        if meta:
                            st.caption(meta)
                        st.dataframe(full_df, use_container_width=True,
                                     hide_index=True)

            if not tbls_df.empty and table_sel is not None:
                sel_row = tbls_df.loc[tbls_df["table_id"] == table_sel].iloc[0]
                table_page_range = sel_row["page_range"]

                title_src = resolve_title(
                    sel_row["table_title"], sel_row.get("table_title_clean"))
                st.markdown(
                    f"**{display_name(title_src or table_sel)}**")
                raw_df, n_hidden = _raw_frame(doc_sel, table_sel)
                meta = " · ".join(
                    str(x) for x in (
                        sel_row["unit"], f"p.{sel_row['page_range']}",
                        f"row hierarchy: "
                        f"{hierarchy_source_label(sel_row.get('hierarchy_source'))}")
                    if x)
                if n_hidden:
                    meta += (f" · {n_hidden} unused column "
                             f"definition(s) hidden (loader artifact)")
                if meta:
                    st.caption(meta)
                if raw_df.empty:
                    st.info("No cells for this table.")
                else:
                    st.dataframe(raw_df, use_container_width=True,
                                 hide_index=True)
                    st.download_button(
                        "Download table (CSV, original shape)",
                        raw_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"{table_sel}.csv", mime="text/csv",
                        key=f"rawcsv_{table_sel}")

                with st.expander(
                        "Row identity mapping (per row: cleaned label, concept, "
                        "sums_to, unit, geo, segment)"):
                    st.caption(
                        "row_leaf_label is VERBATIM from the PDF (footnote markers "
                        "included) — it is the evidence the verifier checks against "
                        "the page. row_leaf_label_clean is the identity form, with "
                        "footnote superscripts stripped typographically by the "
                        "geometry stage; it is empty on tables whose row hierarchy "
                        "came from model levels.")
                    rows_df = run(
                        f"SELECT row_id, row_hierarchy, row_leaf_label, "
                        f"row_leaf_label_clean, concept_key, unit, geo_key, "
                        f"segment_key, sums_to "
                        f"FROM {TBL('row_dim')} "
                        f"WHERE doc_id = '{_esc(doc_sel)}' "
                        f"AND table_id = '{_esc(table_sel)}' ORDER BY row_id")
                    st.dataframe(rows_df, use_container_width=True,
                                 hide_index=True)

                with st.expander("Numeric pivot (columns deduplicated)"):
                    cells_df = run(
                        f"SELECT r.row_leaf_label, "
                        f"COALESCE(c.col_leaf_label, CAST(f.col_id AS TEXT)) "
                        f"AS col_label, c.col_period, c.period_span, f.value_num "
                        f"FROM {TBL('cell_fact')} f "
                        f"JOIN {TBL('row_dim')} r ON r.doc_id = f.doc_id "
                        f"AND r.table_id = f.table_id AND r.row_id = f.row_id "
                        f"LEFT JOIN {TBL('col_dim')} c ON c.doc_id = f.doc_id "
                        f"AND c.table_id = f.table_id AND c.col_id = f.col_id "
                        f"WHERE f.doc_id = '{_esc(doc_sel)}' "
                        f"AND f.table_id = '{_esc(table_sel)}'")
                    if cells_df.empty:
                        st.info("No cells for this table.")
                    else:
                        # Columns carrying a period (col_period not NULL) show
                        # the canonical period_label, replacing the raw
                        # header entirely; columns with no period (e.g. a
                        # '% chg' comparison column, which legitimately has
                        # none) keep their verbatim col_label.
                        cells_df = cells_df.copy()
                        cells_df["col_label"] = cells_df.apply(
                            lambda r: period_label(r["col_period"], r["period_span"])
                            if pd.notna(r["col_period"]) else r["col_label"],
                            axis=1)
                        pivot = cells_df.pivot_table(
                            index="row_leaf_label", columns="col_label",
                            values="value_num", aggfunc="first")
                        st.dataframe(pivot, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

        # ------------------------------------------- original PDF panel
        # The source document behind the current selection, so the
        # extraction can be eyeballed against the original. Defaults to
        # the selected table's page_range; the page picker roams the
        # whole PDF. Materializes from the GCS source bucket on demand.
        if not docs_df.empty:
            st.markdown(
                '<div class="card"><div class="card-title">Original document</div>',
                unsafe_allow_html=True,
            )
            src_df = run(
                f"SELECT source_file FROM {TBL('document')} "
                f"WHERE doc_id = '{_esc(doc_sel)}'")
            source_file = (src_df["source_file"].iloc[0]
                           if not src_df.empty else None)
            pdf_path = _pdf_local_path(source_file) if source_file else None
            if pdf_path is None:
                st.info(
                    "Original PDF unavailable — not in the local cache and "
                    "no blob in the GCS source bucket (pre-migration "
                    "document).")
            else:
                n_pages = _pdf_n_pages(pdf_path)
                table_pages = pages_from_range(table_page_range, n_pages)
                default_page = table_pages[0] if table_pages else 1
                if table_pages:
                    st.caption(
                        f"Selected table is on page"
                        f"{'s' if len(table_pages) > 1 else ''} "
                        f"{table_page_range} of {n_pages}.")
                page_no = st.number_input(
                    "Page", min_value=1, max_value=n_pages,
                    value=default_page, key=f"pdfpage_{doc_sel}")
                pages_to_show = (
                    table_pages
                    if table_pages and int(page_no) == default_page
                    else [int(page_no)])
                for p in pages_to_show:
                    st.image(
                        _render_pdf_page(pdf_path, p),
                        caption=f"{Path(source_file).name} — page {p}",
                        use_container_width=True)
            st.markdown("</div>", unsafe_allow_html=True)

        with st.expander("Raw tables"):
            health = run(
                f"SELECT "
                f"(SELECT COUNT(*) FROM {TBL('document')}) AS document, "
                f"(SELECT COUNT(*) FROM {TBL('table_t')}) AS table_t, "
                f"(SELECT COUNT(*) FROM {TBL('row_dim')}) AS row_dim, "
                f"(SELECT COUNT(*) FROM {TBL('cell_fact')}) AS cell_fact"
            )
            st.dataframe(health, use_container_width=True, hide_index=True)

    # DEPLOY-MIRROR PATCH: the Table Registry and Ingest views ended here and ran
    # to EOF. Both are dropped in this mirror.
    #
    #   Table Registry -- its catalog / line-map panels read table_catalog and
    #   bank_line_map, which are RETIRED BY DESIGN (the canonical leaf label
    #   replaced them). A legacy reader with nothing left to show.
    #
    #   Ingest -- shelled out to findociq/pipeline/run_doc.py, which needs
    #   PaddleOCR + GCS + Gemini and does not ship here.
    #
    # The helpers that served only those two views are pruned below, which is
    # why this mirror needs no pipeline/ package at all.
