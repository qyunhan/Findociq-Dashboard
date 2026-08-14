#!/usr/bin/env bash
# Re-copy the deploy file set from the FinDocIQ source repo into this mirror,
# then reapply the patches this trimmed, two-view layout needs.
#
#   ./sync.sh [path-to-FinDocIQ]      (default: /home/user/FinDocIQ)
#
# This script IS the contract. Never hand-edit streamlit_app.py — change
# findociq/app/findociq_app.py upstream and re-run this. If the app grows a new
# import or reads a new data file, add it here, or the mirror boots on the
# workstation (where the full source tree is on sys.path) and dies on Cloud.
set -euo pipefail

SRC="${1:-/home/user/FinDocIQ}"
DST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$SRC/findociq/app/findociq_app.py" ] || {
    echo "not a FinDocIQ checkout: $SRC" >&2; exit 1
}

mkdir -p "$DST/.streamlit" "$DST/db" "$DST/data/dashboards"

cp "$SRC/findociq/app/findociq_app.py"       "$DST/streamlit_app.py"
cp "$SRC/.streamlit/config.toml"             "$DST/.streamlit/"
cp "$SRC/findociq/db/compiled_v2.db"         "$DST/db/"
# EVERY anchor pair, not the two highlights files by name. The app globs
# `*_anchors.csv` / `*_formulaanchors.csv` and merges whatever it finds, so a new
# dashboard is added by DROPPING A CSV PAIR IN — naming the files here meant the
# next one (breakdown_of_gross_nb_loans_*) would load upstream and be silently
# absent from the deploy. Note `*_anchors.csv` does not match `*_formulaanchors.csv`
# (the char before 'anchors' is 'a', not '_'), so neither is copied twice.
rm -f "$DST"/data/dashboards/*.csv
cp "$SRC"/findociq/data/derived/dashboards/*_anchors.csv \
   "$SRC"/findociq/data/derived/dashboards/*_formulaanchors.csv \
                                             "$DST/data/dashboards/"

# The source PDFs behind the Database view's "Original document" panel. DERIVED
# FROM THE DB, never a hardcoded list: every document.source_file is resolved
# against the upstream sources tree and only those blobs are copied, preserving
# the subfolder (a Pillar 3 doc lives under sources/pillar3/, not
# financial_statements/). That is 7.7 MB for the 10 documents compiled_v2.db
# carries, against 44.5 MB for all 63 PDFs upstream — and it self-adjusts when
# the DB gains or loses a document.
#
# Without these the panel reports "Original PDF unavailable" for every document:
# the mirror has no GCS credentials, so the source_store fallback can never fire.
rm -rf "$DST/data/sources"
python3 - "$SRC" "$DST" <<'PYCOPY'
import shutil, sqlite3, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
tree = src / "findociq" / "data" / "sources"
con = sqlite3.connect(src / "findociq" / "db" / "compiled_v2.db")
n = miss = 0
for (sf,) in con.execute("SELECT DISTINCT source_file FROM document"):
    if not sf:
        continue
    hits = sorted(q for q in tree.rglob(Path(sf).name) if q.is_file())
    if not hits:
        print(f"  sync.sh: no PDF for {sf}", file=sys.stderr); miss += 1; continue
    out = dst / "data" / "sources" / hits[0].relative_to(tree)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hits[0], out); n += 1
print(f"  {n} source PDF(s) copied" + (f", {miss} unresolved" if miss else ""))
PYCOPY

# NOT copied, deliberately:
#   requirements.txt      upstream's root file is a `-r findociq/app/...` pointer
#                         that does not resolve here, and this copy drops
#                         matplotlib/pyyaml/openpyxl (see that file's header)
#   pipeline/*.py         source_store and mapping/normalize were only reachable
#                         from the Ingest and Table Registry views, both dropped
#                         below — so the mirror ships no Python but the app

python3 - "$DST/streamlit_app.py" <<'PY'
import ast, sys, pathlib

# ---------------------------------------------------------------- 1. patches
# (upstream text, mirror replacement). Exact-match — if upstream edits any of
# these, sync ABORTS so a human reapplies it, rather than half-patching.
PATCHES = [
    # flattened layout: the app sits at the repo root, not findociq/app/
    ('REPO = Path(__file__).resolve().parents[2]           # repo root (findociq\'s parent)\n'
     'FINDOCIQ_DIR = REPO / "findociq"',
     '# DEPLOY-MIRROR PATCH (see README "Structure"): upstream lives at\n'
     '# findociq/app/findociq_app.py and walks up two levels; here the app sits at the\n'
     '# repo root, so both constants collapse to that root.\n'
     'REPO = Path(__file__).resolve().parent               # repo root\n'
     'FINDOCIQ_DIR = REPO'),
    ('DASHBOARDS_DIR = FINDOCIQ_DIR / "data" / "derived" / "dashboards"',
     'DASHBOARDS_DIR = FINDOCIQ_DIR / "data" / "dashboards"   '
     '# DEPLOY-MIRROR PATCH: was data/derived/dashboards'),
    # nav: two views only
    ('            "Navigate", ["Ingest", "Database", "Table Registry", "Dashboard"],',
     '            # DEPLOY-MIRROR PATCH: the two views that work here.\n'
     '            "Navigate", ["Dashboard", "Database"],'),
]

# --------------------------------------------------- 2. view-block truncation
# Table Registry and Ingest are the last two branches of the view dispatch and
# run to EOF, so dropping them is a truncation at the Table Registry banner.
BANNER = "    # ------------------------------------------------------- Table Registry"
NOTE = """    # DEPLOY-MIRROR PATCH: the Table Registry and Ingest views ended here and ran
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
"""

# ------------------------------------------------------------- 3. dead prune
# Top-level symbols reachable ONLY from the two dropped views. Derived by an
# AST reachability fixpoint, then frozen here so a sync is reviewable rather
# than surprising. Each must exist exactly once, and must be unreferenced after
# its own definition is removed — both are asserted.
# 2026-08-14: eight names left this list because they no longer EXIST upstream.
# FinDocIQ-v2 17480ee deleted the bank_line_map/table_catalog machinery outright
# (table_masterlist_frame, line_item_masterlist_frame, line_item_display_order,
# line_item_benchmark_frame, _ordered_row_addresses, _doc_kind_of,
# BENCHMARK_PERIOD, BENCHMARK_LABEL) — the retired mapping layer this mirror had
# been pruning by hand is now simply gone at the source, which is the better
# place for it. Listing a symbol that does not exist makes sync abort, so they
# are dropped rather than kept "just in case".
#
# The three anchor-registry helpers replaced them upstream. They are reachable
# ONLY from the Table Registry view, which this mirror truncates, so they are
# pruned here for the same reason their predecessors were.
DEAD = [
    "STAGES", "_plain_xlsx_export", "doc_to_csv", "stage_states",
    "anchor_declarations", "anchor_coverage_frame", "unanchored_leaves_frame",
]

p = pathlib.Path(sys.argv[1])
src = p.read_text()

for old, new in PATCHES:
    if src.count(old) != 1:
        sys.exit(f"sync.sh: upstream changed a patched line — reapply by hand:\n{old}")
    src = src.replace(old, new)

lines = src.splitlines(keepends=True)
hits = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == BANNER]
if len(hits) != 1:
    sys.exit("sync.sh: could not find exactly one Table Registry banner — truncate by hand")
upstream_line = hits[0] + 1
src = "".join(lines[:hits[0]]) + NOTE

def spans(source):
    """Top-level {name: (start, end)} 1-indexed, comment block above included."""
    tree, lines = ast.parse(source), source.splitlines()
    out = {}
    def add(name, node):
        s = node.lineno
        while s > 1 and lines[s - 2].lstrip().startswith("#"):
            s -= 1                     # absorb the comment block above
        out[name] = (s, node.end_lineno)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(n.name, n)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    add(t.id, n)
    return out

# Fixpoint, not one pass: these symbols reference each other (STAGES is
# stage_states' default arg, _ordered_row_addresses is called by two others), so
# a name only becomes removable once its dead callers are gone. Each round drops
# whatever is now unreferenced; a round that drops nothing means the survivors
# are genuinely reachable from live code, and we abort naming them.
pending = list(DEAD)
while pending:
    table = spans(src)
    missing = [n for n in pending if n not in table]
    if missing:
        sys.exit(f"sync.sh: no longer top-level symbols, review the DEAD list: {missing}")
    tree = ast.parse(src)
    refs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            refs.setdefault(node.id, []).append(node.lineno)
    removable = [n for n in pending
                 if not [ln for ln in refs.get(n, [])
                         if not (table[n][0] <= ln <= table[n][1])]]
    if not removable:
        sys.exit(f"sync.sh: REACHABLE from live code now, drop from DEAD: {pending}")
    for name in removable:
        s, e = spans(src)[name]
        lines = src.splitlines(keepends=True)
        src = "".join(lines[:s - 1] + lines[e:])
    pending = [n for n in pending if n not in removable]

while "\n\n\n\n" in src:
    src = src.replace("\n\n\n\n", "\n\n\n")

ast.parse(src)
p.write_text(src)
print(f"patched; views truncated at upstream line {upstream_line}; "
      f"{len(DEAD)} dead symbols pruned")
PY

echo "synced from $SRC"
du -sh "$DST"
