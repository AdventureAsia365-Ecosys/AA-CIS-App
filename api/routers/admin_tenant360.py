"""AA-766 — Tenant 360 read endpoints behind the admin /admin/tenants inline panel.

Three admin-only views of one tenant, none of which a tenant ever sees (tenant-only-content rule,
AA-613: cost, gate verdicts and research coverage stay on admin routes):

* ``GET /admin/tenants/{id}/cost``    — month-to-date LLM spend per stage, $/piece per channel
  (blog vs social), $/tour for T2 rewrites, and plan revenue vs LLM cost -> margin.
* ``GET /admin/tenants/{id}/quality`` — T10 gate verdicts per gate (pass / blocking fail / warn,
  held pieces), piece status mix, held reasons and the tenant edit rate on T2 tour versions.
* ``GET /admin/tenants/{id}/markets`` — the tenant's SEO markets (Brand Identity ->
  tenant_seo_config.target_market) with segment_research_log coverage over the places in the
  tenant's tours, the planning markets (acp_shared.tenant_config), and this tenant's
  unmapped_market_requests rows (read-only).

AA-978 adds the Members routes over the CIS membership directory (``shared.tenant_members``, CIS 276):

* ``GET  /admin/tenants/{id}/members``                     — every membership, removed ones last; the identity only
  as a short fingerprint, never the raw (iss, sub).
* ``PATCH /admin/tenants/{id}/members/{membership_id}``     — change the Content studio role (owner / editor / viewer)
  only; CRM roles are managed in the CRM.
* ``POST /admin/tenants/{id}/members/{membership_id}/remove`` — remove the membership (final). The CIS 276 trigger
  writes the tenant.membership.v1 event in the same transaction; the partner CRM disables the member and signs them
  out within a minute, and locks the agency when that was its last owner.

Both writes need tenant.write (staff_authz POLICY) and write an acp_shared.audit_log row with the change.

AA-979 P2b adds the Content studio invitation (re-enrolment in the partner pool, root ADR 0010a):

* ``POST /admin/tenants/{id}/members/invitations``                 — create an ``invited`` membership with the
  Content studio role CIS decides, then relay the e-mail ONCE to the CRM (``crm.ops.v1`` ``POST
  /tenants/{id}/studio-invitations``, the staff member's own Cognito token through ``api/services/crm_console.py``):
  the CRM sends the AA system mail and owns the claim. CIS never stores the e-mail. 201 even when the delivery
  failed: the row stays ``invited`` and the answer says ``delivery.status = failed`` (send again).
* ``POST /admin/tenants/{id}/members/{membership_id}/invitation/send`` — send the invitation of an ``invited`` row
  again (the CRM is idempotent on ``cis_invitation_id``: it re-issues the same invitation with a new link).

Both invitation routes are on the write allow-list of the Dev-only E2E CIS admin (root ADR 0006a A10b-1, AA-979 phase
2 automation): :func:`_refuse_e2e_invitation` answers that session 403 ``e2e_read_only`` before any write or relay
unless the tenant is synthetic (``is_canary``), the address is an E2E mailbox address and the role is viewer / editor.

Cost definition is the AA-798 one (migration 223): ``shared.llm_call_log.cost_usd`` for the
tenant, current calendar month (``date_trunc('month', now())``), Jev included, shadow rows
(``quality_signal @> {"shadow": true}``) excluded — so the totals here add up to the
``llm_cost_usd`` the Usage & Billing tab reads from ``shared.v_tenant_monthly_usage``.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core import e2e_staff
from api.core.staff_authz import request_staff_identity, staff_policy

from api.routers._billing import usage_row_to_dict
from api.routers.admin import _admin_actor, verify_admin_secret
from services.tenant_membership import session as members_session
from services.tenant_membership import writer as members_writer
from api.services import crm_console
from api.services.platform_apps import relay_authorization

logger = structlog.get_logger()

# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/tenants", tags=["admin-tenant360"], dependencies=[Depends(staff_policy)])

AA_INTERNAL_TENANT = "00000000-0000-0000-0000-000000000001"

# The research cache's own freshness horizon (services/acp_contract/segment_research.py FRESH_FOR,
# same constant admin_seo.py mirrors).
FRESH_DAYS = 182

# Channel -> piece kind for the "$/piece blog vs social" split. Channels follow
# services/acp_planning/models.py's Channel literal (admin.py _VALID_CHANNELS).
SOCIAL_CHANNELS = frozenset({"facebook", "tiktok", "instagram", "linkedin"})

# ── Cost ──────────────────────────────────────────────────────────────────────────────────────
# One WHERE for every cost query: this tenant, this calendar month, no shadow rows (AA-798).
_COST_WHERE = """
    l.tenant_id = $1::uuid
    AND l.created_at >= date_trunc('month', now())
    AND l.created_at <  date_trunc('month', now()) + interval '1 month'
    AND NOT (l.quality_signal @> '{"shadow": true}'::jsonb)
