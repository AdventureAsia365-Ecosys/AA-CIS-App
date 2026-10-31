"""AA-884 (S241 GĐ7) — what /health and /health/ready report, and the API's own build identity.

- ``git_sha()``: the commit the running image was built from (Dockerfile ARG GIT_SHA → ENV APP_GIT_SHA, set by
  deploy-dev.yml), the same value Sentry uses as its release. ``"unknown"`` outside an image.
- ``readiness(pool)``: one ``SELECT`` that both proves the DB answers and reads the highest **numeric** CIS migration
  in ``shared.schema_versions`` (the table also holds ``bk-*``, ``tp-*`` and ``rl-*`` keys, which are never compared).
  Ready = DB answered and that number >= ``REQUIRED_CIS_SCHEMA``, the newest migration this code needs. Raise the
  constant in the same PR as code that needs a newer migration, so the deploy smoke and the Platform Apps page show a
  missing apply instead of a 500 on first use. A number used out of order (applied after a higher one, e.g. a reserved
  number) goes into ``OUT_OF_ORDER_CIS``: while it is not registered the reported version is capped just below it, so
  max() alone cannot hide a missing apply (AA-920: 297 applied after 299).

AA-993 M4a (root ADR 0017 d7): the body also lists ``registered_keys``, every numeric CIS version registered above
``LEGACY_CIS_BASELINE`` (below it the hand-applied legacy files left gaps: 1-24, 62-64, 158, 232, 252, 263), and
readiness requires every key of ``REQUIRED_CIS_KEYS`` (each number from the baseline up to ``REQUIRED_CIS_SCHEMA``),
like Booking and Partner, so a gap under the required key is ``schema_behind`` with ``missing_schema_versions``. The
Deploy Dev ``schema-gate`` (M4b) reads ``registered_keys`` from the running task. The keys are a second read; if it
fails, readiness falls back to the version rule above and the body has no ``registered_keys``.

``/health/ready`` is public (the ALB / CI probe shape), so the answer is cached in-process for
``READY_CACHE_SECONDS`` (failures too): a burst of probes reaches RDS at most once per window per task.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import structlog

from shared.sentry_init import release

logger = structlog.get_logger()

# The newest CIS migration this code needs: 300 (AA-795 — the T2 job's save UPDATE names
# gold_aa_internal.tenant_tour_versions.judge_breakdown; without the column every T2 save fails). Before: 297 (AA-920
# WM-25 — the same save names tenant_tour_versions.generation_meta); 293 (AA-665
# C2 — every shared.dfs_call_log write names job_id; without the column each DFS cost row is lost, the write being
# fire-and-forget); 287 (AA-919 WM-18 — the writers resolve
# their writing-memory release from shared.writing_memory_release / writing_memory_writer on every call (failing open
# without them) and /admin/writing-memory/releases reads and writes them); 280 (AA-917 — the writing-memory jobs and
# /admin/writing-memory read and write shared.writing_rule; 277, AA-916: the admin A1 field edit and both sentence
# applies write shared.writing_signal inside their own transaction; without the table they fail).
# 301 (AA-995 — the photo route reads and the photo sync writes shared.place_photo.s3_key_medium; without it every
# GET /content/photos/{id} fails). 302 (AA-814 — /admin/facts, /v1/facts and the T9 facts read name
# acp_shared.facts.kind / valid_until / updated_by, and the tenant DELETE needs aa_app_user's DELETE grant). 303 (AA-671
# S257 — the photo sync upsert, the assign route and the photo_destination_match job write
# shared.place_photo.destination_match_source; without it the sync fails). 305 (AA-1003 — kpi_snapshot_daily,
# report_generate and /admin/reports read and write shared.kpi_daily and shared.report_run; strict prefix: 304 too).
# 313 (AA-921 WM-27 — /admin/writing-memory/exemplars and a rules draft read and write shared.writing_exemplar and
# shared.writing_memory_release_exemplar; the writers' resolver reads the links (failing open without them); strict
# prefix: 306-312 too). 314 (AA-922 WM-30 — the weekly KPI job, /admin/writing-memory/metrics, keeps and the
# disagreement queue read and write shared.writing_memory_kpi_report / _keep / _disagreement). 315 (AA-923 WM-32 — the
# T2 version and A1 rewrite label routes of /admin/content-quality read and write acp_shared.content_label).
# 319 (AA-804 ledger v2 — the readers of shared.llm_call_log name its app / is_shadow / status columns: platform apps
# and cost, the KPI and money reports, the planner, tenant 360 cost, LLM availability; strict prefix: 316-318 too).
# 322 (AA-1017 registry v2 — the catalog read names llm_model_catalog.family (320), the deployment read
# llm_model_deployment.api_style (320), the gateway reads shared.stage_model_params (320), the connection read and the
# Jev / DataForSEO / Geoapify settings read the 322 columns and rows; each read falls back without them, so a missing
# apply degrades to today's behaviour, and readiness says so; strict prefix: 320 and 321 too).
REQUIRED_CIS_SCHEMA = 322
# Versions this code needs that were registered after a higher one (reserved numbers): 297 (AA-920 WM-25) was reserved
# in S255 and applied after 299. max() is already 299 without it, so the version reported is capped below the lowest
# listed version that is not registered (297 missing -> 296 -> "schema_behind").
OUT_OF_ORDER_CIS = (297,)
READY_CACHE_SECONDS = 5.0
# AA-993 M4a: every number above this one is a registered CIS migration on Dev, number for number (298 is reserved and
# not applied yet, above REQUIRED_CIS_SCHEMA). Below it are the legacy gaps; they are never listed nor required.
LEGACY_CIS_BASELINE = 263
# Every CIS key this code needs: raising REQUIRED_CIS_SCHEMA requires every number up to it (strict prefix, ADR 0017).
REQUIRED_CIS_KEYS = tuple(range(LEGACY_CIS_BASELINE + 1, REQUIRED_CIS_SCHEMA + 1))

# The highest numeric CIS version, capped below the lowest OUT_OF_ORDER_CIS version that is not registered ($1).
SCHEMA_VERSION_SQL = """
    SELECT CASE WHEN m.missing IS NULL THEN m.head ELSE least(m.head, m.missing - 1) END
      FROM (SELECT (SELECT max(version::int) FROM shared.schema_versions WHERE version ~ '^[0-9]+$') AS head,
                   (SELECT min(r) FROM unnest($1::int[]) AS r
                     WHERE NOT EXISTS (SELECT 1 FROM shared.schema_versions s WHERE s.version = r::text)) AS missing) m
