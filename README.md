# FinDocIQ — dashboard

Deploy-only mirror of the FinDocIQ Streamlit app. Source of truth is
[`qyunhan/FinDocIQ`](https://github.com/qyunhan/FinDocIQ); this repo carries the
minimum set of files the app needs at runtime — ~11 MB instead of ~220 MB — so
Streamlit Community Cloud clones and boots quickly.

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

    streamlit_app.py              the app (upstream findociq/app/findociq_app.py)
    requirements.txt              Cloud installs from the repo root
    .streamlit/config.toml        cobalt theme
    db/compiled_v2.db             the data — 10.5 MB, ~98% of this repo
    data/dashboards/*.csv         anchors; Key Financial Highlights renders
                                  empty without them
    pipeline/mapping/normalize.py 12 KB — supplies normalize_row_label, which
    pipeline/mapping/__init__.py  builds row identities in the Table Registry view
    sync.sh                       re-copy + re-patch from the source repo

Two Python files besides the app, 12 KB total. Everything else upstream — the
pipeline, prompts, specs, experiments, 215 commits of a 31 MB ingest DB — is
what the mirror exists to leave behind. The DB is the size, not the code.

## Patches against upstream

`sync.sh` reapplies all of these and **aborts** if upstream edits a patched
line, rather than half-applying:

1. **Flattened layout.** Upstream sits at `findociq/app/findociq_app.py` and does
   `REPO = Path(__file__).resolve().parents[2]`; here the app is at the repo
   root, so `REPO`/`FINDOCIQ_DIR` collapse to `parent` and `DASHBOARDS_DIR`
   drops the `derived/` level.
2. **Nav reordered, Ingest removed** — `["Dashboard", "Database", "Table Registry"]`.
3. **Ingest block truncated.** It was the final `else:` of the view dispatch and
   ran to EOF. It shelled out to `findociq/pipeline/run_doc.py`, which needs
   PaddleOCR + GCS + Gemini and does not ship here. Dropping it also removed the
   only unguarded `import source_store` (so `source_store.py` is gone) and the
   only caller of `_plain_xlsx_export` (so `openpyxl` is gone from requirements).

## How the dashboard resolves a figure

Key Financial Highlights goes **straight to the stamped DB via the anchor CSVs**,
keyed on `(bank, table_type_id, canonical_leaf_id)` — the identity the loader
stamps. One address per bank; a multi-leaf line is *declared* in the formula
file, never chosen by a resolver tie-break.

`table_catalog`, `bank_line_map`, `row_lineage` and `v_fact_metric_serving` are
**retired by design** — the canonical leaf label replaced them. They are not
missing from `compiled_v2.db` and must not be carried back into it.

Consequence: the **Table Registry** catalog / line-map panels and **Concept
compare** are legacy readers of those retired tables and render empty. They are
guarded (`run_opt`), so they fail quietly rather than erroring. Rewriting them
onto the canonical-leaf path is open work upstream.

Working views: **Dashboard → Key Financial Highlights** (14,624 anchor rows) and
the **Database** browser.

The original-PDF pane in the Database view stays empty on Cloud: source PDFs are
not committed anywhere, and the GCS fallback needs credentials.

## Updating

After a DB rebuild or app change upstream:

    ./sync.sh /home/user/FinDocIQ
    git add -A && git commit -m "sync: <what changed>" && git push
