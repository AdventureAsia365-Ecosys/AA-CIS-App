"""AA-1042 (S270, scope addition of S267) — the Guide tab of the admin AI gateway page (/admin/ai-gateway?tab=guide).

``GET /admin/ai-gateway/guide`` (api/routers/admin_ai_gateway.py, spend.read) answers ``{doc, doc_reason, live}``:

- ``doc``: the ONE prose source of the gateway, root ``docs/architecture/ai-gateway.md`` (catalog id
  ``ARCH-01-ai-gateway``) and its Vietnamese copy, read from the Docs sync bundle through
  ``docs_reader.load_staff_page_by_id`` (the manifest names the entry and its key; no caller path, the body already
  redacted by the builder and masked again on serve). Staff tier only: when the manifest marks it owner (or anything
  but staff) ``doc`` is None and ``doc_reason`` is "owner_tier". When the bundle does not hold the page yet (or storage
  fails) ``doc`` is ``{"missing": true, "reason": ...}`` and the tab shows a note. ``links``: whether this caller may
  open /admin/docs (``reader``: the POLICY of ``GET /admin/docs/page``) and, only then, the staff pages published in
  the bundle, so the page's relative links open the reader for those viewers and stay plain text for the others.
- ``live``: the tables under the document, each read by the reader that already serves it elsewhere (no new SQL):
  ``routes`` (the GET /admin/ai-gateway/route-control payload without the editor options; each stage's ``first`` is
  the deployment that would SERVE it, from ``llm_route_control.serving``, the resolver of the Route control preview,
  which skips refused deployments: never the chain's first key), ``matrix`` (model x connection from the deployment
  rows the refusal evidence reads: enabled, priority, access state, last probe), ``register`` (every connection:
  key, kind, switch, access state and the secret NAME, never a value; no account ID, role or URL), ``caps`` (the
  connection monthly budgets of the Budgets tab) and ``coverage`` (the Stage coverage report: active stages and the
  ``disabled`` list). One failing source leaves its table empty and names itself in ``errors``.

$0: database reads and one S3 read through the docs reader cache; no provider is called. Editing a route changes
these tables at once (no doc edit, no deploy).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from api.services import docs_reader

GUIDE_PAGE_ID = "ARCH-01-ai-gateway"
GUIDE_REPO = "AA-Ecosys"

# Register: the only connection fields the tab shows (names and states; account IDs, roles, URLs never).
REGISTER_FIELDS = ("connection_key", "kind", "label", "enabled", "data_scope", "access_state", "last_probe_at",
                   "secret_name", "credential_env", "failure_domain", "balance_source")
STAGE_FIELDS = ("stage", "role", "is_active", "product", "chain", "pii_class")
CAP_FIELDS = ("connection_key", "kind", "label", "enabled", "monthly_budget_usd", "month_spent_usd", "month_calls",
              "used_pct", "alert_pct", "hard_stop", "state")
COVERAGE_STAGE_FIELDS = ("stage", "role", "status", "live_model", "live_connection", "unproven")
MATRIX_CELL_FIELDS = ("model_key", "connection_key", "enabled", "priority", "access_state", "last_probe_at")


# ── doc ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def read_doc(*, reader: bool, client_factory: Optional[Callable[[], Any]] = None
                   ) -> tuple[Optional[dict], Optional[str]]:
    """(doc, doc_reason). See the module docstring."""
    try:
        view = await docs_reader.load_staff_page_by_id(GUIDE_PAGE_ID, GUIDE_REPO, client_factory)
    except docs_reader.DocsRestricted:
        return None, "owner_tier"
    except docs_reader.DocsMissing:
        return {"missing": True, "reason": "not_published"}, None
    except docs_reader.DocsUnavailable:
        return {"missing": True, "reason": "unavailable"}, None
    published: list[str] = []
    if reader:
        try:
            m = await docs_reader.load_manifest(False, client_factory=client_factory)
            published = sorted({p["path"] for p in m["pages"] if p["repo"] == GUIDE_REPO})
        except (docs_reader.DocsMissing, docs_reader.DocsUnavailable):
            published = []
    page = view["page"]
    source = view.get("source") or {}
    return {
        "missing": False,
        "id": page["id"], "repo": page["repo"], "path": page["path"], "title": page["title"], "lang": page["lang"],
        "status": page.get("status"), "last_reviewed": page.get("last_reviewed"),
        "markdown": view["markdown"],
        "translations": [{"lang": t["lang"], "path": t["page"]["path"], "title": t["page"]["title"],
                          "markdown": t["markdown"]} for t in view["translations"]],
        "source": {"repo": source.get("repo"), "sha": source.get("sha")},
        "generated_at": view.get("generated_at"),
        "links": {"reader": bool(reader), "published": published},
    }, None


# ── live tables ──────────────────────────────────────────────────────────────────────────────────────────────────

def routes_view(state: dict, resolved: dict) -> dict:
    """The routes table (pure): the route-control state with each active stage's serving deployment. ``resolved``:
    stage -> ``llm_route_chains.resolve_stage`` output (``first`` = "<model>@<connection>" that would serve)."""
    stages = []
    for s in state["stages"]:
        r = resolved.get(s["stage"]) or {}
        first = r.get("first")
        model, _, connection = first.partition("@") if first else (None, "", None)
        stages.append({
            **{k: s.get(k) for k in STAGE_FIELDS},
            "first": first, "first_model": model, "first_connection": connection or None,
            "called_first": r.get("called_first"),
            "skipped": [{"model": x.get("model"), "connection": x.get("connection"), "reason": x.get("reason")}
                        for x in (r.get("skipped") or [])],
            "error": r.get("error"),
        })
    return {"version": state["version"], "order_mode": state["order_mode"], "updated_at": state.get("updated_at"),
            "updated_by": state.get("updated_by"), "max_attempts": state.get("max_attempts"), "stages": stages,
            "provider_order": state.get("provider_order") or []}


def matrix_view(world) -> dict:
    """Model x connection (pure): every deployment row with its probe state. The rows the refusal evidence read
    (with ``last_probe_at``, CIS 367) when present, else the route control's own deployment rows."""
    evidence = getattr(world, "evidence", None)
    rows = list(evidence.deployments.values()) if evidence is not None and evidence.deployments else list(
        world.deployments)
    models = {m.model_key: m for m in world.models}
    cells = sorted(({k: d.get(k) for k in MATRIX_CELL_FIELDS} for d in rows),
                   key=lambda d: (d["model_key"], d.get("priority") or 0, d["connection_key"]))
    used_models = sorted({c["model_key"] for c in cells})
    used_conns = sorted({c["connection_key"] for c in cells})
    return {
        "models": [{"model_key": k, "label": getattr(models.get(k), "label", None) or k,
                    "family": getattr(models.get(k), "family_key", None),
                    "enabled": getattr(models.get(k), "enabled", None)} for k in used_models],
        "connections": [{"connection_key": k, "label": (world.connections.get(k) or {}).get("label") or k,
                         "kind": (world.connections.get(k) or {}).get("kind"),
                         "enabled": (world.connections.get(k) or {}).get("enabled")} for k in used_conns],
        "cells": cells,
    }


