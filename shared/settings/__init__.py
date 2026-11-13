"""shared.settings — the CIS binding of the settings registry reader (S279 thresholds lane L1, CIS migration 407).

    from shared import settings
    settings.get("cis.publish.daily_limit_default")            # sync: the last snapshot (never the database)
    await settings.aget("cis.t2.version_usd_max")               # async: refresh first when older than 30 s

Resolution, cache and fallbacks: ``shared/settings/reader.py`` (the vendorable part). The defaults come from
``compiled_defaults.json``, generated from the migration seed by ``scripts/settings/gen_compiled_defaults.py``.
The API (lifespan) and the worker (``shared/jobs/worker._main``) install their pool and run a background refresh, so
``get`` in a sync code path sees a change within one cache window. Admin page: Platform › Settings
(``/admin/settings/registry``, api/routers/admin_settings_registry.py).

A value read here is the platform's policy: never copy it into a module constant at import time (the CI guard
tests/unit/test_s279_settings_lint.py fails on a new policy-looking constant in the governed modules).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from shared.settings.reader import Definition, Resolution, SettingsReader, coerce, parse_text, validate

COMPILED_DEFAULTS = Path(__file__).with_name("compiled_defaults.json")
KEY_PREFIX = "cis."


def load_definitions(path: Path = COMPILED_DEFAULTS) -> list[dict]:
    return list(json.loads(path.read_text(encoding="utf-8"))["definitions"])


async def _alert(key: str, problem: str, raw: Any, version: Optional[int]) -> None:
    """A stored value broke its definition: one ops e-mail per key and UTC day (the reader already logged it)."""
    pool = READER._pool
    if pool is None:
        return
    from shared import ops_alerts

    async with pool.acquire() as conn:
        await ops_alerts.enqueue_setting_alert_email(
            conn, details={"key": key, "problem": problem, "version": version, "value": str(raw)[:200]})


READER = SettingsReader(load_definitions(), key_prefix=KEY_PREFIX, alert=_alert)


def get(key: str, tenant_id: Optional[str] = None) -> Any:
    return READER.get(key, tenant_id)


async def aget(key: str, tenant_id: Optional[str] = None) -> Any:
    return await READER.aget(key, tenant_id)


def resolve(key: str, tenant_id: Optional[str] = None) -> Resolution:
    return READER.resolve(key, tenant_id)


def definition(key: str) -> Definition:
    return READER.definition(key)


def install(pool: Any) -> None:
    READER.install(pool)


def start_refresher(pool: Any) -> Optional[asyncio.Task]:
    """Install ``pool`` and refresh in the background (None under ENVIRONMENT=test: tests read the defaults)."""
    READER.install(pool)
    if os.environ.get("ENVIRONMENT") == "test":
        return None
    return asyncio.create_task(READER.run_refresher(), name="settings-refresher")


async def ensure_fresh(pool: Any) -> bool:
    """Re-read the approved values through ``pool`` now; call it at the start of every paid-run entry point that gets
    a pool (S279 review). A process that never ran ``start_refresher`` (an ECS exec script with its own asyncpg pool:
    the question top-up, an R2 / shadow replay) would otherwise serve the compiled defaults for good, so a cap the
    administrator lowered would be ignored. True when the snapshot is fresh; False when the defaults / last good
    snapshot stay in force (no pool, DB error, the 407 tables missing). A no-op under ENVIRONMENT=test, like the
    refresher (unit tests read the defaults or monkeypatch ``get``)."""
    if pool is None or os.environ.get("ENVIRONMENT") == "test":
        return False
    try:
        return await READER.refresh(force=True, pool=pool)
    except Exception as exc:  # a malformed row never breaks the run: the defaults / last good stay in force
        READER.last_error = f"{type(exc).__name__}: {exc}"[:300]
        return False


async def stop_refresher(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = ["COMPILED_DEFAULTS", "Definition", "KEY_PREFIX", "READER", "Resolution", "aget", "coerce", "definition",
           "ensure_fresh", "get", "install", "load_definitions", "parse_text", "resolve", "start_refresher",
           "stop_refresher", "validate"]