"""

_COST_BY_STAGE_SQL = f"""
SELECT l.stage, count(*) AS calls, COALESCE(sum(l.cost_usd), 0) AS cost_usd
FROM shared.llm_call_log l
WHERE {_COST_WHERE}
GROUP BY l.stage
ORDER BY cost_usd DESC, l.stage
"""

# Calls linked to a T9/T10 piece, per channel. The piece's own channel wins over the request's
# (same COALESCE as /admin/a4/gate-telemetry).
_COST_BY_CHANNEL_SQL = f"""
SELECT COALESCE(cp.channel, agr.channel, 'unknown') AS channel,
       count(DISTINCT l.content_piece_id)            AS pieces,
       COALESCE(sum(l.cost_usd), 0)                  AS cost_usd
FROM shared.llm_call_log l
JOIN acp_shared.content_piece cp ON cp.piece_id = l.content_piece_id
LEFT JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
WHERE {_COST_WHERE}
GROUP BY 1
ORDER BY cost_usd DESC
"""

_USAGE_SQL = """
SELECT v.tenant_name, v.plan_tier, v.billing_month,
       v.tours_quota_monthly, v.api_calls_quota_monthly, v.price_usd_monthly,
       v.tours_rewritten, v.api_calls_used, v.quota_tours_pct, v.quota_calls_pct,
       v.tours_overage, v.overage_usd, v.llm_cost_usd, v.overage_rate_usd_per_tour
FROM shared.v_tenant_monthly_usage v
WHERE v.tenant_id = $1::uuid
"""

# ── Quality ───────────────────────────────────────────────────────────────────────────────────
_PIECE_STATUS_SQL = """
SELECT cp.status, count(*) AS n
FROM acp_shared.content_piece cp
WHERE cp.tenant_id = $1::uuid AND cp.created_at >= now() - make_interval(days => $2)
GROUP BY cp.status
ORDER BY n DESC
"""

# One row per T10 gate over the shipped pieces (approved + held) in the window. A ledger entry
# without `blocking` is blocking (sentence_repair.plan / service._hold_or_ship default).
_GATES_SQL = """
SELECT elem->>'gate' AS gate,
       count(*) AS evaluated,
       count(*) FILTER (WHERE COALESCE((elem->>'passed')::boolean, false)) AS passed,
       count(*) FILTER (WHERE NOT COALESCE((elem->>'passed')::boolean, false)
                          AND COALESCE((elem->>'blocking')::boolean, true)) AS failed_blocking,
       count(*) FILTER (WHERE NOT COALESCE((elem->>'passed')::boolean, false)
                          AND NOT COALESCE((elem->>'blocking')::boolean, true)) AS failed_warn,
       count(DISTINCT cp.piece_id) FILTER (WHERE cp.status = 'held'
                          AND NOT COALESCE((elem->>'passed')::boolean, false)) AS held_pieces
FROM acp_shared.content_piece cp
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(cp.gate_ledger) = 'array' THEN cp.gate_ledger ELSE '[]'::jsonb END) AS elem
WHERE cp.tenant_id = $1::uuid
  AND cp.created_at >= now() - make_interval(days => $2)
  AND cp.status IN ('approved', 'held')
GROUP BY 1
ORDER BY failed_blocking DESC, evaluated DESC, gate
"""

_HELD_REASONS_SQL = """
SELECT COALESCE(cp.held_reason, '(none)') AS held_reason, count(*) AS n
FROM acp_shared.content_piece cp
WHERE cp.tenant_id = $1::uuid AND cp.status = 'held'
  AND cp.created_at >= now() - make_interval(days => $2)
