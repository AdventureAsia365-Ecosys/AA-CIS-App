"""
api/routers/v1_marketplace.py — AA-444: tenant Marketplace view.

Per ADR-2026-038 §0.3 (Notion, 22/08/2026): "Marketplace của tenant X" is NOT a
separate stored page/table — it is a read-only VIEW aggregating two already
tenant-scoped tables the tenant reaches through other stages of the T-series:

    gold_aa_internal.tenant_tour_versions WHERE tenant_id = X   (T1->T4, "tour đã chọn")
    JOIN acp_contract.tour_atoms          WHERE owner_scope = X (T5->T6, "atom curated")

No new table, no migration (AA-440 confirmed both source tables are already
tenant-scoped — AA-439-01/03). This intentionally does NOT reuse
`admin_marketplace.py`'s `_CATALOG_QUERY` — that query browses the WHOLE platform
catalog for pre-tenant onboarding (a different question: "what could a tenant pick
before they exist"); this endpoint answers "what has THIS tenant already picked and
curated" (see docs/implementation-notes/AA-444-marketplace-view.md, "Should know").

Auth: tenant Bearer JWT only (`get_tenant`, imported unchanged from v1_tours.py) —
same as that file's own `/pool`/`/my-versions` read views. No staff/admin path: this
is a single tenant's own rollup, not a cross-tenant tool (A4 Cross-Tenant Oversight,
mentioned in the ADR as a related-but-separate future admin-tier need, is not this
endpoint).

GET /v1/marketplace — one tour per row (latest tenant_tour_versions per
published_tour_id), with that tour's owner_scope=tenant_id atom aggregate and a
reused (not reimplemented) price/runway estimate.
"""
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from api.routers.v1_tours import get_tenant
from services.acp_shared.marketplace_estimates import parse_price, runway_months

router = APIRouter(prefix="/v1/marketplace", tags=["tenant-marketplace"])


def _safe(row) -> dict:
    """Same local UUID/Decimal/datetime -> JSON-safe pattern every router in this
    repo defines for itself (no shared api/utils.safe() covers asyncpg Row objects
    directly — see admin_marketplace.py/v1_tours.py's own copies of this)."""
    if not row:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, UUID):
            d[k] = str(v)
        elif isinstance(v, Decimal):
            d[k] = float(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


# One row per tour the tenant has rewritten at least once — DISTINCT ON picks the
# latest version_number per published_tour_id, matching CatalogTab.tsx's (T4) own
# "current state per tour" framing (full version-by-version history already lives
# in that page's VersionHistory.tsx, not duplicated here — see implementation notes
# "Tradeoffs"). LEFT JOIN on the atom aggregate deliberately keeps a 0-atom tour in
# the result (COALESCE to 0) rather than dropping it — a rewritten tour with no
# atoms yet is a real, useful gap signal for T7 planning (ADR §0.3's stated
# purpose), not noise to filter out.
# INTENTIONAL: the JOIN into published_tours/raw_tours below reads a shared reference pool
# (100% sentinel tenant_id=aa_internal), not per-tenant data — the real per-tenant boundary is
# already drawn by tenant_tour_versions.tenant_id above it. KHÔNG BAO GIỜ chuyển sang RLS pool
# thật cho các query này — xem AA-578.
_MARKETPLACE_QUERY = """
    WITH latest_versions AS (
        SELECT DISTINCT ON (ttv.published_tour_id)
            ttv.id AS version_id, ttv.published_tour_id, ttv.version_number,
            ttv.status, ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed,
            ttv.created_at AS version_created_at
        FROM gold_aa_internal.tenant_tour_versions ttv
        WHERE ttv.tenant_id = $1::uuid
        ORDER BY ttv.published_tour_id, ttv.version_number DESC
    )
    SELECT
        lv.version_id, lv.version_number, lv.status, lv.quality_score,
        lv.qa_status, lv.qa_auto_passed, lv.version_created_at,
        pt.id AS published_tour_id, pt.tour_id, pt.aa_name AS name,
        rt.country, rt.duration, rt.price_raw,
        COALESCE(ac.atom_count, 0) AS atom_count,
        COALESCE(ac.high_atom_count, 0) AS high_atom_count
    FROM latest_versions lv
    JOIN gold_aa_internal.published_tours pt ON pt.id = lv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    LEFT JOIN (
        SELECT tour_id,
               count(*) AS atom_count,
               count(*) FILTER (WHERE distinctiveness = 'HIGH') AS high_atom_count
        FROM acp_contract.tour_atoms
        WHERE owner_scope = $2 AND NOT deleted AND NOT is_empty_marker
        GROUP BY tour_id
    ) ac ON ac.tour_id = pt.tour_id
    ORDER BY lv.version_created_at DESC
"""


@router.get("")
async def get_marketplace(request: Request, tenant=Depends(get_tenant)):
    """Tenant's own Marketplace rollup — see module docstring for the exact join.

    `posts_per_week` is read fresh from `shared.tenants` (never client-supplied)
    to feed `runway_months()` — same reused, unmodified formula
    `admin_marketplace.py`'s AA-330 Phần B already established (AA-440 confirmed
    pure/reusable). `price_usd`/`price_available` reuse `parse_price()` unchanged
    for the same "never fabricate a price, mark unavailable instead" contract that
    function's own docstring guarantees.
    """
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT posts_per_week FROM shared.tenants WHERE tenant_id = $1::uuid",
            tenant_id,
        )
        rows = await conn.fetch(_MARKETPLACE_QUERY, tenant_id, tenant_id)

    posts_per_week = tenant_row["posts_per_week"] if tenant_row else None

    tours = []
    total_atoms = 0
    for r in rows:
        t = _safe(r)
        price_raw = t.pop("price_raw")
        price_usd = parse_price(price_raw)
        t["price_usd"] = price_usd
        t["price_available"] = price_usd is not None
        atom_count = t["atom_count"]
        total_atoms += atom_count
        t["runway_months"] = (
            runway_months(atom_count, posts_per_week) if posts_per_week else None
        )
        tours.append(t)

    return {
        "tenant_id": tenant_id,
        "posts_per_week": posts_per_week,
        "tours": tours,
        "total_tours": len(tours),
        "total_atoms": total_atoms,
    }
