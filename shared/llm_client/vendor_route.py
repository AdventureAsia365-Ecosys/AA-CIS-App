"""AA-1002 (gateway slice S2) — when a catalog model is served by a vendor API connection instead of today's path.

The only reader of the connection registry (``connections.py``) and the deployments (``deployments.py``) on the
call path. ``LLMClient`` asks it once per catalog-key attempt (both tables are cached in process, 5 minutes):

- ``vendor_deployment_for(model)``: a model's enabled deployments whose connection is enabled, lowest ``priority``
  first (``connection_key`` breaks ties). When the FIRST of them is on an ``anthropic`` connection, the model is
  called through the Anthropic Messages adapter; in every other case (no row, the table not migrated, the DB down,
  the Anthropic row disabled or its connection disabled, a Bedrock deployment ahead of it) the answer is None and
  the gateway takes today's path, byte for byte (tests/unit/test_aa1002_s2_vendor_route.py). Slice S2 calls only
  Anthropic connections through the registry: a Bedrock or OpenAI deployment ahead means "today's code", not a
  registry-driven call (slice S3 spreads a route over every deployment).
- ``pinned_deployment(key)``: an explicit ``model_tier`` of the form ``<model_key>@<connection_key>`` (a probe or a
  connection test, never a stage route: a stored route names catalog keys only) calls that one deployment, provided
  the deployment and its connection are enabled.

Only Anthropic-vendor catalog rows may run on an Anthropic connection; legacy keys (``haiku`` / ``sonnet`` /
``gpt-4.1``) never do (slice S3 aliases them to catalog models, see ``deployments.alias_targets_sync``).

Slice S3 adds ``deployment_plan(model)``: the ordered deployments a catalog model is tried on (``LLMClient`` with
``LLM_DEPLOYMENT_ROUTING`` on, the default; ``embed``; the legacy alias path). See its docstring for the rules; with
the rows migrations 304 / 306 seeded it gives exactly today's single call per model (acc3, acc2 for an embed row,
openai-direct for an OpenAI API row), proved for every Dev stage by ``tests/unit/test_aa1002_s3_routes_snapshot.py``.

Slice S4 plugs the circuit breaker (``breaker.py``) into the plan: a deployment whose circuit is open is left out (see
``deployment_plan``); the provider calls themselves are wrapped by ``LLMClient._call_target`` / ``embed``.

Slice S5 makes the data policy per stage (``data_scope.py``): ``deployment_plan`` / ``vendor_deployment_for`` take the
stage's ``allowed_scopes`` and leave out a deployment whose connection's ``data_scope`` the stage does not allow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import structlog

from . import breaker, connections, data_scope, deployments
from .catalog import LEGACY_KEYS, CatalogModel

logger = structlog.get_logger()

VENDOR_KINDS = ("anthropic",)
PIN_SEPARATOR = "@"


@dataclass(frozen=True)
class VendorDeployment:
    deployment: deployments.ModelDeployment
    connection: connections.ProviderConnection


def _first_enabled(model_key: str) -> Optional[VendorDeployment]:
    for d in deployments.deployments_for_sync(model_key):          # enabled only, priority order
        conn = connections.get_connection_sync(d.connection_key)
        if conn is None or not conn.enabled:
            continue
        return VendorDeployment(d, conn)
    return None


def _usable(model: CatalogModel, found: Optional[VendorDeployment]) -> Optional[VendorDeployment]:
    if found is None or found.connection.kind not in VENDOR_KINDS:
        return None
    if model.model_key in LEGACY_KEYS or model.vendor != "anthropic":
        logger.warning("llm_vendor_deployment_ignored", model=model.model_key, vendor=model.vendor,
                       connection=found.connection.connection_key,
                       reason="legacy key" if model.model_key in LEGACY_KEYS else "not an Anthropic model")
        return None
    return found


def vendor_deployment_for(model: CatalogModel,
                          allowed_scopes: Optional[tuple] = None) -> Optional[VendorDeployment]:
    """The Anthropic deployment to call for ``model``, or None for today's path. AA-1002 (S5): None as well when the
    stage's ``allowed_scopes`` (None = no stage policy) do not allow that connection's data scope. Never raises."""
    try:
        found = _usable(model, _first_enabled(model.model_key))
        if found is not None and not data_scope.allows(allowed_scopes, found.connection.data_scope):
            logger.info("llm_deployment_skip", model=model.model_key, connection=found.connection.connection_key,
                        reason=f"data scope {found.connection.data_scope} is not allowed for the stage")
            return None
        return found
    except Exception as e:  # noqa: BLE001 — a registry problem must never break today's path
        logger.warning("llm_vendor_route_failed", model=model.model_key, error=str(e)[:200])
        return None


def split_pin(key: str) -> Optional[tuple[str, str]]:
    """``(model_key, connection_key)`` for a ``model@connection`` key, else None."""
    if not key or PIN_SEPARATOR not in key:
        return None
    model_key, _, connection_key = key.partition(PIN_SEPARATOR)
    return (model_key, connection_key) if model_key and connection_key else None


def pinned_deployment(model: CatalogModel, connection_key: str) -> VendorDeployment:
    """The enabled deployment of ``model`` on ``connection_key`` (an Anthropic connection, enabled). Raises
    RuntimeError naming what is missing."""
    rows = [d for d in deployments.deployments_for_sync(model.model_key, enabled_only=False)
            if d.connection_key == connection_key]
    if not rows:
        raise RuntimeError(f"{model.model_key!r} has no deployment on connection {connection_key!r}")
    if not rows[0].enabled:
        raise RuntimeError(f"the deployment of {model.model_key!r} on {connection_key!r} is disabled")
    conn = connections.get_connection_sync(connection_key)
    if conn is None or not conn.enabled:
        raise RuntimeError(f"connection {connection_key!r} is missing or disabled")
    found = _usable(model, VendorDeployment(rows[0], conn))
    if found is None:
        raise RuntimeError(f"connection {connection_key!r} ({conn.kind}) cannot serve {model.model_key!r} in S2")
    return found


# ── slice S3: spread a model over its deployments ──────────────────────────────────────────────────────────────────

# The connection kinds the gateway has an adapter for, per catalog api_style (anything else is skipped, logged).
_CALLABLE: dict[str, frozenset] = {
    "bedrock": frozenset({"converse", "anthropic_native", "embed"}),
    "anthropic": frozenset({"converse", "anthropic_messages"}),
    "openai": frozenset({"openai_chat"}),
}
_BEDROCK_AUTH = ("assume_role", "task_role")


def _never_open(connection_key: str, model_key: str) -> bool:
    return False


# Slice S4: the deployment circuit breaker (breaker.py, state in shared.llm_provider_health).
# ``LLM_CIRCUIT_BREAKER=off`` makes it answer False, i.e. the S3 behaviour (``_never_open``). Tests may replace it.
circuit_open: Callable[[str, str], bool] = breaker.is_open


class DeploymentPlan(list):
    """The ordered deployments a model is tried on (a list of VendorDeployment), plus ``circuit_skips``: the
    (connection_key, error class that opened it) of each deployment left out only because its circuit is open. An
    empty plan with skips is "the model was not called because of the breaker" (``breaker.circuit_error``), which keeps
    the class of the refusal that opened the circuit (an account-level one still ends the job at once)."""

    def __init__(self, items=(), circuit_skips=()):
        super().__init__(items)
        self.circuit_skips: list[tuple[str, Optional[str]]] = list(circuit_skips)


def _skip(model: CatalogModel, d: deployments.ModelDeployment, reason: str) -> None:
    logger.info("llm_deployment_skip", model=model.model_key, connection=d.connection_key, reason=reason)


_WIRE_DRIFT_SEEN: set[tuple[str, str, str, str]] = set()


def _warn_wire_drift(model: CatalogModel, d: deployments.ModelDeployment, conn: connections.ProviderConnection) -> None:
    """Review S259: CIS calls the deployment's ``wire_id``; TripPlanner (until S8) still calls the catalog's
    ``bedrock_profile_ids[connection]``. When both exist and differ, the two apps call different profiles with no error
    anywhere: say so once per process and combination (the call still uses the deployment row, which is the registry's
    answer). Nothing to compare when the catalog has no profile for that connection (e.g. a new account's row)."""
    if conn.kind != "bedrock":
        return
    profile = (model.bedrock_profile_ids or {}).get(d.connection_key)
    if not profile or profile == d.wire_id:
        return
    seen = (model.model_key, d.connection_key, d.wire_id, str(profile))
    if seen in _WIRE_DRIFT_SEEN:
        return
    _WIRE_DRIFT_SEEN.add(seen)
    logger.warning("llm_deployment_wire_drift", model=model.model_key, connection=d.connection_key,
                   wire_id=d.wire_id, catalog_profile=str(profile), origin=d.origin)


def _callable(model: CatalogModel, conn: connections.ProviderConnection) -> Optional[str]:
    """None when the gateway has a way to call ``model`` on ``conn``, else why not."""
    if model.api_style not in _CALLABLE.get(conn.kind, frozenset()):
        return f"no {conn.kind} adapter for api_style {model.api_style}"
    if conn.kind == "bedrock":
        # The STS / task-role session is built from the row at call time and fails loudly when a field is missing.
        return None if conn.auth_mode in _BEDROCK_AUTH else f"bedrock auth_mode {conn.auth_mode} is not supported yet"
    if conn.kind == "anthropic" and (model.model_key in LEGACY_KEYS or model.vendor != "anthropic"):
        return "legacy key" if model.model_key in LEGACY_KEYS else "not an Anthropic model"
    if conn.kind == "openai" and model.provider != "openai":
        return "an OpenAI API connection serves only provider='openai' catalog rows (S235)"
    problems = conn.policy_problems()           # an API-key connection: secret prefix, base_url allowlist (S1)
    if problems:
        return "connection policy: " + "; ".join(problems)
    return None


def deployment_plan(model: CatalogModel, allowed_scopes: Optional[tuple] = None) -> Optional[list[VendorDeployment]]:
    """AA-1002 (S3) — where ``model`` is called, in order. The model's deployment rows (``shared.llm_model_deployment``,
    lowest ``priority`` first, ``connection_key`` breaks ties) that the gateway can call — a Bedrock connection
    (assume_role / task_role) for converse / anthropic_native / embed rows, an Anthropic connection for Anthropic
    non-legacy rows, an OpenAI connection for provider='openai' rows only (the S235 gate stays keyed on the catalog
    row), an API-key connection without a policy problem — are its CALLABLE rows. Of those, the plan keeps

    - an enabled deployment on an enabled connection;
    - data scope (slice S5, ``data_scope.py``): with the stage's ``allowed_scopes``, a connection whose ``data_scope``
      is in them (an ``aws_only`` stage never leaves AWS, whatever the priorities; a stage that allows
      ``vendor_direct`` may fall back to a vendor API behind acc3). With no stage policy (``allowed_scopes`` None: the
      column not migrated, a stage with no row) the S3 rule: a ``vendor_direct`` connection only when it is the FIRST
      enabled callable row (the admin deliberately put a vendor API ahead, S2's rule), never a same-model fallback
      behind an ``aws_only`` one (migration 306 seeds enabled Anthropic API rows behind acc3 for every Anthropic
      model, tenant ones included). Decided before the circuit check, so an open acc3 circuit never promotes a row
      the rule leaves out;
    - one whose circuit is not open (``circuit_open``: slice S4's breaker, ``breaker.is_open``; a skipped one is named
      in the returned ``DeploymentPlan.circuit_skips``).

    Returns None — "the registry does not describe this model, take today's path" — when the connection registry has
    no rows (unreadable, not migrated, DB down) or the model has no callable row at all (none, or only rows the gateway
    cannot use: they are ignored, as S2 ignored them). Returns [] when callable rows exist but none is usable now
    (disabled, connection disabled, circuit open): the registry is authoritative once it describes the model, the
    caller fails that model and the route moves on. Never raises."""
    try:
        if not connections.registry_rows_sync() or not deployments.has_rows_sync(model.model_key):
            return None
        callable_rows: list[VendorDeployment] = []
        for d in deployments.deployments_for_sync(model.model_key, enabled_only=False):   # priority order
            conn = connections.get_connection_sync(d.connection_key)
            if conn is None:
                _skip(model, d, "connection not in the registry")
                continue
            why = _callable(model, conn)
            if why:
                _skip(model, d, why)
                continue
            if d.enabled:
                _warn_wire_drift(model, d, conn)
            callable_rows.append(VendorDeployment(d, conn))
        if not callable_rows:
            return None
        candidates: list[VendorDeployment] = []
        for c in callable_rows:
            if not (c.deployment.enabled and c.connection.enabled):
                continue
            scope = c.connection.data_scope
            if allowed_scopes is not None:
                if not data_scope.allows(allowed_scopes, scope):
                    _skip(model, c.deployment, f"data scope {scope} is not allowed for the stage")
                    continue
            elif candidates and scope != data_scope.AWS_ONLY:
                _skip(model, c.deployment,
                      f"{scope} is never a fallback behind another deployment (no stage data scope)")
                continue
            candidates.append(c)
        plan: list[VendorDeployment] = []
        skips: list[tuple[str, Optional[str]]] = []
        for c in candidates:
            if circuit_open(c.connection.connection_key, model.model_key):
                _skip(model, c.deployment, "circuit open")
                skips.append((c.connection.connection_key,
                              breaker.open_class(c.connection.connection_key, model.model_key)))
                continue
            plan.append(c)
        return DeploymentPlan(plan, skips)
    except Exception as e:  # noqa: BLE001 — a registry problem must never break today's path
        logger.warning("llm_deployment_plan_failed", model=model.model_key, error=str(e)[:200])
        return None
