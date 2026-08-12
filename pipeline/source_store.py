"""source_store.py — the single choke point between the pipeline and the GCS
sources bucket. GCS is the SOLE source of truth for raw PDFs; local
findociq/data/sources/ is an ephemeral, gitignored materialization cache.

Canonical source key:
    K = "<folder>/<file>.pdf"   folder in {financial_statements, pillar3},
                                relative to SOURCES_ROOT.
Everything derives from K:
    uri(K)          = gs://<bucket>/data/sources/<K>
    local_path(K)   = SOURCES_ROOT / K
    ingest_status.source_file == K            (bare key)
    doc_id_for(K)   = Path(K).stem, spaces -> underscores  (matches run_doc)

The pure path helpers do NOT import google.cloud.storage, so callers that only
need a URI (e.g. the dashboard) pay no GCS dependency. The client is imported
lazily inside the GCS-backed functions.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]          # pipeline -> findociq -> repo
FINDOCIQ = REPO / "findociq"
SOURCES_ROOT = FINDOCIQ / "data" / "sources"
GCS_SOURCES_PREFIX = "data/sources/"
DEFAULT_BUCKET = "findociq-sources-igc2026-team08-6311"


# --- pure path helpers (no GCS client) -------------------------------------
def bucket_name() -> str:
    return os.environ.get("GCS_BUCKET", DEFAULT_BUCKET)


def key_for(local_file) -> str:
    """Local path under SOURCES_ROOT -> canonical key K (posix, forward slashes)."""
    rel = os.path.relpath(Path(local_file).resolve(), SOURCES_ROOT)
    return Path(rel).as_posix()


def local_path(key: str) -> Path:
    return SOURCES_ROOT / key


def uri(key: str) -> str:
    return f"gs://{bucket_name()}/{GCS_SOURCES_PREFIX}{key}"


def gcs_uri_for_source(source_file: str) -> str:
    """ingest_status.source_file (== K after the rekey migration) -> gs:// uri."""
    return uri(source_file)


def doc_id_for(key_or_path) -> str:
    return Path(key_or_path).stem.replace(" ", "_")


# --- GCS-backed ops (lazy client) ------------------------------------------
def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(bucket_name())


def list_sources() -> list[str]:
    """Every .pdf blob under data/sources/, returned as canonical keys K."""
    out = []
    for blob in _bucket().list_blobs(prefix=GCS_SOURCES_PREFIX):
        if blob.name.endswith("/") or not blob.name.endswith(".pdf"):
            continue
        out.append(blob.name[len(GCS_SOURCES_PREFIX):])
    return sorted(out)


def exists(key: str) -> bool:
    return _bucket().blob(GCS_SOURCES_PREFIX + key).exists()


def materialize(key: str) -> Path:
    """Ensure local_path(key) exists (download from GCS if absent or size-stale);
    return the local path. Idempotent; size-verified. Raises FileNotFoundError
    if the source blob does not exist and there is no local cache."""
    dest = local_path(key)
    blob = _bucket().blob(GCS_SOURCES_PREFIX + key)
    remote_exists = blob.exists()
    if dest.exists():
        if not remote_exists:
            return dest  # remote gone/unreadable; trust the local cache
        blob.reload()
        if blob.size is None or dest.stat().st_size == blob.size:
            return dest
        # fall through: cached file size differs from remote -> re-download
    if not remote_exists:
        raise FileNotFoundError(f"no source blob for key {key!r} at {uri(key)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(dest))
    blob.reload()
    if blob.size is not None and dest.stat().st_size != blob.size:
        raise IOError(
            f"size mismatch after download of {key!r}: "
            f"local {dest.stat().st_size} != remote {blob.size}")
    return dest


def upload(local_file, key: str) -> str:
    """Upload a local PDF to gs://<bucket>/data/sources/<key>; return its uri."""
    _bucket().blob(GCS_SOURCES_PREFIX + key).upload_from_filename(str(local_file))
    return uri(key)


def resolve_to_local(arg: str) -> Path:
    """Turn a run_doc --pdf argument into a local path. Accepts a local path
    (used as-is), a gs:// uri, or a bare key K (materialized from GCS)."""
    if Path(arg).exists():
        return Path(arg).resolve()
    prefix = f"gs://{bucket_name()}/{GCS_SOURCES_PREFIX}"
    key = arg[len(prefix):] if arg.startswith(prefix) else arg
    return materialize(key)