GROUP BY 1
ORDER BY n DESC
LIMIT 20
"""

# T2 tour versions: 'ai_generated' is the rewrite, 'tenant_edit' a tenant's inline edit
# (v1_tours.py review action=edit creates a new version with that edit_source).
_EDIT_RATE_SQL = """
SELECT count(*) FILTER (WHERE ttv.edit_source = 'ai_generated')                         AS ai_versions,
       count(*) FILTER (WHERE ttv.edit_source = 'tenant_edit')                          AS tenant_edits,
       count(DISTINCT ttv.published_tour_id) FILTER (WHERE ttv.edit_source = 'ai_generated') AS tours_generated,
       count(DISTINCT ttv.published_tour_id) FILTER (WHERE ttv.edit_source = 'tenant_edit')  AS tours_edited
FROM gold_aa_internal.tenant_tour_versions ttv
WHERE ttv.tenant_id = $1::uuid AND ttv.created_at >= now() - make_interval(days => $2)
"""

# ── Markets ───────────────────────────────────────────────────────────────────────────────────
_SEO_CONFIG_SQL = "SELECT target_market FROM shared.tenant_seo_config WHERE tenant_id = $1::uuid"

_PLANNING_MARKETS_SQL = "SELECT markets FROM acp_shared.tenant_config WHERE tenant_id = $1::uuid"

# Places in scope = canonical places of the Segments built from the platform atoms of the tours this
# tenant adopted (tenant_tour_versions, the same "tours this tenant made their own" join slate.py
# uses). aa_internal has no adopted versions — its scope is every active master. Atoms are read
# through v_active_tour_atoms (migration 203). Coverage is then counted per market in $3.
_MARKET_COVERAGE_SQL = """
WITH tours AS (
    SELECT DISTINCT pt.tour_id
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    WHERE ttv.tenant_id = $1::uuid AND NOT $2::boolean
    UNION
    SELECT pt.tour_id
    FROM gold_aa_internal.published_tours pt
    WHERE $2::boolean AND pt.master_status = 'active'
),
places AS (
    SELECT DISTINCT s.canonical_place
    FROM tours t
    JOIN acp_contract.v_active_tour_atoms ta ON ta.tour_id = t.tour_id AND ta.owner_scope = 'platform'
    JOIN acp_contract.atom_segment_member m ON m.atom_id = ta.atom_id
    JOIN acp_contract.atom_segment s ON s.segment_id = m.segment_id
)
SELECT mk.market,
       (SELECT count(*) FROM places)                                                   AS places_in_scope,
       count(r.canonical_place)                                                        AS researched,
       count(r.canonical_place) FILTER (
           WHERE r.researched_at > now() - make_interval(days => $4))                  AS fresh,
       max(r.researched_at)                                                            AS last_researched
FROM unnest($3::text[]) AS mk(market)
LEFT JOIN acp_contract.segment_research_log r
       ON r.market = mk.market AND r.canonical_place IN (SELECT canonical_place FROM places)
