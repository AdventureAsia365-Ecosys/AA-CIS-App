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
published_tour_id), with that tour's owner_scope=tenant_id atom aggregate, a reused
(not reimplemented) runway estimate and AA-Booking's public "from" price (AA-1188: never the source price text).

AA-859 wave 4 (S247): the tours / atoms query runs on the aa_app_user tenant pool behind MARKETPLACE_FLAG
(`aa859:marketplace_pool`, fail closed; needs migration 266, RLS on tour_atoms). posts_per_week stays on the
main pool (shared.tenants, the tenant's own setting, D2).
"""
from decimal import Decimal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request

from api.core.aa544_tenant_pool import MARKETPLACE_FLAG, acquire_scoped_conn
from api.routers._tenant_projection import project_version, tenant_version_state  # AA-796 — shared rule
from api.routers.v1_tours import _from_prices, get_tenant
from services.acp_shared.marketplace_estimates import runway_months
from shared import tour_liveness as _tl
from shared.tour_liveness import with_served

router = APIRouter(prefix="/v1/marketplace", tags=["tenant-marketplace"])
logger = structlog.get_logger()


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
# The JOIN into published_tours/raw_tours below reads a shared reference pool (100% sentinel tenant_id =
# aa_internal), not per-tenant data — the real per-tenant boundary is drawn by tenant_tour_versions.tenant_id.
# AA-859 wave 4: it runs on the RLS tenant pool (MARKETPLACE_FLAG): since 239 the tenant role reads the AA master
# rows of published_tours / raw_tours, and since 266 the platform atoms plus its own (this query counts its own
# only). The old "never move this to the RLS pool, AA-578" note predates 239 and no longer holds.
_MARKETPLACE_QUERY = """
    WITH latest_versions AS (
        SELECT DISTINCT ON (ttv.published_tour_id)
            ttv.id AS version_id, ttv.published_tour_id, ttv.version_number,
            ttv.status, ttv.created_at AS version_created_at
        FROM gold_aa_internal.tenant_tour_versions ttv
        WHERE ttv.tenant_id = $1::uuid
        ORDER BY ttv.published_tour_id, ttv.version_number DESC
    )
    SELECT
        lv.version_id, lv.version_number, lv.status, lv.version_created_at,
        pt.id AS published_tour_id, pt.tour_id, pt.aa_name AS name,
        rt.country, rt.duration,
        (COALESCE(rt.source_group_id, rt.tour_id))::text AS catalog_source_id,
        COALESCE(ac.atom_count, 0) AS atom_count
    FROM latest_versions lv
    JOIN gold_aa_internal.published_tours pt ON pt.id = lv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    LEFT JOIN (
        SELECT tour_id,
               count(*) AS atom_count
        FROM acp_contract.tour_atoms
        WHERE owner_scope = $2 AND NOT deleted AND NOT is_empty_marker
        GROUP BY tour_id
    ) ac ON ac.tour_id = pt.tour_id
    ORDER BY lv.version_created_at DESC
"""


def _usd_amount(from_price) -> float | None:
    """The amount of a price.from.v1 dict as a number when its currency is USD; None otherwise or when unreadable."""
    if not from_price or str(from_price.get("currency") or "").upper() != "USD":
        return None
    try:
        return float(from_price["amount"])
    except (KeyError, TypeError, ValueError):
        return None


@router.get("")
async def get_marketplace(request: Request, tenant=Depends(get_tenant)):
    """Tenant's own Marketplace rollup — see module docstring for the exact join.

    `posts_per_week` is read fresh from `shared.tenants` (never client-supplied)
    to feed `runway_months()` — same reused, unmodified formula
    `admin_marketplace.py`'s AA-330 Phần B already established (AA-440 confirmed
    pure/reusable).

    AA-1188: each tour carries `from_price`, AA-Booking's public suggested retail price per person (price.from.v1, as
    on GET /v1/tours/pool) or null; `price_usd` is that amount when it is in USD, else null, and `price_available`
    says whether `price_usd` is set. The source price text (`raw_tours.price_raw`) is never read here.
    """
    tenant_id = tenant["sub"]
    pool = request.app.state.pool

    # AA-859 MAIN exception (D2): the tenant's own posts_per_week (shared.tenants, no RLS, no tenant-role grant)
    async with pool.acquire() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT posts_per_week FROM shared.tenants WHERE tenant_id = $1::uuid",
            tenant_id,
        )
    async with acquire_scoped_conn(request, str(tenant_id), MARKETPLACE_FLAG, fail_closed=True) as (conn, used):
        logger.info("aa859_marketplace_read", tenant_id=str(tenant_id), used_tenant_pool=used)
        rows = await conn.fetch(_MARKETPLACE_QUERY, tenant_id, tenant_id)
        served = await _tl.served_states(conn, [str(r["tour_id"]) for r in rows])   # AA-986 L3

    posts_per_week = tenant_row["posts_per_week"] if tenant_row else None

    tours = []
    total_atoms = 0
    # AA-1188 (DB-01, root ADR 0005 d7): the price a tenant sees is AA-Booking's public "from" price (price.from.v1, the
    # same read as GET /v1/tours/pool, on MAIN after the tenant block, fail soft), never the source price text.
    keys = [r.get("catalog_source_id") for r in rows]
    prices = await _from_prices(pool, keys)
    for r, key in zip(rows, keys):
        t = _safe(project_version(r))  # AA-796 — drops gate fields even if the query ever selects them
        t.pop("catalog_source_id", None)
        # AA-770/AA-796 — no QA states on the tenant side (shared rule in _tenant_projection)
        t["status"] = tenant_version_state(t.get("status"))
        from_price = prices.get(key) if key else None
        t["from_price"] = from_price
        # `price_usd` / `price_available` kept for the studio's "In my catalog" card: the from price when it is in USD,
        # else none (no conversion: CIS never computes a price).
        t["price_usd"] = _usd_amount(from_price)
        t["price_available"] = t["price_usd"] is not None
        atom_count = t["atom_count"]
        state = served.get(str(r["tour_id"]))
        with_served(t, state)
        # AA-986 L3 (ADR 0015 decision 5): a tour AA no longer serves (or has paused) keeps its row and badge, but
        # its atoms feed no new content: no runway, not counted in total_atoms.
        if state is None or state.served:
            total_atoms += atom_count
            t["runway_months"] = (
                runway_months(atom_count, posts_per_week) if posts_per_week else None
            )
        else:
            t["runway_months"] = None
        tours.append(t)

    return {
        "tenant_id": tenant_id,
        "posts_per_week": posts_per_week,
        "tours": tours,
        "total_tours": len(tours),
        "total_atoms": total_atoms,
    }
