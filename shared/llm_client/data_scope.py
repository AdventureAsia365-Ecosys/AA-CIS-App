"""AA-1002 (gateway slice S5) — data scopes: through which connections a stage's content may be sent.

Every connection carries a ``data_scope`` (``shared.llm_provider_connection``, migration 304): ``aws_only`` (a Bedrock
account: the request stays inside AWS) or ``vendor_direct`` (a vendor API: the request leaves AWS, e.g. the OpenAI
platform API, the Anthropic API). Every stage carries ``allowed_scopes`` (``shared.llm_role_config``, migration 311):
the scopes its content may use. The gateway calls a deployment only when its connection's scope is allowed for the
stage (``vendor_route.deployment_plan``, ``LLMClient._call_openai``); the admin refuses a route model the stage could
never call (``api/routers/admin_llm_ops.py``).

This replaces the S235 / S3 constants (``_OPENAI_LAST_RESORT_ONLY`` in the admin, the "vendor_direct only as the
first row" rule of the plan) with data, without changing a call: migration 311 backfills ``{aws_only, vendor_direct}``
for exactly the stages whose route names a model reached only outside AWS (``backfill_scopes``: the judge / validate
stages with ``gpt-6-luna-openai`` as their last resort) and ``{aws_only}`` for the rest
(``tests/unit/test_aa1002_s3_routes_snapshot.py`` runs every Dev stage with the backfilled scopes).

``None`` means "no stage policy": the column is NULL or not migrated, the stage has no row (an ``adhoc_*`` stage, a
connection test). The gateway then keeps the S3 rule (a ``vendor_direct`` row only as a model's FIRST enabled
deployment, never a fallback behind an ``aws_only`` one) and the S235 catalog gate alone, i.e. exactly the pre-S5
behaviour.
"""
from __future__ import annotations

from typing import Iterable, Optional

import structlog

logger = structlog.get_logger()

AWS_ONLY = "aws_only"
VENDOR_DIRECT = "vendor_direct"
# The values of the data_scope CHECK (migration 304, connections.DATA_SCOPES) and of migration 311's allowed_scopes
# CHECK, in that order (tests/unit/test_aa1002_s5_data_scope.py). No registry import: role_config reads this module.
DATA_SCOPES: tuple[str, ...] = (AWS_ONLY, VENDOR_DIRECT)
ALL_SCOPES: tuple[str, ...] = DATA_SCOPES
AWS_ONLY_SCOPES: tuple[str, ...] = (AWS_ONLY,)


def normalize(value) -> Optional[tuple[str, ...]]:
    """A stored ``allowed_scopes`` value as a tuple in ``DATA_SCOPES`` order, or None for "no stage policy" (NULL, not
    a list: e.g. a test double). Fails closed: an unknown scope is dropped, and a list with no known scope left is
    ``(aws_only,)`` (the CHECK of migration 311 makes both impossible in the table)."""
    if value is None or isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        return None
    known = tuple(s for s in DATA_SCOPES if s in value)
    unknown = [s for s in value if s not in DATA_SCOPES]
    if unknown:
        logger.warning("llm_stage_scope_unknown_dropped", unknown=[str(s)[:40] for s in unknown][:5])
    return known or AWS_ONLY_SCOPES


def allows(allowed_scopes: Optional[tuple[str, ...]], data_scope: str) -> bool:
    """True when content of a stage with ``allowed_scopes`` may be sent through a ``data_scope`` connection. None (no
    stage policy) allows everything here: the caller then applies its pre-S5 rule."""
    return allowed_scopes is None or data_scope in allowed_scopes


def outside_aws_only(reach: dict[str, frozenset]) -> frozenset:
    """The model keys whose every deployment is on a connection outside AWS (no ``aws_only`` deployment), from
    ``reach`` = model key -> the data scopes of the connections its deployments are on."""
    return frozenset(k for k, scopes in reach.items() if scopes and AWS_ONLY not in scopes)


def backfill_scopes(route_keys: Iterable[str], outside_keys: Iterable[str]) -> tuple[str, ...]:
    """Migration 311's backfill rule, in Python (SAFE_DEFAULTS and the route snapshot use it): a stage whose route
    (primary, fallbacks, shadow) names a model reached only outside AWS gets both scopes, any other stage
    ``(aws_only,)``."""
    outside = set(outside_keys)
    return ALL_SCOPES if any(k in outside for k in route_keys if k) else AWS_ONLY_SCOPES