GROUP BY mk.market
"""

_UNMAPPED_SQL = """
SELECT country_code, first_seen_at, requested_at, resolved_at
FROM shared.unmapped_market_requests
WHERE tenant_id = $1::uuid
ORDER BY (resolved_at IS NULL) DESC, requested_at DESC
LIMIT 100
"""


# ── pure helpers (unit-tested) ────────────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _ratio(num: Optional[float], den: Optional[float], ndigits: int = 4) -> Optional[float]:
    """num/den, or None when either side is missing or the denominator is 0."""
    if num is None or not den:
        return None
    return round(num / den, ndigits)


def channel_kind(channel: Optional[str]) -> str:
    """blog | social | other — the AA-766 "$/piece blog vs social" split."""
    if channel == "blog":
        return "blog"
    if channel in SOCIAL_CHANNELS:
        return "social"
    return "other"


def piece_cost_by_kind(rows: list[dict]) -> list[dict]:
    """Roll per-channel {channel, pieces, cost_usd} rows up to blog / social / other."""
    acc: dict[str, dict] = {}
    for r in rows:
        k = channel_kind(r["channel"])
        a = acc.setdefault(k, {"kind": k, "pieces": 0, "cost_usd": 0.0})
        a["pieces"] += int(r["pieces"])
        a["cost_usd"] += float(r["cost_usd"])
    out = []
    for k in ("blog", "social", "other"):
        if k in acc:
            a = acc[k]
            a["cost_usd"] = round(a["cost_usd"], 4)
            a["cost_per_piece_usd"] = _ratio(a["cost_usd"], a["pieces"])
            out.append(a)
    return out


def margin_block(usage: Optional[dict], llm_cost_usd: float) -> dict:
    """Plan revenue vs LLM cost for the current month.

    Revenue = the plan's monthly price + this month's overage, not prorated (open business
    question, see docs/implementation-notes/AA-766.md). No plan attached -> revenue and margin
    are None; a price is never invented (AA-798)."""
    price = _f(usage.get("price_usd_monthly")) if usage else None
    overage = _f(usage.get("overage_usd")) if usage else None
    if price is None:
        return {"plan_attached": False, "plan_price_usd": None, "overage_usd": None,
                "revenue_usd": None, "llm_cost_usd": round(llm_cost_usd, 4),
                "margin_usd": None, "margin_pct": None, "revenue_basis": "no_plan"}
    revenue = round(price + (overage or 0.0), 2)
    margin = round(revenue - llm_cost_usd, 4)
    return {"plan_attached": True, "plan_price_usd": price, "overage_usd": overage,
            "revenue_usd": revenue, "llm_cost_usd": round(llm_cost_usd, 4),
            "margin_usd": margin, "margin_pct": _ratio(margin * 100, revenue, 1),
            "revenue_basis": "full_month_price_plus_overage"}


def edit_rate_block(row: Optional[dict]) -> dict:
    r = dict(row or {})
    ai = int(r.get("ai_versions") or 0)
    edits = int(r.get("tenant_edits") or 0)
    gen = int(r.get("tours_generated") or 0)
    edited = int(r.get("tours_edited") or 0)
    return {"ai_versions": ai, "tenant_edits": edits, "tours_generated": gen, "tours_edited": edited,
            "edit_rate": _ratio(float(edited), gen), "edits_per_version": _ratio(float(edits), ai)}


def _parse_json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def seo_market_codes(target_market: Any) -> tuple[list[str], list[str], bool]:
    """(supported market codes the pipeline researches, declared-but-unsupported codes,
    defaulted) — `defaulted` is True when nothing usable was declared and the pipeline falls back
    to US (seed_builder.resolve_buyer_markets). Pure: never records an unmapped request (the Slate
    read path does that; an admin page view must not)."""
    from services.seo_intelligence.seed_builder import (
        DFS_LOCATION_MAP, LOCATION_CODE_TO_MARKET, resolve_buyer_markets,
    )
    tm = _parse_json(target_market, {}) or {}
    countries = [c for c in (tm.get("countries") or []) if isinstance(c, str)]
    codes = [LOCATION_CODE_TO_MARKET[loc] for loc, _n, _l in resolve_buyer_markets(tm)]
    unsupported = list(dict.fromkeys(c for c in countries if c not in DFS_LOCATION_MAP))
    defaulted = not any(c in DFS_LOCATION_MAP for c in countries)
    return codes, unsupported, defaulted


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


async def _require_tenant(conn, tenant_id: UUID) -> dict:
    row = await conn.fetchrow(
        "SELECT tenant_id, name, plan_tier::text AS plan_tier FROM shared.tenants WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return dict(row)


# ── routes ────────────────────────────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/cost", summary="AA-766 — tenant month-to-date LLM cost, $/piece, $/tour, margin")
async def get_tenant_cost(tenant_id: UUID, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        stage_rows = [dict(r) for r in await conn.fetch(_COST_BY_STAGE_SQL, tenant_id)]
        channel_rows = [dict(r) for r in await conn.fetch(_COST_BY_CHANNEL_SQL, tenant_id)]
        usage_row = await conn.fetchrow(_USAGE_SQL, tenant_id)

    usage = usage_row_to_dict(usage_row) if usage_row else None
    by_stage = [{"stage": r["stage"], "calls": int(r["calls"]), "cost_usd": round(float(r["cost_usd"]), 4)}
                for r in stage_rows]
    total = round(sum(s["cost_usd"] for s in by_stage), 4)
    by_channel = [{"channel": r["channel"], "kind": channel_kind(r["channel"]), "pieces": int(r["pieces"]),
                   "cost_usd": round(float(r["cost_usd"]), 4),
                   "cost_per_piece_usd": _ratio(float(r["cost_usd"]), int(r["pieces"]))}
                  for r in channel_rows]
    piece_cost = round(sum(c["cost_usd"] for c in by_channel), 4)
    t2_cost = round(sum(s["cost_usd"] for s in by_stage if s["stage"].startswith("t2_")), 4)
    tours = int(usage["tours_rewritten"]) if usage and usage.get("tours_rewritten") is not None else 0
    return {
        "tenant_id": str(tenant_id),
        "billing_month": usage["billing_month"] if usage else None,
        "total_cost_usd": total,
        "by_stage": by_stage,
        "by_channel": by_channel,
        "by_kind": piece_cost_by_kind(channel_rows),
        "unattributed_to_piece_usd": round(total - piece_cost, 4),
        "per_tour": {"t2_cost_usd": t2_cost, "tours_rewritten": tours,
                     "cost_per_tour_usd": _ratio(t2_cost, tours)},
        "margin": margin_block(usage, total),
        "definition": ("shared.llm_call_log.cost_usd, this calendar month, Jev included, shadow rows "
                       "excluded (AA-798). LLM cost only: infra and DataForSEO are not in it."),
    }


@router.get("/{tenant_id}/quality", summary="AA-766 — tenant T10 gate verdicts, held reasons, edit rate")
async def get_tenant_quality(
    tenant_id: UUID, request: Request,
    days: int = Query(30, ge=1, le=365),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        status_rows = await conn.fetch(_PIECE_STATUS_SQL, tenant_id, days)
        gate_rows = await conn.fetch(_GATES_SQL, tenant_id, days)
        held_rows = await conn.fetch(_HELD_REASONS_SQL, tenant_id, days)
        edit_row = await conn.fetchrow(_EDIT_RATE_SQL, tenant_id, days)

    pieces = {r["status"]: int(r["n"]) for r in status_rows}
    gates = []
    for r in gate_rows:
        evaluated = int(r["evaluated"])
        gates.append({
            "gate": r["gate"], "evaluated": evaluated, "passed": int(r["passed"]),
            "failed_blocking": int(r["failed_blocking"]), "failed_warn": int(r["failed_warn"]),
            "held_pieces": int(r["held_pieces"]),
            "pass_rate": _ratio(float(r["passed"]), evaluated),
        })
    shipped = pieces.get("approved", 0) + pieces.get("held", 0)
    return {
        "tenant_id": str(tenant_id),
        "window_days": days,
        "pieces": {"by_status": pieces, "total": sum(pieces.values()), "shipped": shipped,
                   "held_rate": _ratio(float(pieces.get("held", 0)), shipped)},
        "gates": gates,
        "held_reasons": [{"held_reason": r["held_reason"], "count": int(r["n"])} for r in held_rows],
        "edits": edit_rate_block(dict(edit_row) if edit_row else None),
    }


@router.get("/{tenant_id}/markets", summary="AA-766 — tenant markets, research coverage, unmapped requests")
async def get_tenant_markets(tenant_id: UUID, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        target_market = await conn.fetchval(_SEO_CONFIG_SQL, tenant_id)
        planning = await conn.fetchval(_PLANNING_MARKETS_SQL, tenant_id)
        codes, unsupported, defaulted = seo_market_codes(target_market)
        coverage_rows = await conn.fetch(
            _MARKET_COVERAGE_SQL, tenant_id, str(tenant_id) == AA_INTERNAL_TENANT, codes, FRESH_DAYS)
        unmapped_rows = await conn.fetch(_UNMAPPED_SQL, tenant_id)

    by_market = {r["market"]: r for r in coverage_rows}
    coverage = []
    for m in codes:
        r = by_market.get(m)
        scope = int(r["places_in_scope"]) if r else 0
        fresh = int(r["fresh"]) if r else 0
        coverage.append({
            "market": m, "places_in_scope": scope,
            "researched": int(r["researched"]) if r else 0, "fresh": fresh,
            "fresh_pct": _ratio(fresh * 100.0, scope, 1),
            "last_researched": _iso(r["last_researched"]) if r else None,
        })
    tm = _parse_json(target_market, {}) or {}
    return {
        "tenant_id": str(tenant_id),
        "fresh_days": FRESH_DAYS,
        "seo": {"declared": tm.get("declared") or [], "countries": tm.get("countries") or [],
                "markets": codes, "unsupported": unsupported, "defaulted_to_us": defaulted,
                "configured": target_market is not None},
        "planning_markets": list(planning) if planning else None,
        "coverage": coverage,
        "unmapped_requests": [{
            "country_code": r["country_code"], "first_seen_at": _iso(r["first_seen_at"]),
            "requested_at": _iso(r["requested_at"]), "resolved_at": _iso(r["resolved_at"]),
        } for r in unmapped_rows],
    }


# ── members (AA-978) ──────────────────────────────────────────────────────────────────────────

class MemberRoleChange(BaseModel):
    """PATCH body: the Content studio role only. A CRM role has no field here (CIS can never grant one)."""
    model_config = ConfigDict(extra="forbid")
    content_studio_role: Literal["owner", "editor", "viewer"]


def _members_unavailable(exc: Exception) -> HTTPException:
    logger.error("tenant_members_unavailable", reason=type(exc).__name__)
    return HTTPException(status_code=503, detail="Memberships need migration 276 (shared.tenant_members).")


@router.get("/{tenant_id}/members", summary="AA-978 — the tenant's CIS memberships (Content studio roles)")
async def get_tenant_members(tenant_id: UUID, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        try:
            members = await members_writer.list_members(conn, str(tenant_id))
        except asyncpg.UndefinedTableError as exc:
            raise _members_unavailable(exc) from exc
    return {
        "tenant_id": str(tenant_id),
        "members": members,
        "note": "CRM roles are managed in the partner CRM. Removing a member here also signs them out of the CRM "
                "within a minute.",
    }


async def _member_write(request: Request, tenant_id: UUID, op) -> dict:
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        try:
            row, changed = await op(conn)
        except members_writer.MemberNotFound as exc:
            raise HTTPException(status_code=404, detail="Membership not found") from exc
        except members_writer.MemberRemoved as exc:
            raise HTTPException(status_code=409, detail="This membership is removed; removal is final.") from exc
        except asyncpg.UndefinedTableError as exc:
            raise _members_unavailable(exc) from exc
    return {"tenant_id": str(tenant_id), "member": row, "changed": changed}


@router.patch("/{tenant_id}/members/{membership_id}",
              summary="AA-978 — change a member's Content studio role (owner / editor / viewer)")
async def patch_tenant_member(
    tenant_id: UUID, membership_id: UUID, body: MemberRoleChange, request: Request,
    x_admin_secret: str = Header(None), x_admin_user_id: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    actor = _admin_actor(x_admin_user_id)
    result = await _member_write(request, tenant_id, lambda conn: members_writer.change_role(
        conn, str(tenant_id), str(membership_id), body.content_studio_role, actor))
    logger.info("tenant_member_role_changed", tenant_id=str(tenant_id), membership_id=str(membership_id),
                role=body.content_studio_role, changed=result["changed"])
    return result


@router.post("/{tenant_id}/members/{membership_id}/remove",
             summary="AA-978 — remove a member (final; the partner CRM signs them out within a minute)")
async def remove_tenant_member(
    tenant_id: UUID, membership_id: UUID, request: Request,
    x_admin_secret: str = Header(None), x_admin_user_id: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    actor = _admin_actor(x_admin_user_id)
    result = await _member_write(request, tenant_id, lambda conn: members_writer.remove(
        conn, str(tenant_id), str(membership_id), actor))
    logger.info("tenant_member_removed", tenant_id=str(tenant_id), membership_id=str(membership_id),
                changed=result["changed"])
    return result



# ── Content studio invitations (AA-979 P2b) ───────────────────────────────────────────────────────

_EMAIL = r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$"


class MemberInvite(BaseModel):
    """POST body: the address (relayed once to the CRM, never stored in CIS), the Content studio role CIS decides,
    and the language of the AA system mail. No CRM role: CIS never grants one (ADR 0010 §7)."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=6, max_length=254, pattern=_EMAIL)
    content_studio_role: Literal["owner", "editor", "viewer"]
    locale: Literal["en", "vi"] = "en"


