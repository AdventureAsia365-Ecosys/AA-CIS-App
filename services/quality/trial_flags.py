"""AA-1049 W3 (root ADR 0020) — per-job opt-in of the W3 switches, for a paid trial on an audit tenant only.

The W3 behaviour changes are opt-in process switches whose default equals main (S272 decision):
``T9_REGRESSION_GUARD_MODE`` / ``T9_SEO_BUILDER`` (T9) and ``T2_ORCHESTRATOR`` (T2). Turning one on in the worker's
environment would change every tenant's writes, so a paid trial needs the switch on for ONE job instead. This module
is that seam:

- a job payload may carry ``trial_flags`` ({switch: value}); only the T9 write and T2 rewrite jobs read it, and only
  the switches of their own workflow with one of the values the switch itself knows (``ALLOWED``);
- it is honoured only for an audit tenant: a synthetic tenant (``shared.tenants.kind = 'demo'``, not the pilot
  agency). A real agency, the pilot, the platform tenant, an unknown tenant or a database error = the request is
  refused and the job runs main's default path (fail closed to main, never a failed job);
- the value lives in a ContextVar for the job's task only (asyncio tasks and ``asyncio.to_thread`` copy it), so two
  jobs running side by side in one worker never see each other's switches; the worker's environment is never read
  for a switch the job set and never written;
- the switch readers (``service.regression_guard_mode``, ``service.seo_builder_mode``, ``t2_orchestrator.mode``)
  read ``get(name)``: the job's value when one is active, else the environment exactly as before;
- the job result records what was asked, applied and refused (``trial_flags``), so the trial report reads the
  outcome from ``shared.job.result``.

No payload key = no database read and no change: every job built by the routers today runs exactly as on main.
The release fingerprint (``writing_memory.config_fp``) keeps reading the process environment: a one-job trial is not
a platform configuration change.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Optional

import structlog

logger = structlog.get_logger()

PAYLOAD_KEY = "trial_flags"
AUDIT_TENANT_KIND = "demo"

# job kind -> {switch: values the switch itself accepts}. Kept in step with the readers (test_aa1049_trial_flags).
ALLOWED: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "t9_write": MappingProxyType({"T9_REGRESSION_GUARD_MODE": ("stable_keys", "gate_names"),
                                  "T9_SEO_BUILDER": ("on", "off"),
                                  "T9_LENGTH_EXTEND": ("on", "off")}),   # AA-1077: the extend repair
    "t2_rewrite": MappingProxyType({"T2_ORCHESTRATOR": ("linear", "graph")}),
})

_EMPTY: Mapping[str, str] = MappingProxyType({})
_ACTIVE: ContextVar[Mapping[str, str]] = ContextVar("aa1049_trial_flags", default=_EMPTY)

_TENANT_SQL = "SELECT kind, is_pilot FROM shared.tenants WHERE tenant_id = $1::uuid"


def get(name: str) -> Optional[str]:
    """The value of switch ``name`` for the running job: the trial value when this job set one, else the process
    environment (unchanged behaviour)."""
    active = _ACTIVE.get()
    if name in active:
        return active[name]
    return os.environ.get(name)


def active() -> dict[str, str]:
    """The trial switches of the running job ({} outside a trial)."""
    return dict(_ACTIVE.get())


def parse(kind: str, raw: Any) -> tuple[dict[str, str], list[str]]:
    """(the switches this job kind may set, normalised; the refused entries as 'NAME: reason')."""
    allowed = ALLOWED.get(kind) or {}
    if not isinstance(raw, Mapping):
        return {}, [f"{PAYLOAD_KEY}: not an object"]
    flags: dict[str, str] = {}
    refused: list[str] = []
    for name, value in raw.items():
        name = str(name)
        if name not in allowed:
            refused.append(f"{name[:64]}: not a trial switch of {kind}")
            continue
        norm = str(value if value is not None else "").strip().lower()
        if norm not in allowed[name]:
            refused.append(f"{name}: value not allowed")
            continue
        flags[name] = norm
    return flags, refused


async def audit_tenant(pool, tenant_id: Optional[str]) -> tuple[bool, str]:
    """(True, 'demo') for a synthetic tenant that is not the pilot; else (False, why). Fails closed."""
    if not tenant_id:
        return False, "no tenant"
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_TENANT_SQL, str(tenant_id))
    except Exception as exc:  # noqa: BLE001 — fail closed to main's path, never fail the job
        return False, f"tenant lookup failed: {type(exc).__name__}"
    if row is None:
        return False, "unknown tenant"
    if row["kind"] != AUDIT_TENANT_KIND:
        return False, "not an audit tenant"
    if row["is_pilot"]:
        return False, "pilot tenant"
    return True, AUDIT_TENANT_KIND


@asynccontextmanager
async def scope(pool, kind: str, payload: Optional[Mapping], tenant_id: Optional[str]
                ) -> AsyncIterator[Optional[dict]]:
    """Run the body with the job's trial switches on (audit tenant only). Yields the record for the job result, or
    None when the payload asks for nothing (no database read, no change)."""
    raw = (payload or {}).get(PAYLOAD_KEY)
    if not raw:
        yield None
        return
    flags, refused = parse(kind, raw)
    if flags:
        ok, why = await audit_tenant(pool, tenant_id)
        if not ok:
            refused += [f"{n}: {why}" for n in flags]
            flags = {}
    record = {"requested": {str(k)[:64]: str(v)[:32] for k, v in raw.items()} if isinstance(raw, Mapping) else {},
              "applied": dict(flags), "refused": refused}
    if refused:
        logger.warning("trial_flags_refused", kind=kind, tenant_id=str(tenant_id), refused=refused)
    if not flags:
        yield record
        return
    token = _ACTIVE.set(MappingProxyType(dict(flags)))
    logger.info("trial_flags_applied", kind=kind, tenant_id=str(tenant_id), applied=flags)
    try:
        yield record
    finally:
        _ACTIVE.reset(token)
