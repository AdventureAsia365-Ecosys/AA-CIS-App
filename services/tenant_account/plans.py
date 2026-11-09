"""The plan catalogue (AA-1028 R1 slice 3, root ADR 0019 d2): ``shared.plan`` + ``shared.plan_feature`` (CIS 353 / 355),
edited from CIS admin › Tenants › Plans & add-ons (``api/routers/admin_plans.py``).

A plan is a product (content, crm, tripplanner), a price and the feature keys it gives (``shared.entitlement_feature``,
a fixed catalogue: a key is code, so new keys come with a migration). Apps read the resolved entitlements, never a
plan name, so a change here changes what every subscriber may do at once: the CIS 353 trigger on ``plan_feature``
re-syncs each tenant on the plan and writes one ``entitlement.changed`` per changed key (``aa.actor`` = the admin).

- **Versioned**: every edit bumps ``shared.plan.version``; the write names the version it was made from and a stale
  one is refused (``version_conflict``), so two admins never overwrite each other unseen. The audit row
  (``acp_shared.audit_log``, ``plan.created`` / ``plan.updated`` / ``plan.retired``) holds before and after.
- **Retired** plans take no new subscription (the CIS 353 subscription guard); current subscribers keep them (the
  resolver reads their features) until staff move them. A retired plan is frozen: no edit.
- ``plan_key`` (``<product>.<name>``), ``product`` and ``tier`` never change after create: the content tier links
  ``shared.tenants.plan_tier`` to its plan (the 355 bridge), so it is chosen once. A content plan with a tier must
  give ``content.rate_limit_rpm`` (the limiter and the tenant create read it), a whole number.
- **Limits never vanish** (S266 review): "no row" means "no limit", so an edit cannot drop a limit key the plan has
  (``limit_removal_refused``: set its value, empty = no limit, instead), and a content plan with a tier always lists
  ``ai.budget_usd_monthly`` (a number, NULL only on the internal tier), ``content.rewrite`` and
  ``content.rate_limit_rpm`` (``enforced_limit_required``). Platform keys (``ai.*``, ``policy.*``) go on content plans
  only (``platform_key_content_only``; the resolver reads them from the content plan, CIS 361). Count, seat, day and
  RPM values are whole numbers up to int4 (the readers' type), an RPM at most ``RATE_LIMIT_RPM_MAX``.
- **RPM follows live**: the limiter enforces ``shared.tenants.rate_limit_rpm`` (copied from the plan at create), so an
  edit of a content plan's RPM also moves that column for every tenant on the plan whose column still equals the old
  value (they follow the default); a tenant with its own RPM (PATCH /admin/tenants/{id}) keeps it.

Also the readers that replaced the hard-coded ``PLAN_LIMITS``: ``tier_rate_limit_rpm`` (the RPM a new tenant of a
tier starts with) and ``assignable_tiers`` (the content tiers staff can put a tenant on).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from services.acp_shared.audit_log import write_audit_log

PRODUCTS = ("content", "crm", "tripplanner")
PRICE_UNITS = ("tenant", "seat", "site")
TIERS = ("starter", "growth", "business", "enterprise", "internal")
PLAN_KEY_RE = re.compile(r"^[a-z]+\.[a-z][a-z0-9_]*$")
LIMIT_MAX = Decimal(10) ** 12 - Decimal("0.01")  # numeric(14,2) max (written so: 12 nines read as an account id)
PRICE_MAX = Decimal("99999999.99")              # numeric(10,2)
RPM_KEY = "content.rate_limit_rpm"
AI_BUDGET_KEY = "ai.budget_usd_monthly"
REWRITE_KEY = "content.rewrite"
# Bounds by unit (S266 review): a count, seat, day or RPM value is a whole number its integer readers can hold
# (shared.tenants.rate_limit_rpm and the rewrite quota are int4); an RPM is capped like PATCH /admin/tenants.
INT4_MAX = 2**31 - 1
RATE_LIMIT_RPM_MAX = 10000
WHOLE_UNITS = frozenset({"count_month", "seats", "days", "rpm"})
# Limits a reader enforces on every tenant of a tiered content plan (S266 review): "no row" reads as "no limit", so
# such a plan always lists them, with an explicit value. The AI budget is a number (0 = no AI included) on every tier
# but AA's own (internal: NULL = no limit); the rewrite quota may be NULL (Enterprise / internal: the quota falls back
# to shared.membership_plans) but is listed. The RPM is required by rate_limit_required.
ENFORCED_CONTENT_KEYS = (AI_BUDGET_KEY, REWRITE_KEY, RPM_KEY)
UNLIMITED_AI_TIERS = frozenset({"internal"})

CREATED = "plan.created"
UPDATED = "plan.updated"
RETIRED = "plan.retired"


class PlanError(ValueError):
    """A plan write the catalogue refuses; ``code`` is stable (API 404 / 409 / 422), ``status`` the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code, message, status)
        self.code = code
        self.message = message
        self.status = status