class MemberInviteResend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=6, max_length=254, pattern=_EMAIL)
    locale: Literal["en", "vi"] = "en"


async def _refuse_e2e_invitation(request: Request, conn, tenant_id: UUID, email: str,
                                 role: Optional[str] = None) -> None:
    """AA-979 (root ADR 0006a A10b-1, S252): the Dev-only E2E CIS admin may invite (or send again) only on a synthetic
    tenant (``shared.tenants.is_canary IS TRUE``), only to an address of the E2E mailbox domain and, on a new
    invitation, only as ``viewer`` or ``editor``. 403 ``e2e_read_only`` otherwise, before any row, audit or relay.
    Every other staff member is unaffected."""
    if not getattr(request_staff_identity(request), "e2e_read_only", False):
        return
    reason = None
    if role is not None and role not in e2e_staff.E2E_INVITE_ROLES:
        reason = "role"
    elif not e2e_staff.is_e2e_mailbox_address(email):
        reason = "address"
    elif not await e2e_staff.is_synthetic_tenant(conn, tenant_id):
        reason = "tenant_not_synthetic"
    if reason is not None:
        logger.warning("tenant_member_invitation_e2e_refused", tenant_id=str(tenant_id), reason=reason)
        raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)


async def _deliver(request: Request, tenant_id: UUID, invitation_id: str, email: str, locale: str) -> dict:
    """Relay the invitation to the CRM (crm.ops.v1). Never raises: the outcome goes into the answer."""
    status, body = await crm_console.crm_ops_call(
        "POST", f"/tenants/{tenant_id}/studio-invitations", relay_authorization(request),
        json={"email": email, "cis_invitation_id": invitation_id, "locale": locale})
    ok = 200 <= status < 300
    logger.info("tenant_member_invitation_relayed", tenant_id=str(tenant_id), invitation_id=invitation_id,
                crm_status=status, delivered=ok)
    out = {"status": "sent" if ok else "failed", "crm_status": status}
    if ok:
        out["expires_at"] = body.get("expires_at")
    else:
        out["detail"] = body.get("detail")
        out["message"] = body.get("message")
    return out


