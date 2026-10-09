# api/routers/admin_search.py
# AA-752 — global admin search powering the ⌘K command palette.
#
# GET /admin/search?q=&limit= (admin-secret, same gate as every other /admin/*). Returns a small,
# capped bundle of tours / tenants / jobs matching `q` by a parameterised, case-insensitive
# substring (ILIKE '%q%' with %/_ escaped so a literal underscore or percent is not a wildcard).
# The command palette fetches this debounced; it is deliberately cheap (fixed small caps) and never
# writes anything.
#
# The SQL + params are built by pure helpers (`_tours_query`, `_tenants_query`, `_jobs_query`,
# `escape_like`) so they are unit-testable (shape / params / escaping / caps / short-q) without a DB.
from __future__ import annotations

import structlog
from fastapi import APIRouter, Header, Query, Request

from api.routers.admin import verify_admin_secret

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger()

# Minimum query length — a 1-char query would match almost everything and is not useful.
MIN_Q_LEN = 2
# Per-group caps (the palette shows a short list per group).
TOURS_CAP = 8
TENANTS_CAP = 5
JOBS_CAP = 5

_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def escape_like(q: str) -> str:
    """Escape the LIKE/ILIKE wildcards so a user's literal `%` or `_` is matched literally. The
    backslash is escaped first so it is not doubled by the following replacements. The queries use
    `ESCAPE '\\'` to pair with this."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tours_query(q: str, cap: int) -> tuple[str, list]:
    """Tours whose raw `src_name` OR published `aa_name` contains `q`. LEFT JOIN the published row
    (a raw tour may not be published yet); exclude deleted raw tours. `master` = it has an
    active published row, `trashed` = the published row is soft-deleted."""
    like = f"%{escape_like(q)}%"
    sql = """
        SELECT rt.tour_id,
               COALESCE(pt.aa_name, rt.src_name) AS name,
               rt.src_name,
               rt.country,
               (pt.tour_id IS NOT NULL AND pt.deleted_at IS NULL) AS master,
               (pt.deleted_at IS NOT NULL) AS trashed
        FROM silver_aa_internal.raw_tours rt
        LEFT JOIN gold_aa_internal.published_tours pt
          ON pt.tour_id = rt.tour_id
        WHERE rt.deleted_at IS NULL
          AND (rt.src_name ILIKE $1 ESCAPE '\\'
               OR pt.aa_name ILIKE $1 ESCAPE '\\')
        ORDER BY master DESC, name ASC
        LIMIT $2
    """
    return sql, [like, cap]


def _tenants_query(q: str, cap: int) -> tuple[str, list]:
    """Tenants whose name or slug contains `q`. The master sentinel tenant is internal, not a real
    marketplace tenant — exclude it."""
    like = f"%{escape_like(q)}%"
    sql = """
        SELECT tenant_id, name, slug, is_active
        FROM shared.tenants
        WHERE tenant_id <> $3::uuid
          AND (name ILIKE $1 ESCAPE '\\'
               OR slug ILIKE $1 ESCAPE '\\')
        ORDER BY is_active DESC, name ASC
        LIMIT $2
    """
    return sql, [like, cap, _MASTER_TENANT_ID]


def _jobs_query(q: str, cap: int) -> tuple[str, list]:
    """Jobs whose id starts with `q` (id prefix) OR whose kind contains `q`. The id is a UUID, so a
    prefix match (`id::text ILIKE q||'%'`) is the useful shape; kind is a substring match."""
    prefix = f"{escape_like(q)}%"
    kind_like = f"%{escape_like(q)}%"
    sql = """
        SELECT id, kind, status, created_at
        FROM shared.job
        WHERE id::text ILIKE $1 ESCAPE '\\'
           OR kind ILIKE $2 ESCAPE '\\'
        ORDER BY created_at DESC
        LIMIT $3
    """
    return sql, [prefix, kind_like, cap]


@router.get("/search", summary="AA-752 — global admin search (tours/tenants/jobs) for the ⌘K palette")
async def admin_search(
    request: Request,
    q: str = Query("", description="search term, >= 2 chars after trimming"),
    limit: int = Query(0, ge=0, le=20, description="optional override per group (clamped to the caps)"),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    term = (q or "").strip()
    empty = {"tours": [], "tenants": [], "jobs": []}
    if len(term) < MIN_Q_LEN:
        return empty

    # `limit` is an optional per-group override; it never exceeds the hard caps.
    tours_cap = min(limit, TOURS_CAP) if limit else TOURS_CAP
    tenants_cap = min(limit, TENANTS_CAP) if limit else TENANTS_CAP
    jobs_cap = min(limit, JOBS_CAP) if limit else JOBS_CAP

    tours_sql, tours_params = _tours_query(term, tours_cap)
    tenants_sql, tenants_params = _tenants_query(term, tenants_cap)
    jobs_sql, jobs_params = _jobs_query(term, jobs_cap)

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tour_rows = await conn.fetch(tours_sql, *tours_params)
        tenant_rows = await conn.fetch(tenants_sql, *tenants_params)
        job_rows = await conn.fetch(jobs_sql, *jobs_params)

    return {
        "tours": [
            {
                "tour_id": str(r["tour_id"]),
                "name": r["name"],
                "src_name": r["src_name"],
                "country": r["country"],
                "master": bool(r["master"]),
                "trashed": bool(r["trashed"]),
            }
            for r in tour_rows
        ],
        "tenants": [
            {
                "tenant_id": str(r["tenant_id"]),
                "name": r["name"],
                "slug": r["slug"],
                "is_active": bool(r["is_active"]),
            }
            for r in tenant_rows
        ],
        "jobs": [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in job_rows
        ],
    }