# ── Readers (also used by the tenant create / PATCH and the billing view) ───────────────────────────────────────
TIER_RPM_SQL = """
    SELECT pf.limit_value
      FROM shared.plan p
      JOIN shared.plan_feature pf ON pf.plan_key = p.plan_key AND pf.feature_key = 'content.rate_limit_rpm'
     WHERE p.product = 'content' AND p.tier = $1::plan_tier_enum AND p.retired_at IS NULL
"""
ASSIGNABLE_TIERS_SQL = """
    SELECT p.tier::text AS tier
      FROM shared.plan p
      JOIN shared.plan_feature pf ON pf.plan_key = p.plan_key AND pf.feature_key = 'content.rate_limit_rpm'
     WHERE p.product = 'content' AND p.tier IS NOT NULL AND p.retired_at IS NULL AND pf.limit_value IS NOT NULL
     ORDER BY p.tier
"""
TIERS_RPM_SQL = """
    SELECT p.tier::text AS tier, pf.limit_value
      FROM shared.plan p
      JOIN shared.plan_feature pf ON pf.plan_key = p.plan_key AND pf.feature_key = 'content.rate_limit_rpm'
     WHERE p.product = 'content' AND p.tier IS NOT NULL AND p.retired_at IS NULL
"""


async def tier_rate_limit_rpm(conn, tier: str) -> Optional[int]:
    """The ``content.rate_limit_rpm`` of the live content plan of ``tier`` (what a tenant of that tier starts with),
    or None when the tier has no live plan or the plan sets no RPM (the tier is then not assignable)."""
    if tier not in TIERS:
        return None
    value = await conn.fetchval(TIER_RPM_SQL, tier)
    return None if value is None else int(value)


async def assignable_tiers(conn) -> list[str]:
    """The content tiers with a live plan that sets an RPM (the choices the tenant create and PATCH accept)."""
    return [r["tier"] for r in await conn.fetch(ASSIGNABLE_TIERS_SQL)]


async def rate_limits_by_tier(conn) -> dict[str, int]:
    """{tier: rpm} of the live content plans (the billing view's per-plan RPM)."""
    return {r["tier"]: int(r["limit_value"]) for r in await conn.fetch(TIERS_RPM_SQL) if r["limit_value"] is not None}


