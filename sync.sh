#!/usr/bin/env bash
# Re-copy the deploy file set from the FinDocIQ source repo into this mirror,
# then reapply the two path patches the flattened layout needs.
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
cp "$SRC/findociq/pipeline/source_store.py"  "$DST/pipeline/"
cp "$SRC/findociq/pipeline/mapping/__init__.py" \
   "$SRC/findociq/pipeline/mapping/normalize.py" \
                                             "$DST/pipeline/mapping/"

# requirements.txt is deliberately NOT copied: upstream's root file is a
# `-r findociq/app/requirements.txt` pointer that does not resolve here, and this
# repo's copy drops matplotlib/pyyaml (verified unused by the app).

python3 - "$DST/streamlit_app.py" <<'PY'
import sys, pathlib

# (upstream line, mirror replacement). Exact-match — an upstream edit to any of
# these must be reviewed by a human, not silently half-applied.
PATCHES = [
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
]

p = pathlib.Path(sys.argv[1])
src = p.read_text()
for old, new in PATCHES:
    if src.count(old) != 1:
        sys.exit(f"sync.sh: upstream changed a patched line — reapply by hand:\n{old}")
    src = src.replace(old, new)
p.write_text(src)
print("path patches applied")
PY

echo "synced from $SRC"
du -sh "$DST"
