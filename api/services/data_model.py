"""AA-894 scope 2 — the live data model (ERD) for the admin page /admin/data-model.

The nightly DB Schema Dump (AA-CIS-Infra, db-schema-dump.yml) writes the ERD model `erd.json` (format 1, built by
db/gen_erd.py: schemas, tables, columns with full types, PK, unique keys, FKs, DM-6.1 tags, owners; no row data) to
the bronze bucket:

  <prefix>/latest.json     the model of the last dump
  <prefix>/previous.json   the model before the last structural change (absent until the model has changed once)

<prefix> is DATA_MODEL_S3_PREFIX (default "data-model/erd"); the bucket is shared/aws_client/bronze.py. The CIS task
role may read exactly those two objects (AA-CIS-Infra modules/ecs ReadErdModel).

This repo is public: it ships the reader and the renderer only. Table and column names exist only in the S3 object
and in the response of the admin-only route.

load_data_model() returns {"model", "previous", "diff", "loaded_at", "cache_ttl_s"}, cached in this process for
CACHE_TTL_S. A missing latest.json raises DataModelMissing (route: 404); any other read or parse failure raises
DataModelUnavailable (route: 503). A missing or unreadable previous.json only leaves `diff` empty (None).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
DEFAULT_PREFIX = "data-model/erd"
CACHE_TTL_S = 300
# ?refresh=1 re-reads S3 at most this often per process (the page's Reload button), so it can not drive an S3 loop.
MIN_REFRESH_S = 30
MAX_OBJECT_BYTES = 20 * 1024 * 1024   # erd.json is ~650 KB today; refuse anything absurd rather than load it


class DataModelMissing(Exception):
    """latest.json does not exist (the dump has not uploaded a model yet)."""


class DataModelUnavailable(Exception):
    """latest.json could not be read or is not a format-1 model."""


def s3_prefix() -> str:
    return (os.environ.get("DATA_MODEL_S3_PREFIX", "").strip() or DEFAULT_PREFIX).strip("/")


def _s3_client():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))


def _bucket() -> str:
    from shared.aws_client.bronze import bronze_bucket

    return bronze_bucket()


def _is_missing(err: Exception) -> bool:
    response = getattr(err, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in ("NoSuchKey", "404", "NotFound")


def _read_json(client, bucket: str, key: str) -> Optional[dict]:
    """The object as a dict; None when it does not exist. Raises on any other failure."""
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception as err:  # botocore ClientError, or a fake in tests
        if _is_missing(err):
            return None
        raise
    size = obj.get("ContentLength")
    if isinstance(size, int) and size > MAX_OBJECT_BYTES:
        raise ValueError(f"{key} is {size} bytes, over the {MAX_OBJECT_BYTES} byte limit")
    body = obj["Body"].read(MAX_OBJECT_BYTES + 1)
    if len(body) > MAX_OBJECT_BYTES:
        raise ValueError(f"{key} is over the {MAX_OBJECT_BYTES} byte limit")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"{key} is not a JSON object")
    return data


def _valid_model(model: Any) -> bool:
    return (isinstance(model, dict) and model.get("format") == SUPPORTED_FORMAT
            and isinstance(model.get("schemas"), list) and isinstance(model.get("edges"), list))


# ── diff ────────────────────────────────────────────────────────────────────────────────────────────

def _tables(model: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sch in model.get("schemas") or []:
        for t in sch.get("tables") or []:
            out[f"{sch.get('schema')}.{t.get('name')}"] = t
    return out


def _edge_key(e: dict) -> str:
    return f"{e.get('from')}:{e.get('name')}"


def _column_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old.get("type") != new.get("type"):
        changes.append(f"type {old.get('type')} -> {new.get('type')}")
    if bool(old.get("not_null")) != bool(new.get("not_null")):
        changes.append("NOT NULL" if new.get("not_null") else "nullable")
    if old.get("tag") != new.get("tag"):
        changes.append(f"tag {old.get('tag') or 'none'} -> {new.get('tag') or 'none'}")
    for flag, label in (("pk", "PK"), ("fk", "FK"), ("uk", "UK")):
        if bool(old.get(flag)) != bool(new.get(flag)):
            changes.append(f"{label} {'added' if new.get(flag) else 'removed'}")
    return changes


def _table_diff(old: dict, new: dict) -> Optional[dict]:
    old_cols = {c.get("name"): c for c in old.get("columns") or []}
    new_cols = {c.get("name"): c for c in new.get("columns") or []}
    added = [n for n in new_cols if n not in old_cols]
    removed = [n for n in old_cols if n not in new_cols]
    changed = []
    for name in new_cols:
        if name in old_cols:
            ch = _column_changes(old_cols[name], new_cols[name])
            if ch:
                changed.append({"name": name, "changes": ch})
    old_fks = {fk.get("name") for fk in old.get("foreign_keys") or []}
    new_fks = {fk.get("name") for fk in new.get("foreign_keys") or []}
    out: dict[str, Any] = {}
    if added:
        out["columns_added"] = added
    if removed:
        out["columns_removed"] = removed
    if changed:
        out["columns_changed"] = changed
    if (old.get("primary_key") or []) != (new.get("primary_key") or []):
        out["primary_key"] = {"from": old.get("primary_key") or [], "to": new.get("primary_key") or []}
    if new_fks - old_fks:
        out["fks_added"] = sorted(new_fks - old_fks)
    if old_fks - new_fks:
        out["fks_removed"] = sorted(old_fks - new_fks)
    if old.get("rls") != new.get("rls") or old.get("rls_forced") != new.get("rls_forced"):
        out["rls"] = {"from": [bool(old.get("rls")), bool(old.get("rls_forced"))],
                      "to": [bool(new.get("rls")), bool(new.get("rls_forced"))]}
    return out or None


def diff_models(previous: dict, latest: dict) -> dict:
    """What changed from `previous` to `latest`: tables added / removed / changed (per table: columns added, removed,
    changed; PK; FKs; RLS) and FK edges added / removed. Names only, sorted; no counts of rows (there are none)."""
    old_t, new_t = _tables(previous), _tables(latest)
    changed = {}
    for key in sorted(set(old_t) & set(new_t)):
        d = _table_diff(old_t[key], new_t[key])
        if d:
            changed[key] = d
    old_e = {_edge_key(e): e for e in previous.get("edges") or []}
    new_e = {_edge_key(e): e for e in latest.get("edges") or []}
    return {
        "tables_added": sorted(set(new_t) - set(old_t)),
        "tables_removed": sorted(set(old_t) - set(new_t)),
        "tables_changed": changed,
        # "<schema.table>:<constraint>" (a constraint name is unique per table, not per database)
        "edges_added": sorted(set(new_e) - set(old_e)),
        "edges_removed": sorted(set(old_e) - set(new_e)),
    }


# ── cache + load ────────────────────────────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()


def reset_cache() -> None:
    _cache["value"] = None
    _cache["at"] = 0.0


def _build(client_factory: Callable[[], Any]) -> dict:
    client = client_factory()
    bucket, prefix = _bucket(), s3_prefix()
    try:
        latest = _read_json(client, bucket, f"{prefix}/latest.json")
    except Exception as err:
        logger.warning("data_model_read_failed", key=f"{prefix}/latest.json", error=type(err).__name__)
        raise DataModelUnavailable("The data model could not be read from storage") from err
    if latest is None:
        raise DataModelMissing("No data model has been published yet (DB Schema Dump ERD upload)")
    if not _valid_model(latest):
        raise DataModelUnavailable(f"The stored data model is not format {SUPPORTED_FORMAT}")

    previous_meta, diff = None, None
    try:
        previous = _read_json(client, bucket, f"{prefix}/previous.json")
    except Exception as err:   # no diff is not worth failing the page for
        logger.warning("data_model_previous_read_failed", error=type(err).__name__)
        previous = None
    if _valid_model(previous) and previous.get("fingerprint") != latest.get("fingerprint"):
        previous_meta = {"fingerprint": previous.get("fingerprint"), "generated_at": previous.get("generated_at"),
                         "migrations_latest_per_owner": previous.get("migrations_latest_per_owner") or {}}
        diff = diff_models(previous, latest)
    return {"model": latest, "previous": previous_meta, "diff": diff,
            "loaded_at": datetime.now(timezone.utc).isoformat(), "cache_ttl_s": CACHE_TTL_S}


async def load_data_model(refresh: bool = False, client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """The cached view (CACHE_TTL_S); `refresh` re-reads S3 when the cached copy is older than MIN_REFRESH_S."""
    now = time.monotonic()
    value, at = _cache["value"], _cache["at"]
    max_age = MIN_REFRESH_S if refresh else CACHE_TTL_S
    if value is not None and now - at < max_age:
        return value
    async with _lock:
        value, at = _cache["value"], _cache["at"]
        if value is not None and time.monotonic() - at < max_age:
            return value
        value = await asyncio.to_thread(_build, client_factory or _s3_client)
        _cache["value"], _cache["at"] = value, time.monotonic()
        return value