@router.post("/{tenant_id}/members/invitations", status_code=201,
             summary="AA-979 — invite a Content studio member (CIS row + CRM-delivered AA system mail)")
async def invite_tenant_member(
    tenant_id: UUID, body: MemberInvite, request: Request,
    x_admin_secret: str = Header(None), x_admin_user_id: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    actor = _admin_actor(x_admin_user_id)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        await _refuse_e2e_invitation(request, conn, tenant_id, body.email, body.content_studio_role)
        try:
            member = await members_session.invite(conn, str(tenant_id), body.content_studio_role, actor)
        except members_session.TenantInactive as exc:
            raise HTTPException(status_code=409, detail="This tenant is suspended or offboarded; "
                                                        "activate it before inviting members.") from exc
        except asyncpg.UndefinedTableError as exc:
            raise _members_unavailable(exc) from exc
    delivery = await _deliver(request, tenant_id, member["invitation_id"], body.email, body.locale)
    logger.info("tenant_member_invited", tenant_id=str(tenant_id), membership_id=member["membership_id"],
                role=body.content_studio_role, delivery=delivery["status"])
    return {"tenant_id": str(tenant_id), "member": member, "delivery": delivery}


@router.post("/{tenant_id}/members/{membership_id}/invitation/send",
             summary="AA-979 — send a Content studio invitation again (invited members only)")
async def resend_tenant_member_invitation(
    tenant_id: UUID, membership_id: UUID, body: MemberInviteResend, request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _require_tenant(conn, tenant_id)
        await _refuse_e2e_invitation(request, conn, tenant_id, body.email)
        try:
            members = await members_writer.list_members(conn, str(tenant_id))
        except asyncpg.UndefinedTableError as exc:
            raise _members_unavailable(exc) from exc
    member = next((m for m in members if m["membership_id"] == str(membership_id)), None)
    if member is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    if member["status"] != "invited" or not member["invitation_id"]:
        raise HTTPException(status_code=409, detail="Only an invited membership has an invitation to send.")
    delivery = await _deliver(request, tenant_id, member["invitation_id"], body.email, body.locale)
    return {"tenant_id": str(tenant_id), "member": member, "delivery": delivery}