# ── Catalogue reads ────────────────────────────────────────────────────────────────────────────────────────────
FEATURES_SQL = """
    SELECT feature_key, product, value_kind, unit, description
      FROM shared.entitlement_feature
     WHERE retired_at IS NULL
     ORDER BY CASE product WHEN 'content' THEN 0 WHEN 'crm' THEN 1 WHEN 'tripplanner' THEN 2 ELSE 3 END, feature_key
"""
PLANS_SQL = """
    SELECT p.plan_key, p.product, p.name, p.tier::text AS tier, p.price_usd_monthly::text AS price_usd_monthly,
           p.price_unit, p.seats_included, p.price_extra_seat_usd::text AS price_extra_seat_usd, p.is_public,
           p.version, p.notes, p.updated_at, p.updated_by, p.retired_at,
           (SELECT count(*)::int FROM shared.tenant_subscription s
             WHERE s.plan_key = p.plan_key AND s.status IN ('trialing', 'active', 'past_due')) AS live_subscriptions,
           coalesce((SELECT json_agg(json_build_object('feature_key', pf.feature_key,
                                                       'limit_value', pf.limit_value::text)
                                     ORDER BY pf.feature_key)
                       FROM shared.plan_feature pf WHERE pf.plan_key = p.plan_key), '[]'::json) AS features
      FROM shared.plan p
     WHERE ($1::text IS NULL OR p.product = $1) AND ($2 OR p.retired_at IS NULL)
     ORDER BY CASE p.product WHEN 'content' THEN 0 WHEN 'crm' THEN 1 ELSE 2 END, p.retired_at IS NOT NULL,
              p.price_usd_monthly NULLS LAST, p.plan_key
"""
PLAN_SQL = PLANS_SQL.replace("WHERE ($1::text IS NULL OR p.product = $1) AND ($2 OR p.retired_at IS NULL)",
                             "WHERE p.plan_key = $1")


