"""AA-1126 l16 (S283, CIS 447) — which AI spend rules this CIS environment follows: the platform setting
``cis.spend.environment`` (dev | prod; Platform › Settings, area spend, maker-checker, its EN + VI guide in CIS 447).

- ``dev`` (the default, today's behaviour): every hard stop stays. A tenant AI budget that cannot be read stops that
  tenant's Content studio writing until it can (AA-642 A8, ``shared/llm_client/tenant_budget.check``).
- ``prod`` (tech lead policy S283, pay per use): only a tenant's own plan allowance stops its AI; a budget that cannot
  be read degrades with a warning (log + one admin bell) and the call goes on.

The value is read per call through the settings reader (its cache, then the compiled default when the database cannot
be read), so flipping it on the admin page takes effect within the key's cache window, with no deploy. Any read error
answers ``dev``: the stricter rules, never a silent loosening.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()

KEY = "cis.spend.environment"
DEV = "dev"
PROD = "prod"


def spend_environment() -> str:
    """``dev`` or ``prod`` (``cis.spend.environment``); ``dev`` when the setting cannot be read."""
    try:
        from shared import settings  # lazy: keeps the gateway import-light
        value = settings.get(KEY)
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("spend_environment_unreadable", error=str(e)[:200])
        return DEV
    return PROD if str(value or "").strip().lower() == PROD else DEV


def is_prod() -> bool:
    return spend_environment() == PROD
