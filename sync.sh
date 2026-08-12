#!/usr/bin/env bash
# Re-copy the deploy file set from the FinDocIQ source repo into this mirror,
# then reapply the patches the flattened, Ingest-free layout needs.
#
#   ./sync.sh [path-to-FinDocIQ]      (default: /home/user/FinDocIQ)
#
# This list IS the contract. If findociq_app.py grows a new import or reads a
# new data file, add it here — otherwise the mirror boots on the workstation
# (where the full source tree is on sys.path) and dies on Streamlit Cloud.
set -euo pipefail

SRC="${1:-/home/user/FinDocIQ}"
DST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$SRC/findociq/app/findociq_app.py" ] || {
    echo "not a FinDocIQ checkout: $SRC" >&2; exit 1
}

mkdir -p "$DST/.streamlit" "$DST/db" "$DST/data/dashboards" "$DST/pipeline/mapping"

cp "$SRC/findociq/app/findociq_app.py"       "$DST/streamlit_app.py"
cp "$SRC/.streamlit/config.toml"             "$DST/.streamlit/"
cp "$SRC/findociq/db/compiled_v2.db"         "$DST/db/"
cp "$SRC/findociq/data/derived/dashboards/highlights_dashboard_anchors.csv" \
   "$SRC/findociq/data/derived/dashboards/highlights_formulaanchors.csv" \
                                             "$DST/data/dashboards/"
cp "$SRC/findociq/pipeline/mapping/__init__.py" \
   "$SRC/findociq/pipeline/mapping/normalize.py" \
                                             "$DST/pipeline/mapping/"

# NOT copied, deliberately:
#   requirements.txt   upstream's root file is a `-r findociq/app/...` pointer
#                      that does not resolve here, and this copy drops
#                      matplotlib/pyyaml/openpyxl (see that file's header)
#   source_store.py    its only unguarded import lived in the Ingest view; the
#                      one that survives (_pdf_local_path) is inside try/except

python3 - "$DST/streamlit_app.py" <<'PY'
import sys, pathlib

# (upstream text, mirror replacement). Exact-match — if upstream edits any of
# these, sync ABORTS so a human reapplies it, rather than half-patching.
PATCHES = [
    # 1. flattened layout: the app sits at the repo root, not findociq/app/
    ('REPO = Path(__file__).resolve().parents[2]           # repo root (findociq\'s parent)\n'
     'FINDOCIQ_DIR = REPO / "findociq"',
     '# DEPLOY-MIRROR PATCH (see README "Structure"): upstream lives at\n'
     '# findociq/app/findociq_app.py and walks up two levels; here the app sits at the\n'
     '# repo root, so both constants collapse to that root. sync.sh reapplies this and\n'
     '# fails loudly if upstream ever edits these lines.\n'
     'REPO = Path(__file__).resolve().parent               # repo root\n'
     'FINDOCIQ_DIR = REPO'),
    ('DASHBOARDS_DIR = FINDOCIQ_DIR / "data" / "derived" / "dashboards"',
     'DASHBOARDS_DIR = FINDOCIQ_DIR / "data" / "dashboards"   '
     '# DEPLOY-MIRROR PATCH: was data/derived/dashboards'),
    # 2. nav: Dashboard first, Ingest gone
    ('            "Navigate", ["Ingest", "Database", "Table Registry", "Dashboard"],',
     '            # DEPLOY-MIRROR PATCH: Dashboard first (it is the view that works\n'
     '            # here), and Ingest dropped entirely — its block is truncated below.\n'
     '            "Navigate", ["Dashboard", "Database", "Table Registry"],'),
]

# 3. the Ingest view is the last `else:` of the view dispatch and runs to EOF,
#    so removing it is a truncation at its banner comment.
INGEST_BANNER = "    # ------------------------------------------------------------- Ingest"
TRUNCATION_NOTE = """    # DEPLOY-MIRROR PATCH: the Ingest view ended here and ran to EOF. It is
    # dropped in this mirror — it shells out to findociq/pipeline/run_doc.py,
    # which needs PaddleOCR + GCS + Gemini and does not ship here. Removing it
    # also removed the only unguarded `import source_store` and the only
    # caller of _plain_xlsx_export (the sole openpyxl user).
"""

p = pathlib.Path(sys.argv[1])
src = p.read_text()
for old, new in PATCHES:
    if src.count(old) != 1:
        sys.exit(f"sync.sh: upstream changed a patched line — reapply by hand:\n{old}")
    src = src.replace(old, new)

lines = src.splitlines(keepends=True)
hits = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == INGEST_BANNER]
if len(hits) != 1:
    sys.exit("sync.sh: could not find exactly one Ingest banner — truncate by hand")
p.write_text("".join(lines[:hits[0]]) + TRUNCATION_NOTE)
print(f"patched; Ingest block truncated at upstream line {hits[0] + 1}")
PY

echo "synced from $SRC"
du -sh "$DST"