def _plan_body(row: Any) -> dict:
    features = row["features"]
    features = json.loads(features) if isinstance(features, str) else list(features or [])
    return {
        "plan_key": row["plan_key"], "product": row["product"], "name": row["name"], "tier": row["tier"],
        "price_usd_monthly": row["price_usd_monthly"], "price_unit": row["price_unit"],
        "seats_included": row["seats_included"], "price_extra_seat_usd": row["price_extra_seat_usd"],
        "is_public": row["is_public"], "version": row["version"], "notes": row["notes"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None, "updated_by": row["updated_by"],
        "retired_at": row["retired_at"].isoformat() if row["retired_at"] else None,
        "live_subscriptions": row["live_subscriptions"], "features": features,
    }


async def list_features(conn) -> list[dict]:
    return [dict(r) for r in await conn.fetch(FEATURES_SQL)]


async def list_plans(conn, *, product: Optional[str] = None, include_retired: bool = False) -> list[dict]:
    return [_plan_body(r) for r in await conn.fetch(PLANS_SQL, product, include_retired)]


async def get_plan(conn, plan_key: str) -> Optional[dict]:
    row = await conn.fetchrow(PLAN_SQL, plan_key)
    return _plan_body(row) if row else None


# ── Validation ─────────────────────────────────────────────────────────────────────────────────────────────────
def _decimal(value: Any, what: str, maximum: Decimal) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PlanError("invalid_number", f"{what} must be a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PlanError("invalid_number", f"{what} must be a number") from None
    if not d.is_finite() or d < 0 or d > maximum or d != d.quantize(Decimal("0.01")):
        raise PlanError("invalid_number", f"{what} must be 0-{maximum} with at most 2 decimals")
    return d.quantize(Decimal("0.01"))


parse_decimal = _decimal


def bound_limit(key: str, unit: Optional[str], limit: Optional[Decimal]) -> Optional[Decimal]:
    """``limit`` checked against its unit's bound (module constants): a whole number up to INT4_MAX for a count,
    seat, day or RPM key, an RPM at most RATE_LIMIT_RPM_MAX. Raises :class:`PlanError`."""
    if limit is None or unit not in WHOLE_UNITS:
        return limit
    if limit != limit.to_integral_value():
        if key == RPM_KEY:
            raise PlanError("rate_limit_whole", f"{RPM_KEY} is a whole number of requests a minute")
        raise PlanError("invalid_number", f"{key} is a whole number")
    if key == RPM_KEY and limit > RATE_LIMIT_RPM_MAX:
        raise PlanError("invalid_number", f"{RPM_KEY} must be 1-{RATE_LIMIT_RPM_MAX}")
    if limit > INT4_MAX:
        raise PlanError("invalid_number", f"{key} must be 0-{INT4_MAX}")
    return limit


@dataclass(frozen=True)
class PlanFeature:
    feature_key: str
    limit_value: Optional[Decimal] = None


@dataclass
class PlanDraft:
    """The editable fields of a plan, validated; ``features`` is the full set the plan gives."""
    name: str
    price_usd_monthly: Optional[Decimal]
    price_unit: str
    seats_included: Optional[int]
    price_extra_seat_usd: Optional[Decimal]
    is_public: bool
    notes: Optional[str]
    features: tuple[PlanFeature, ...] = field(default_factory=tuple)

    def columns(self) -> dict:
        return {"name": self.name, "price_usd_monthly": self.price_usd_monthly, "price_unit": self.price_unit,
                "seats_included": self.seats_included, "price_extra_seat_usd": self.price_extra_seat_usd,
                "is_public": self.is_public, "notes": self.notes}


def _features(raw: Any, product: str, catalogue: dict[str, dict]) -> tuple[PlanFeature, ...]:
    if not isinstance(raw, list):
        raise PlanError("invalid_features", "features is a list of {feature_key, limit_value}")
    out: dict[str, PlanFeature] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("feature_key"), str):
            raise PlanError("invalid_features", "each feature is an object with a feature_key")
        key = item["feature_key"]
        f = catalogue.get(key)
        if f is None:
            raise PlanError("unknown_feature", f"no live feature key {key!r}")
        if f["product"] not in (product, "platform"):
            raise PlanError("feature_product_mismatch", f"{key} is a {f['product']} feature, the plan is {product}")
        if f["product"] == "platform" and product != "content":
            # a platform key (AI budget, notice policy) belongs to the base subscription: the resolver takes it from
            # the content plan only (CIS 361), so on an add-on plan it would be shown but never applied
            raise PlanError("platform_key_content_only", f"{key} is set on the content plan, not on a {product} plan")
        if key in out:
            raise PlanError("invalid_features", f"{key} is listed twice")
        limit = bound_limit(key, f.get("unit"), _decimal(item.get("limit_value"), f"{key} limit", LIMIT_MAX))
        if f["value_kind"] == "flag" and limit is not None:
            raise PlanError("flag_takes_no_limit", f"{key} is on/off and takes no limit")
        out[key] = PlanFeature(key, limit)
    return tuple(out[k] for k in sorted(out))


def draft(payload: dict, *, product: str, tier: Optional[str], catalogue: dict[str, dict],
          base: Optional[dict] = None) -> PlanDraft:
    """The validated fields of a create (``base`` None) or of an edit (``base`` = the plan as stored: every field the
    payload leaves out keeps its value). Raises :class:`PlanError`."""
    base = base or {}

    def pick(name, default=None):
        return payload[name] if name in payload else base.get(name, default)

    name = pick("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise PlanError("invalid_name", "name: 1-100 characters")
    unit = pick("price_unit", {"content": "tenant", "crm": "seat", "tripplanner": "site"}[product])
    if unit not in PRICE_UNITS:
        raise PlanError("invalid_price_unit", f"price_unit must be one of {', '.join(PRICE_UNITS)}")
    seats = pick("seats_included")
    if seats is not None and (isinstance(seats, bool) or not isinstance(seats, int) or not 0 <= seats <= 100000):
        raise PlanError("invalid_seats", "seats_included: 0-100000")
    is_public = pick("is_public", False)
    if not isinstance(is_public, bool):
        raise PlanError("invalid_is_public", "is_public is true or false")
    notes = pick("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
        raise PlanError("invalid_notes", "notes: at most 1000 characters")
    notes = notes.strip() or None if isinstance(notes, str) else None
    if "features" in payload:
        features = _features(payload["features"], product, catalogue)
    else:
        features = tuple(PlanFeature(f["feature_key"], _decimal(f.get("limit_value"), f["feature_key"], LIMIT_MAX))
                         for f in base.get("features") or [])
    rpm = next((f for f in features if f.feature_key == RPM_KEY), None)
    if rpm is not None and rpm.limit_value is not None and rpm.limit_value != rpm.limit_value.to_integral_value():
        # shared.tenants.rate_limit_rpm (what the limiter enforces and a plan edit moves) is an integer
        raise PlanError("rate_limit_whole", f"{RPM_KEY} is a whole number of requests a minute")
    if product == "content" and tier is not None:
        if rpm is None or rpm.limit_value is None or rpm.limit_value < 1:
            raise PlanError("rate_limit_required", f"a content plan with a tier sets {RPM_KEY} (requests a minute)")
        given = {f.feature_key: f for f in features}
        for key in ENFORCED_CONTENT_KEYS:
            if key not in given:
                raise PlanError("enforced_limit_required",
                                f"a content plan with a tier lists {key}: removing it would remove the limit for "
                                "every tenant on the plan; give a value instead")
        if given[AI_BUDGET_KEY].limit_value is None and tier not in UNLIMITED_AI_TIERS:
            raise PlanError("enforced_limit_required",
                            f"{AI_BUDGET_KEY} needs a number on the {tier} plan (0 = no AI spend included)")
    return PlanDraft(name=name.strip(), price_usd_monthly=_decimal(pick("price_usd_monthly"), "price", PRICE_MAX),
                     price_unit=unit, seats_included=seats,
                     price_extra_seat_usd=_decimal(pick("price_extra_seat_usd"), "extra seat price", PRICE_MAX),
                     is_public=is_public, notes=notes, features=features)


# ── Writes ─────────────────────────────────────────────────────────────────────────────────────────────────────
LOCK_PLAN_SQL = "SELECT plan_key, version, retired_at FROM shared.plan WHERE plan_key = $1 FOR UPDATE"
INSERT_PLAN_SQL = """
    INSERT INTO shared.plan (plan_key, product, name, tier, price_usd_monthly, price_unit, seats_included,
                             price_extra_seat_usd, is_public, notes, updated_by)
    VALUES ($1, $2, $3, $4::plan_tier_enum, $5, $6, $7, $8, $9, $10, $11)
"""
UPDATE_PLAN_SQL = """
    UPDATE shared.plan
       SET name = $2, price_usd_monthly = $3, price_unit = $4, seats_included = $5, price_extra_seat_usd = $6,
           is_public = $7, notes = $8, version = version + 1, updated_at = now(), updated_by = $9
     WHERE plan_key = $1
"""
RETIRE_PLAN_SQL = """
    UPDATE shared.plan SET retired_at = now(), version = version + 1, updated_at = now(), updated_by = $2
     WHERE plan_key = $1
"""
# A plan's RPM edit moves the limiter's column of every tenant that follows the plan (S266 review of slice 3):
# shared.tenants.rate_limit_rpm is what the public API limiter and the Lambda authorizer enforce, copied from the plan
# at create. A tenant on this plan (content subscription trialing / active / past_due) whose column still equals the
# old plan value follows the default and moves; a tenant whose column differs keeps its own RPM (a per-tenant value
# set with PATCH /admin/tenants/{id}). The CIS 356 trigger tenants_rate_limit_resync then re-syncs each one (source
# plan again, entitlement.changed with aa.actor = the admin).
FOLLOW_RPM_SQL = """
    UPDATE shared.tenants t
       SET rate_limit_rpm = $2
     WHERE t.rate_limit_rpm = $3
       AND EXISTS (SELECT 1 FROM shared.tenant_subscription s
                    WHERE s.tenant_id = t.tenant_id AND s.product = 'content' AND s.plan_key = $1
                      AND s.status IN ('trialing', 'active', 'past_due'))
    RETURNING t.tenant_id
"""
LIVE_TIER_SQL = """
    SELECT plan_key FROM shared.plan
     WHERE product = 'content' AND tier = $1::plan_tier_enum AND retired_at IS NULL
"""
DELETE_FEATURE_SQL = "DELETE FROM shared.plan_feature WHERE plan_key = $1 AND feature_key = $2"
UPSERT_FEATURE_SQL = """
    INSERT INTO shared.plan_feature AS pf (plan_key, feature_key, limit_value, updated_by)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (plan_key, feature_key) DO UPDATE
       SET limit_value = EXCLUDED.limit_value, updated_at = now(), updated_by = EXCLUDED.updated_by
     WHERE pf.limit_value IS DISTINCT FROM EXCLUDED.limit_value
"""


async def _catalogue(conn) -> dict[str, dict]:
    return {f["feature_key"]: f for f in await list_features(conn)}


async def _write_features(conn, plan_key: str, before: tuple[PlanFeature, ...], after: tuple[PlanFeature, ...],
                          actor: str) -> list[str]:
    """Applies the feature set difference; returns the keys that changed (added, removed or a new limit)."""
    old = {f.feature_key: f.limit_value for f in before}
    new = {f.feature_key: f.limit_value for f in after}
    changed: list[str] = []
    for key in sorted(old.keys() - new.keys()):
        await conn.execute(DELETE_FEATURE_SQL, plan_key, key)
        changed.append(key)
    for key in sorted(new):
        if key not in old or old[key] != new[key]:
            await conn.execute(UPSERT_FEATURE_SQL, plan_key, key, new[key], actor)
            changed.append(key)
    return changed


def _snapshot(d: PlanDraft) -> dict:
    out = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.columns().items()}
    out["features"] = {f.feature_key: (None if f.limit_value is None else str(f.limit_value)) for f in d.features}
    return out


async def _audit(conn, actor: str, action: str, plan_key: str, details: dict) -> None:
    # last in the transaction: the audit chain trigger holds a transaction-scoped lock (QRY-4)
    await write_audit_log(conn, tenant_id=None, actor=actor, action=action, resource_type="plan",
                          resource_id=plan_key, details=json.loads(json.dumps(details, default=str)))


async def create_plan(conn, payload: dict, *, actor: str) -> dict:
    """A new plan with its features, in one transaction (``conn`` is a pool connection)."""
    plan_key, product, tier = payload.get("plan_key"), payload.get("product"), payload.get("tier")
    if product not in PRODUCTS:
        raise PlanError("invalid_product", f"product must be one of {', '.join(PRODUCTS)}")
    if not isinstance(plan_key, str) or len(plan_key) > 64 or not PLAN_KEY_RE.match(plan_key) \
            or plan_key.split(".")[0] != product:
        raise PlanError("invalid_plan_key", f"plan_key is {product}.<name>: lower-case letters, digits and '_'")
    if tier is not None and (product != "content" or tier not in TIERS):
        raise PlanError("invalid_tier", f"only a content plan has a tier, one of {', '.join(TIERS)}")
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        d = draft(payload, product=product, tier=tier, catalogue=await _catalogue(conn))
        if await conn.fetchval("SELECT 1 FROM shared.plan WHERE plan_key = $1", plan_key):
            raise PlanError("plan_exists", f"plan {plan_key} already exists", 409)
        if tier is not None:
            taken = await conn.fetchval(LIVE_TIER_SQL, tier)
            if taken:
                raise PlanError("tier_taken", f"{taken} is the live {tier} plan: retire it first", 409)
        await conn.execute(INSERT_PLAN_SQL, plan_key, product, d.name, tier, d.price_usd_monthly, d.price_unit,
                           d.seats_included, d.price_extra_seat_usd, d.is_public, d.notes, actor)
        await _write_features(conn, plan_key, (), d.features, actor)
        await _audit(conn, actor, CREATED, plan_key,
                     {"product": product, "tier": tier, "version": 1, "after": _snapshot(d)})
    return await get_plan(conn, plan_key)


def _rpm_of(d: PlanDraft) -> Optional[int]:
    v = next((f.limit_value for f in d.features if f.feature_key == RPM_KEY), None)
    return None if v is None else int(v)


async def _follow_rpm(conn, plan_key: str, product: str, before: PlanDraft, after: PlanDraft) -> int:
    """Moves ``shared.tenants.rate_limit_rpm`` of the tenants that follow this content plan's RPM (FOLLOW_RPM_SQL);
    returns how many moved. A plan that drops the key or gains it moves no one (no old / new default to follow)."""
    old, new = _rpm_of(before), _rpm_of(after)
    if product != "content" or old is None or new is None or old == new:
        return 0
    return len(await conn.fetch(FOLLOW_RPM_SQL, plan_key, new, old))


async def update_plan(conn, plan_key: str, payload: dict, *, actor: str) -> dict:
    """Edits a live plan made from ``payload['version']``; bumps the version. Raises :class:`PlanError`."""
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanError("version_required", "send the version the edit was made from")
    for frozen in ("plan_key", "product", "tier"):
        if frozen in payload:
            raise PlanError("immutable_field", f"{frozen} never changes; create a new plan instead")
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        locked = await conn.fetchrow(LOCK_PLAN_SQL, plan_key)
        if locked is None:
            raise PlanError("plan_not_found", "no plan with this key", 404)
        if locked["retired_at"] is not None:
            raise PlanError("plan_retired", "a retired plan is frozen", 409)
        if locked["version"] != version:
            raise PlanError("version_conflict",
                            f"the plan changed since (now version {locked['version']}): reload and edit again", 409)
        stored = await get_plan(conn, plan_key)
        catalogue = await _catalogue(conn)
        before = draft({}, product=stored["product"], tier=None, catalogue=catalogue, base=stored)
        after = draft(payload, product=stored["product"], tier=stored["tier"], catalogue=catalogue, base=stored)
        dropped = {f.feature_key for f in before.features} - {f.feature_key for f in after.features}
        removed_limits = sorted(k for k in dropped if (catalogue.get(k) or {}).get("value_kind") in ("limit", "seats"))
        if removed_limits:
            # "no row" = no limit for every subscriber at once (S266 review): an edit sets the limit, explicitly
            raise PlanError("limit_removal_refused",
                            f"{', '.join(removed_limits)}: a limit is not removed from a plan; set its value "
                            "(empty = no limit) instead")
        fields_changed = [k for k, v in after.columns().items() if before.columns()[k] != v]
        if not fields_changed and before.features == after.features:
            return {**stored, "changed": []}
        await conn.execute(UPDATE_PLAN_SQL, plan_key, after.name, after.price_usd_monthly, after.price_unit,
                           after.seats_included, after.price_extra_seat_usd, after.is_public, after.notes, actor)
        features_changed = await _write_features(conn, plan_key, before.features, after.features, actor)
        rpm_followed = await _follow_rpm(conn, plan_key, stored["product"], before, after) \
            if RPM_KEY in features_changed else 0
        await _audit(conn, actor, UPDATED, plan_key,
                     {"version": version + 1, "fields": fields_changed, "features": features_changed,
                      "live_subscriptions": stored["live_subscriptions"], "rate_limit_followed": rpm_followed,
                      "before": _snapshot(before), "after": _snapshot(after)})
    out = await get_plan(conn, plan_key)
    return {**out, "changed": fields_changed + [f"feature:{k}" for k in features_changed],
            "rate_limit_followed": rpm_followed}


async def retire_plan(conn, plan_key: str, *, version: Any, reason: Optional[str], actor: str) -> dict:
    """Retires a live plan: no new subscription takes it; current subscribers keep it. Raises :class:`PlanError`."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanError("version_required", "send the version the retire was decided on")
    reason = (reason or "").strip()[:500] or None
    async with conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        locked = await conn.fetchrow(LOCK_PLAN_SQL, plan_key)
        if locked is None:
            raise PlanError("plan_not_found", "no plan with this key", 404)
        if locked["retired_at"] is not None:
            raise PlanError("plan_retired", "this plan is already retired", 409)
        if locked["version"] != version:
            raise PlanError("version_conflict", f"the plan changed since (now version {locked['version']})", 409)
        stored = await get_plan(conn, plan_key)
        await conn.execute(RETIRE_PLAN_SQL, plan_key, actor)
        await _audit(conn, actor, RETIRED, plan_key,
                     {"version": version + 1, "reason": reason, "live_subscriptions": stored["live_subscriptions"],
                      "tier": stored["tier"]})
    return await get_plan(conn, plan_key)
