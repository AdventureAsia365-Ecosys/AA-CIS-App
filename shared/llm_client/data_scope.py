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

import re
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


# ── AA-1026 (S267, tech lead Q2: model first) — the vendor twin of an AWS model ──────────────────────────────────────
# "Every deployment of a model before the next model: acc3 first, then the same model on its vendor API (the Anthropic
# API for Claude, the OpenAI API for a GPT twin if one exists)." A Claude 5.x row carries both deployments itself
# (migration 306), so the gateway's per-model order already covers it. GPT-6 Luna's OpenAI API path is a separate
# catalog row (``gpt-6-luna-openai``, migration 170: "Same model as gpt-6-luna, served by the OpenAI API"), reached
# only outside AWS: its TWIN. A twin right behind its AWS model is part of that model's slot, so the route rules (the
# admin PATCH, ``llm_route_chains.violations``, migration 366's guard) accept it ahead of the route's next AWS model.

_REGION_PREFIX = re.compile(r"^(?:global|us|eu|apac|jp|au|ca)\.")
_VERSION_SUFFIX = re.compile(r"(?:-v\d+(?::\d+)?|:\d+)$")


def base_model_id(wire: Optional[str], vendor: Optional[str]) -> Optional[str]:
    """The model a wire id names, without where it is served: lower case, no Bedrock region prefix (``global.``,
    ``us.``, ...), no vendor prefix (``<vendor>.``) and no version suffix (``-v1:0``, ``:0``).
    ``global.openai.gpt-6-luna`` and the OpenAI API's ``gpt-6-luna`` are both ``gpt-6-luna``. None for no id."""
    if not isinstance(wire, str) or not wire.strip():
        return None
    w = _REGION_PREFIX.sub("", wire.strip().lower())
    v = (vendor or "").strip().lower()
    if v and w.startswith(v + "."):
        w = w[len(v) + 1:]
    return _VERSION_SUFFIX.sub("", w) or None


def vendor_twins(models: Iterable, reach: dict[str, frozenset]) -> dict[str, frozenset]:
    """Model key reached only outside AWS -> the catalog rows reachable inside AWS that are the SAME model (its AWS
    twins): same family, and the twin's ``wire_model`` names the same base model (``base_model_id``) as one of the
    AWS row's Bedrock profiles or its ``wire_model``. Today: ``gpt-6-luna-openai`` -> ``{gpt-6-luna}``. ``models``:
    catalog rows (``model_key``, ``vendor``, ``family_key``, ``api_style``, ``bedrock_profile_ids``,
    ``wire_model``); ``reach`` as in ``outside_aws_only`` (a model with no reach row counts as inside AWS)."""
    models = list(models)
    outside = outside_aws_only(reach)
    inside: dict[tuple, set] = {}
    for m in models:
        if m.model_key in outside or getattr(m, "api_style", None) == "embed":
            continue
        profiles = getattr(m, "bedrock_profile_ids", None) or {}
        for wire in [*(profiles.values() if isinstance(profiles, dict) else ()), getattr(m, "wire_model", None)]:
            base = base_model_id(wire, m.vendor)
            if base:
                inside.setdefault((m.family_key, base), set()).add(m.model_key)
    out: dict[str, frozenset] = {}
    for m in models:
        if m.model_key not in outside:
            continue
        base = base_model_id(getattr(m, "wire_model", None), m.vendor)
        twins = inside.get((m.family_key, base), set()) - {m.model_key} if base else set()
        if twins:
            out[m.model_key] = frozenset(twins)
    return out


def in_twin_slot(chain: list, i: int, twins: dict[str, frozenset]) -> bool:
    """True when ``chain[i]`` is a model reached only outside AWS sitting right behind its AWS twin (``vendor_twins``):
    the same model on its vendor API, part of that model's slot in a model-first route (tech lead S267 Q2)."""
    return 0 < i < len(chain) and chain[i - 1] in (twins or {}).get(chain[i], ())