"""

# The numeric CIS versions registered above LEGACY_CIS_BASELINE ($1), in number order.
REGISTERED_KEYS_SQL = """
    SELECT coalesce(array_agg(version::int ORDER BY version::int), '{}')
      FROM shared.schema_versions WHERE version ~ '^[0-9]+$' AND version::int > $1
"""

_cache: dict[str, Any] = {"at": None, "value": None}


def git_sha() -> str:
    return release()


async def _probe(pool: Any) -> tuple[int, dict]:
    sha = git_sha()
    if pool is None:
        return 503, {"status": "unavailable", "db": "not_ready", "schema_version": None,
                     "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    try:
        version: Optional[int] = await pool.fetchval(SCHEMA_VERSION_SQL, list(OUT_OF_ORDER_CIS))
    except Exception as e:  # any DB failure is "not ready", never a 500
        logger.warning("health_ready_db_failed", error=repr(e)[:200])
        return 503, {"status": "unavailable", "db": "error", "schema_version": None,
                     "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    ok = version is not None and version >= REQUIRED_CIS_SCHEMA
    keys = await _registered_keys(pool)
    missing = [str(k) for k in REQUIRED_CIS_KEYS if keys is not None and k not in keys]
    ok = ok and not missing
    body = {"status": "ok" if ok else "schema_behind", "db": "ok",
            "schema_version": None if version is None else str(version),
            "required_schema": str(REQUIRED_CIS_SCHEMA), "git_sha": sha}
    if keys is not None:
        body["registered_keys"] = [str(k) for k in keys]
        body["keys_baseline"] = str(LEGACY_CIS_BASELINE)
    if missing:
        body["missing_schema_versions"] = missing
    return (200 if ok else 503), body


async def _registered_keys(pool: Any) -> Optional[list[int]]:
    """The registered numeric CIS keys above LEGACY_CIS_BASELINE, or None when the read fails or answers something
    other than a list (readiness then keeps the version rule alone)."""
    try:
        keys = await pool.fetchval(REGISTERED_KEYS_SQL, LEGACY_CIS_BASELINE)
    except Exception as e:  # never a 500, never a flip to 503 on this read alone
        logger.warning("health_ready_keys_failed", error=repr(e)[:200])
        return None
    if not isinstance(keys, (list, tuple)):
        return None
    return sorted(int(k) for k in keys)


async def readiness(pool: Any, *, now: Optional[float] = None) -> tuple[int, dict]:
    """(HTTP status, body) for /health/ready; cached for READY_CACHE_SECONDS."""
    t = time.monotonic() if now is None else now
    at = _cache["at"]
    if at is not None and t - at < READY_CACHE_SECONDS and _cache["value"] is not None:
        return _cache["value"]
    value = await _probe(pool)
    _cache["at"], _cache["value"] = t, value
    return value


def reset_cache() -> None:
    _cache["at"], _cache["value"] = None, None
