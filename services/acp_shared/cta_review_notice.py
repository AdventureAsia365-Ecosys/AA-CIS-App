"""AA-819 follow-up (S237) — a tenant CTA that claims reviews, ratings or testimonials.

F19_authenticity deliberately exempts the tenant's own CTA line (it is their configured text and is
published word for word), so "Read the five-star reviews our travellers left" reaches the reader
as typed. Nghiệp's decision (S237): never block or edit it. Instead, on every tenant endpoint that
saves a CTA:

- the save happens exactly as before;
- the response carries a non-blocking `cta_warning` ({"code": "cta_review_claim", "phrases": [...]})
  the portal shows inline (tenant-facing wording lives in the portal; it never names gates or flags);
- AA admins get ONE bell notification per tenant + CTA text (`shared.notifications`, event
  `tenant.cta.review_claim`, target_roles ['admin']), so the case feeds the Terms (AA-820) and a
  talk with the business owner. A re-save of the same text does not notify again.

The rule is `authenticity.cta_review_claims()` — the F19 testimonial patterns plus a few CTA review
hooks — so there is one source of truth. Fail open: a notification error is logged, never raised.
"""
from __future__ import annotations

import hashlib
from typing import Optional

import structlog

from services.acp_content_writing.authenticity import cta_review_claims
from services.notifications import EventType, NotificationService

logger = structlog.get_logger()

WARNING_CODE = "cta_review_claim"
EVENT = EventType.TENANT_CTA_REVIEW_CLAIM

# Where the tenant set the CTA — the admin message names it.
WHERE_BRAND_DEFAULT = "brand_default_cta"
WHERE_WRITE_OVERRIDE = "write_cta"
_WHERE_LABEL = {
    WHERE_BRAND_DEFAULT: "Brand Identity (default call to action)",
    WHERE_WRITE_OVERRIDE: "the call to action typed for one piece (Writer)",
}

_SEEN_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1 AND tenant_id = $2::uuid AND payload->>'cta_key' = $3
     LIMIT 1
"""
_TENANT_SQL = "SELECT name, slug FROM shared.tenants WHERE tenant_id = $1::uuid"


def _normalise(cta: str) -> str:
    return " ".join((cta or "").split())


def cta_key(cta: str) -> str:
    """Idempotency key of a CTA text: case and whitespace do not make a new notification."""
    return hashlib.sha256(_normalise(cta).lower().encode("utf-8")).hexdigest()[:24]


def cta_warning(cta: Optional[str]) -> Optional[dict]:
    """The non-blocking response field, or None for an ordinary CTA."""
    phrases = cta_review_claims(cta)
    return {"code": WARNING_CODE, "phrases": phrases} if phrases else None


async def notify_admins(pool, tenant_id: str, cta: str, phrases: list[str], where: str) -> bool:
    """Insert the admin bell row unless this tenant already has one for the same CTA text. Returns
    True when a row was inserted. Never raises: the tenant's save has already happened."""
    text = _normalise(cta)
    key = cta_key(text)
    try:
        async with pool.acquire() as conn, conn.transaction():
            # Two saves of the same text at once must not both insert.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{EVENT.value}:{tenant_id}:{key}")
            if await conn.fetchval(_SEEN_SQL, EVENT.value, str(tenant_id), key):
                return False
            tenant = await conn.fetchrow(_TENANT_SQL, str(tenant_id))
            name = (tenant["name"] if tenant else None) or "Unknown tenant"
            slug = (tenant["slug"] if tenant else None) or str(tenant_id)
            where_label = _WHERE_LABEL.get(where, where)
            message = (f"{name} ({slug}) set a call to action that mentions reviews or ratings in "
                       f"{where_label}: \"{text[:200]}\" — matched: {', '.join(phrases)}")
            await NotificationService(conn).emit(
                EVENT, entity_type="tenant_cta", entity_id=slug, tenant_id=str(tenant_id),
                payload={"message": message, "tenant_name": name, "tenant_slug": slug, "where": where,
                         "cta": text, "phrases": phrases, "cta_key": key},
                actor_type="tenant",
            )
        logger.info("cta_review_claim_notified", tenant_id=str(tenant_id), where=where, phrases=phrases)
        return True
    except Exception as exc:  # fail open — log, never fail the tenant's save
        logger.warning("cta_review_claim_notify_failed", tenant_id=str(tenant_id), where=where, error=str(exc))
        return False


async def check_saved_cta(pool, tenant_id: str, cta: Optional[str], where: str,
                          *, notify: bool = True) -> Optional[dict]:
    """Call AFTER the CTA was saved. Returns the `cta_warning` field (or None) and, when the CTA
    claims reviews, notifies AA admins once per tenant + text. Never raises."""
    try:
        warning = cta_warning(cta)
    except Exception as exc:  # a regex bug must not fail a save either
        logger.warning("cta_review_claim_check_failed", tenant_id=str(tenant_id), error=str(exc))
        return None
    if warning and notify:
        await notify_admins(pool, tenant_id, cta or "", warning["phrases"], where)
    return warning


__all__ = ["EVENT", "WARNING_CODE", "WHERE_BRAND_DEFAULT", "WHERE_WRITE_OVERRIDE", "check_saved_cta",
           "cta_key", "cta_warning", "notify_admins"]
