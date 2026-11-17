"""AA-1116 (contract tenant.entitlements.v1 1.10) — the quote discount of each subscription line, stored for Booking.

AA-Booking drafts the monthly software invoice from ``catalog.v_tenant_billing_v1``; since CIS 444 the view carries
``discount_pct`` / ``discount_kind`` per subscription line, read from ``shared.tenant_subscription.quote_discount_pct``
/ ``quote_discount_kind`` (CIS 443). This module fills those two columns from the ONE quote rule
(:func:`services.tenant_account.quote.build`, the same call Tenant 360 and ``/v1/billing`` make through
``billing_terms.tenant_quote``), so Booking's discount line equals what staff and the agency see:

* a line the quote discounts gets the quote's ``discount_pct`` (the founding share on an eligible line when the founding
  offer wins, the combo share on every line when the combo wins; never both, S282 decision 6) and its kind;
* every other row of the tenant (no discount, a line the founding offer does not cover, a subscription that is not
  live) gets 0 / NULL.

The share is a percentage of the line, not an amount: Booking already prices the line (currency, agreed price, seats)
with the same rules and takes the share off it, so a seat change between two ticks never leaves a stale amount.

When: :func:`refresh` after a Tenant 360 billing write (billing details: currency, founding dates; subscription terms)
and :func:`refresh_all` every hour in the ``tenant_billing_tick`` job (the founding offer ends on a date, a plan price
or a ``cis.billing.*`` setting may change, an add-on may start or end). A write only touches a row whose share changed
(the update trigger ignores these columns: no account event; the guard bumps ``updated_at``). Kind ``internal`` (AA
itself) is never billed and never refreshed. Before CIS 443 is applied the refresh answers ``skipped``.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

import asyncpg
import structlog

from services.tenant_account import billing_terms as bt
from services.tenant_account import pricing_settings as ps
from services.tenant_account import quote as qt

logger = structlog.get_logger()

PCT_PLACES = Decimal("0.01")     # technical-constant: numeric(5,2) of CIS 443
PCT_MAX = Decimal(100)           # technical-constant: CIS 443 CHECK (a share of the line)

STORED_SQL = """
    SELECT product, quote_discount_pct, quote_discount_kind FROM shared.tenant_subscription WHERE tenant_id = $1::uuid
"""
WRITE_SQL = """
    UPDATE shared.tenant_subscription SET quote_discount_pct = $3, quote_discount_kind = $4
     WHERE tenant_id = $1::uuid AND product = $2
       AND (quote_discount_pct, quote_discount_kind) IS DISTINCT FROM ($3::numeric, $4::text)
"""
TENANTS_SQL = """
    SELECT DISTINCT s.tenant_id::text AS tenant_id
      FROM shared.tenant_subscription s JOIN shared.tenants t ON t.tenant_id = s.tenant_id
     WHERE t.kind <> 'internal'
     ORDER BY 1
"""


def _pct(value: Any) -> Decimal:
    p = Decimal(str(value or 0)).quantize(PCT_PLACES)
    return min(max(p, Decimal(0)), PCT_MAX)


def line_shares(quote: qt.Quote, products: list[str]) -> dict[str, tuple[Decimal, Optional[str]]]:
    """``{product: (share %, kind)}`` for every product of the tenant: the quote's share on a line it discounts, else
    ``(0, None)``. Pure."""
    out: dict[str, tuple[Decimal, Optional[str]]] = {p: (Decimal(0).quantize(PCT_PLACES), None) for p in products}
    pct = _pct(quote.discount_pct)
    if quote.discount_kind is None or pct <= 0:
        return out
    for line in quote.lines:
        if line.discount > 0:
            out[line.product] = (pct, quote.discount_kind)
    return out


async def refresh(conn, tenant_id: str, *, today: Optional[date] = None,
                  terms: Optional[dict] = None) -> dict[str, Any]:
    """Recompute the tenant's shares and write the rows that changed. ``{"tenant_id", "changed": [products]}``; a
    tenant that does not exist changes nothing."""
    tenant = await conn.fetchrow(bt.TENANT_SQL, tenant_id)
    if tenant is None:
        return {"tenant_id": tenant_id, "changed": []}
    subs = [dict(r) for r in await conn.fetch(bt.SUBS_SQL, tenant_id)]
    quote = qt.build(dict(tenant), subs, terms if terms is not None else ps.snapshot(ps.COMMERCIAL),
                     today=today or date.today())
    stored = {r["product"]: r for r in await conn.fetch(STORED_SQL, tenant_id)}
    changed = []
    for product, (pct, kind) in line_shares(quote, list(stored)).items():
        row = stored[product]
        if _pct(row["quote_discount_pct"]) == pct and row["quote_discount_kind"] == kind:
            continue
        await conn.execute(WRITE_SQL, tenant_id, product, pct, kind)
        changed.append(product)
    return {"tenant_id": tenant_id, "changed": changed}


async def refresh_all(pool, *, today: Optional[date] = None) -> dict[str, Any]:
    """Every tenant with a subscription (kind internal excluded), each in its own transaction (one bad row never holds
    the others). ``{"outcome": "ok" | "skipped", "tenants", "changed", "failed"}``."""
    today = today or date.today()
    terms = ps.snapshot(ps.COMMERCIAL)
    out: dict[str, Any] = {"outcome": "ok", "tenants": 0, "changed": 0, "failed": 0}
    async with pool.acquire() as conn:
        tenants = [r["tenant_id"] for r in await conn.fetch(TENANTS_SQL)]
        for tid in tenants:
            try:
                async with conn.transaction():
                    done = await refresh(conn, tid, today=today, terms=terms)
            except asyncpg.UndefinedColumnError:
                logger.info("quote_discount_skipped", reason="443_not_applied")
                return {**out, "outcome": "skipped"}
            except Exception:  # noqa: BLE001 — logged; the next hour retries this tenant
                out["failed"] += 1
                logger.warning("quote_discount_refresh_failed", tenant_id=tid, exc_info=True)
                continue
            out["tenants"] += 1
            out["changed"] += len(done["changed"])
    logger.info("quote_discount_refreshed", **out)
    return out


async def refresh_quietly(conn, tenant_id: str) -> None:
    """:func:`refresh` after a Tenant 360 billing write, in its own transaction; a failure is logged, never raised
    (the write is committed; the hourly tick catches up)."""
    try:
        async with conn.transaction():
            await refresh(conn, tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning("quote_discount_refresh_failed", tenant_id=tenant_id, exc_info=True)