def register_view(connections: dict, cells: list[dict]) -> list[dict]:
    """Every connection (pure): names and states only. ``connections``: key -> ProviderConnection."""
    per_conn: dict[str, list[dict]] = {}
    for c in cells:
        per_conn.setdefault(c["connection_key"], []).append(c)
    out = []
    for key in sorted(connections):
        c = connections[key]
        row = {f: getattr(c, f, None) for f in REGISTER_FIELDS}
        row["served_roles"] = list(c.served_roles) if getattr(c, "served_roles", None) is not None else None
        row["credential_source"] = c.credential_source()
        deps = per_conn.get(key, [])
        row["deployments"] = len(deps)
        row["deployments_enabled"] = sum(1 for d in deps if d.get("enabled"))
        out.append(row)
    return out


def caps_view(rows: list[dict]) -> list[dict]:
    """The connection caps (pure): the Budgets tab's connection tier without the account ID."""
    return [{k: r.get(k) for k in CAP_FIELDS} for r in rows]


def coverage_view(report: dict) -> dict:
    """The Stage coverage report (pure), stage lines without their per-deployment detail."""
    if not report.get("ready"):
        return {"ready": False, "stages": [], "uncovered": [], "disabled": []}
    return {"ready": True, "ok": report["ok"], "checked_at": report.get("checked_at"),
            "stages_checked": report.get("stages_checked"), "uncovered": report.get("uncovered") or [],
            "disabled": report.get("disabled") or [], "proof_max_age_hours": report.get("proof_max_age_hours"),
            "stages": [{k: s.get(k) for k in COVERAGE_STAGE_FIELDS} for s in report.get("stages") or []]}


async def _routes_and_matrix(pool) -> dict:
    from api.services import llm_route_control as rc
    world = await rc.read_world(pool)
    state = rc.state_of(world)
    resolved = await rc.serving(world)
    return {"routes": routes_view(state, resolved), "matrix": matrix_view(world)}


async def _register(pool) -> dict:
    from shared.llm_client import connections as conn_registry
    async with pool.acquire() as conn:
        return await conn_registry.fetch_connections(conn, llm_only=False)


async def _caps(pool) -> list[dict]:
    from api.services import ai_gateway
    return caps_view(await ai_gateway._read_connection_budgets(pool))


async def _coverage(pool) -> dict:
    from shared.llm_client import stage_coverage
    async with pool.acquire() as conn:
        return coverage_view(await stage_coverage.read(conn))


async def read_live(pool) -> dict:
    from api.services.ai_gateway import _guard
    (rm, rm_err), (conns, conns_err), (caps, caps_err), (cov, cov_err) = await asyncio.gather(
        _guard("Routes", lambda: _routes_and_matrix(pool)),
        _guard("Connection register", lambda: _register(pool)),
        _guard("Connection caps", lambda: _caps(pool)),
        _guard("Stage coverage", lambda: _coverage(pool)),
    )
    matrix = (rm or {}).get("matrix")
    errors = [{"source": s, "error": e} for s, e in (("routes", rm_err), ("register", conns_err), ("caps", caps_err),
                                                     ("coverage", cov_err)) if e]
    return {
        "routes": (rm or {}).get("routes"),
        "matrix": matrix,
        "register": register_view(conns, (matrix or {}).get("cells") or []) if conns is not None else None,
        "caps": caps,
        "coverage": cov,
        "errors": errors,
    }


async def read_guide(pool, *, reader: bool, client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """GET /admin/ai-gateway/guide (see the module docstring)."""
    (doc, reason), live = await asyncio.gather(read_doc(reader=reader, client_factory=client_factory),
                                               read_live(pool))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "page_id": GUIDE_PAGE_ID, "doc": doc,
            "doc_reason": reason, "live": live}
