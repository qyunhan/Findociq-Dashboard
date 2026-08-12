# FinDocIQ — dashboard

Deploy-only mirror of the FinDocIQ Streamlit app, trimmed to the two views that
work off `compiled_v2.db` alone: **Dashboard** and **Database**. Source of truth
is [`qyunhan/FinDocIQ`](https://github.com/qyunhan/FinDocIQ); this repo carries
~11 MB instead of ~220 MB so Streamlit Community Cloud clones and boots quickly.

## Deploy on Streamlit Community Cloud

| Setting | Value |
| --- | --- |
| Repository | `qyunhan/Findociq-Dashboard` |
| Branch | `main` |
| Main file path | `streamlit_app.py` |
| Secrets | none — the app runs entirely on defaults |

## Run locally

    pip install -r requirements.txt
    streamlit run streamlit_app.py

## Structure

    streamlit_app.py        the app — the only Python in the repo
    requirements.txt        Cloud installs from the repo root
    .streamlit/config.toml  cobalt theme
    db/compiled_v2.db       the data — 10.5 MB, ~98% of this repo
    data/dashboards/*.csv   anchors; Key Financial Highlights renders empty
                            without them
    sync.sh                 regenerates streamlit_app.py from upstream

No `pipeline/` package, no support modules. The DB is the size, not the code.

## Never hand-edit `streamlit_app.py`

Change `findociq/app/findociq_app.py` upstream, then:

    ./sync.sh /home/user/FinDocIQ
    git add -A && git commit -m "sync: <what changed>" && git push

`sync.sh` re-copies the file set and reapplies every transformation below. It
**aborts** rather than half-apply if upstream moves out from under it, and
running it from an empty directory reproduces this app byte-for-byte.

## What `sync.sh` does to the app

1. **Flattens the layout.** Upstream sits at `findociq/app/findociq_app.py` and
   does `REPO = Path(__file__).resolve().parents[2]`; here the app is at the repo
   root, so `REPO`/`FINDOCIQ_DIR` collapse to `parent` and `DASHBOARDS_DIR` drops
   the `derived/` level.
2. **Cuts the nav to** `["Dashboard", "Database"]`.
3. **Truncates the Table Registry and Ingest views.** They were the last two
   branches of the view dispatch and ran to EOF, so removing them is a single
   truncation.
   * *Table Registry* — its catalog / line-map panels read `table_catalog` and
     `bank_line_map`, retired by design (see below). A legacy reader with
     nothing left to show.
   * *Ingest* — shelled out to `findociq/pipeline/run_doc.py`, which needs
     PaddleOCR + GCS + Gemini and does not ship here.
4. **Prunes the 12 top-level symbols reachable only from those two views**, each
   with its comment block. Found by an AST reachability fixpoint, then frozen as
   a named list so a sync is reviewable rather than surprising; the prune re-runs
   that fixpoint and **aborts** if any of them became reachable upstream.

Net: 2,809 → 1,958 lines, and with them went `pipeline/source_store.py`,
`pipeline/mapping/normalize.py` and the `openpyxl` dependency — each had its last
live caller inside a dropped view.

## How the dashboard resolves a figure

Key Financial Highlights goes **straight to the stamped DB via the anchor CSVs**,
keyed on `(bank, table_type_id, canonical_leaf_id)` — the identity the loader
stamps. One address per bank; a multi-leaf line is *declared* in the formula
file, never chosen by a resolver tie-break.

`table_catalog`, `bank_line_map`, `row_lineage` and `v_fact_metric_serving` are
**retired by design** — the canonical leaf label replaced them. They are not
missing from `compiled_v2.db` and must not be carried back in.

## Known gap

The original-PDF pane in the Database view stays empty: source PDFs are not
committed anywhere, and the GCS fallback needs credentials. The lookup is inside
`try/except` and returns `None`, so it degrades quietly.
