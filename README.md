# FinDocIQ — dashboard

Deploy-only mirror of the FinDocIQ Streamlit app. Source of truth is
[`qyunhan/FinDocIQ`](https://github.com/qyunhan/FinDocIQ); this repo carries the
minimum set of files the app needs at runtime — 11 MB instead of ~220 MB — so
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
    db/compiled_v2.db             the data — 10.5 MB, 98.5% of this repo
    data/dashboards/*.csv         anchors; Key Financial Highlights renders
                                  empty without them
    pipeline/source_store.py      16.6 KB of support code, 0.16% of the repo —
    pipeline/mapping/normalize.py and not optional, see below
    pipeline/mapping/__init__.py
    sync.sh                       re-copy + re-patch from the source repo

**Why three extra Python files?** They are not optional imports:

* `source_store.py` — `streamlit_app.py` does a bare `import source_store` in the
  Ingest view, and Ingest is the **first** sidebar tab, so it runs on every cold
  load. Without this file the app raises `ModuleNotFoundError` before rendering
  anything.
* `mapping/normalize.py` (+ the empty `__init__.py` that makes `mapping` a
  package) — supplies `normalize_row_label`, which builds row identities in the
  Table Registry view.

Everything else upstream — the pipeline, prompts, specs, experiments, 215
commits of a 31 MB ingest DB — is what the mirror exists to leave behind.

**Two patched lines.** Upstream sits at `findociq/app/findociq_app.py` and does
`REPO = Path(__file__).resolve().parents[2]`; here the app is at the repo root,
so `REPO`/`FINDOCIQ_DIR` collapse to `parent`, and `DASHBOARDS_DIR` drops the
`derived/` level. `sync.sh` reapplies both and **aborts** if upstream ever edits
those lines, rather than half-applying a patch.

## What does not work here (by design)

* **Ingest** — the button shells out to `findociq/pipeline/run_doc.py`, which is
  not in this repo and needs PaddleOCR + GCS + Gemini regardless. It fails
  without crashing the app.
* **Original-PDF pane** — source PDFs are not committed anywhere, and the GCS
  fallback needs credentials. Renders empty.
* **Table Registry / Concept compare** — `compiled_v2.db` currently ships
  without `table_catalog`, `bank_line_map`, `row_lineage` and
  `v_fact_metric_serving`. Fix that in the source repo with
  `build_compiled_v2.py --carry-from`, then re-sync.

Working views: **Database** browser and **Dashboard → Key Financial Highlights**
(14,624 anchor rows).

## Updating

After a DB rebuild or app change upstream:

    ./sync.sh /home/user/FinDocIQ
    git add -A && git commit -m "sync: <what changed>" && git push
