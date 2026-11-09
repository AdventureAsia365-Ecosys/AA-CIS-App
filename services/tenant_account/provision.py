"""The ``tenant_provision`` saga (AA-1028 R1 slice 2, root ADR 0019 d1): one action creates a tenant with its content
subscription and add-ons, or adds add-ons to an existing tenant. It replaces the hand steps of the S265 design
(README §1.3 / §3.6): the CRM and Booking projections then follow the account event feed by themselves
(``crm_entitlement_pull`` in AA-Partner, ``bk_tenant_pull`` in AA-Booking).

Three modes (``ProvisionRequest``, validated again in the job because the payload comes back from the database):

- ``create``: a new ``shared.tenants`` row (the same rows as ``POST /admin/tenants``: the ``default`` brand rules
  row); there are no tenant API keys (AA-1036 R3 slice 3, root ADR 0019 d6): people join through a studio
  invitation. The CIS 355 bridge trigger creates the content subscription on
  the live plan of ``plan_tier``. Without ``trial_days`` the account and the subscription are active. With
  ``trial_days`` (slice 3) the account is ``trial`` and the content subscription (and every add-on of the request)
  ``trialing`` until ``trial_ends_at``; the ``tenant_account_tick`` job ends it (``services/tenant_account/
  lifecycle.py``). The Dev guard (``dev_guard``, root ADR 0019 d16): on Dev the default ``kind`` is ``demo`` and an
  ``agency`` only as the one pilot (``pilot_weeks``). No ``acp_quota_ledger`` row any more (slice 3: the ACPv1
  S2/S3/S4 ledger has no reader; the limits are entitlements).
- ``attach``: add-ons for an existing tenant that is not offboarding or closed (an add-on needs a live content row,
  the CIS 353 guard). With ``only_new`` (set only by an approved add-on request, ``addon_requests.approve``; the admin
  API bodies cannot carry it) an add-on that is already live is left exactly as it is (step ``<product>_already_on``):
  the request's decider (the account manager, ``tenant.book.write``) may turn an add-on on, never change the plan or
  the seats of a live one (``tenant.write`` / ``billing.write``), even when the add-on went live between the approval
  and the job. Every attach then closes the tenant's pending add-on requests of the products it made live (approved,
  ``decided_by`` the saga actor, the plan the subscription has, audited ``tenant.addon_request_approved``), so no
  stale request is left to approve.
- ``detach`` (slice 3): cancel add-ons (``products``) of an existing tenant: the subscription row stays, status
  ``canceled``; its plan's keys and the add-on's overrides stop applying (the resolver), and the CRM / Booking
  projections follow the feed. This is the revocation path that replaced the CRM's L1 entitlement write.

An add-on is an upsert of ``shared.tenant_subscription`` (``crm`` / ``tripplanner``, a live plan of that product,
optional seats): a canceled one is active again. The CIS 353 triggers write ``subscription.changed`` and one
``entitlement.changed`` per changed key; the saga sets ``aa.actor`` = ``job:tenant_provision:<job id>`` so the feed
rows carry it (never served).

Idempotent per request key (the client's ``request_key``, the job's idempotency key ``tenant_provision:<key>``):
every database step runs in **one transaction** that also writes the ``acp_shared.audit_log`` row naming the key, so
a retry after a commit finds that row and returns the stored outcome (``resumed``) instead of writing twice, and a
retry after a rollback starts again from nothing. A slug that exists without this key's audit row is a permanent
failure (``slug_taken``). Steps are reported in the job's progress (``phase``) and its result.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_account import dev_guard

KIND = "tenant_provision"
IDEMPOTENCY_PREFIX = f"{KIND}:"
TIERS = ("starter", "growth", "business")
KINDS = ("agency", "demo")
ADDON_PRODUCTS = ("crm", "tripplanner")
SEATS_MAX = 500
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
PLAN_KEY_RE = re.compile(r"^[a-z]+\.[a-z][a-z0-9_]*$")
TRIAL_DAYS_MAX = 60
MODES = ("create", "attach", "detach")
ONBOARD = AdminTenantAuditAction.ONBOARD
SUBSCRIPTION_CHANGED = AdminTenantAuditAction.SUBSCRIPTION_CHANGED
ADDON_REQUEST_APPROVED = AdminTenantAuditAction.ADDON_REQUEST_APPROVED


class ProvisionError(ValueError):
    """A request the saga refuses; ``code`` is the stable reason (job error, API 409 / 422)."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class AddOn:
    product: str
    plan_key: str
    seats: Optional[int] = None

    def to_payload(self) -> dict[str, Any]:
        return {"product": self.product, "plan_key": self.plan_key, "seats": self.seats}


@dataclass(frozen=True)
class ProvisionRequest:
    request_key: str
    mode: str                                   # create | attach
    tenant_id: Optional[str] = None             # attach
    name: Optional[str] = None                  # create
    slug: Optional[str] = None
    plan_tier: Optional[str] = None
    posts_per_week: int = 3
    kind: str = "agency"
    add_ons: tuple[AddOn, ...] = field(default_factory=tuple)
    requested_by: Optional[str] = None
    trial_days: Optional[int] = None            # create: the account starts in trial
    pilot_weeks: Optional[int] = None           # create, Dev only: the one pilot agency (root ADR 0019 d16)
    cancel: tuple[str, ...] = field(default_factory=tuple)   # detach: the add-on products to cancel
    only_new: bool = False                      # attach: never change a live add-on (an approved add-on request)

    @property
    def idempotency_key(self) -> str:
        return IDEMPOTENCY_PREFIX + self.request_key

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"request_key": self.request_key, "mode": self.mode,
                               "add_ons": [a.to_payload() for a in self.add_ons]}
        if self.mode == "create":
            out.update(name=self.name, slug=self.slug, plan_tier=self.plan_tier, posts_per_week=self.posts_per_week,
                       kind=self.kind)
            if self.trial_days is not None:
                out["trial_days"] = self.trial_days
            if self.pilot_weeks is not None:
                out["pilot_weeks"] = self.pilot_weeks
        else:
            out["tenant_id"] = self.tenant_id
        if self.mode == "detach":
            out["products"] = list(self.cancel)
        if self.only_new:
            out["only_new"] = True
        if self.requested_by:
            out["requested_by"] = self.requested_by
        return out


def _add_ons(raw: Any) -> tuple[AddOn, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > len(ADDON_PRODUCTS):
        raise ProvisionError("invalid_add_ons", "add_ons is a list of at most one crm and one tripplanner add-on")
    out: list[AddOn] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProvisionError("invalid_add_ons", "each add-on is an object")
        product, plan_key, seats = item.get("product"), item.get("plan_key"), item.get("seats")
        if product not in ADDON_PRODUCTS:
            raise ProvisionError("invalid_add_ons", f"product must be one of {', '.join(ADDON_PRODUCTS)}")
        if not isinstance(plan_key, str) or not PLAN_KEY_RE.match(plan_key) or plan_key.split(".")[0] != product:
            raise ProvisionError("invalid_add_ons", f"plan_key must be a {product}.<plan> key")
        if seats is not None and (isinstance(seats, bool) or not isinstance(seats, int) or not 1 <= seats <= SEATS_MAX):
            raise ProvisionError("invalid_add_ons", f"seats must be 1-{SEATS_MAX}")
        out.append(AddOn(product, plan_key, seats))
    if len({a.product for a in out}) != len(out):
        raise ProvisionError("invalid_add_ons", "one add-on per product")
    return tuple(out)


def parse(payload: dict[str, Any]) -> ProvisionRequest:
    """The request from an API body or a stored job payload; raises :class:`ProvisionError`."""
    if not isinstance(payload, dict):
        raise ProvisionError("invalid_request", "the request is an object")
    key = payload.get("request_key")
    if not isinstance(key, str) or not REQUEST_KEY_RE.match(key):
        raise ProvisionError("invalid_request_key", "request_key: 8-64 letters, digits, '.', '_', ':' or '-'")
    mode = payload.get("mode")
    if mode not in MODES:
        raise ProvisionError("invalid_request", "mode must be create, attach or detach")
    add_ons = _add_ons(payload.get("add_ons")) if mode != "detach" else ()
    requested_by = payload.get("requested_by")
    requested_by = requested_by[:40] if isinstance(requested_by, str) and requested_by else None
    if mode in ("attach", "detach"):
        try:
            tenant_id = str(uuid.UUID(str(payload.get("tenant_id"))))
        except ValueError:
            raise ProvisionError("invalid_tenant", "tenant_id must be a UUID") from None
        if mode == "attach":
            if not add_ons:
                raise ProvisionError("invalid_add_ons", "attach needs at least one add-on")
            return ProvisionRequest(request_key=key, mode=mode, tenant_id=tenant_id, add_ons=add_ons,
                                    requested_by=requested_by, only_new=payload.get("only_new") is True)
        products = payload.get("products")
        if not isinstance(products, list) or not products or any(p not in ADDON_PRODUCTS for p in products) \
                or len(set(products)) != len(products):
            raise ProvisionError("invalid_products",
                                 f"products: one or more of {', '.join(ADDON_PRODUCTS)}, each once")
        return ProvisionRequest(request_key=key, mode=mode, tenant_id=tenant_id,
                                cancel=tuple(sorted(products)), requested_by=requested_by)
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
        raise ProvisionError("invalid_name", "name: 1-255 characters")
    slug = payload.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ProvisionError("invalid_slug", "slug: lower-case letters, digits and '-', at most 100")
    tier = payload.get("plan_tier")
    if tier not in TIERS:
        raise ProvisionError("invalid_plan_tier", f"plan_tier must be one of {', '.join(TIERS)}")
    ppw = payload.get("posts_per_week", 3)
    if isinstance(ppw, bool) or not isinstance(ppw, int) or not 1 <= ppw <= 14:
        raise ProvisionError("invalid_posts_per_week", "posts_per_week: 1-14")
    kind = payload.get("kind") or dev_guard.default_kind()
    if kind not in KINDS:
        raise ProvisionError("invalid_kind", f"kind must be one of {', '.join(KINDS)}")
    trial_days = payload.get("trial_days")
    if trial_days is not None and (isinstance(trial_days, bool) or not isinstance(trial_days, int)
                                   or not 1 <= trial_days <= TRIAL_DAYS_MAX):
        raise ProvisionError("invalid_trial_days", f"trial_days: 1-{TRIAL_DAYS_MAX}")
    pilot_weeks = payload.get("pilot_weeks")
    refused = dev_guard.refusal(kind, pilot_weeks)
    if refused is not None:
        raise ProvisionError(*refused)
    return ProvisionRequest(request_key=key, mode=mode, name=name.strip(), slug=slug, plan_tier=tier,
                            posts_per_week=ppw, kind=kind, add_ons=add_ons, requested_by=requested_by,
                            trial_days=trial_days, pilot_weeks=pilot_weeks)


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
DONE_SQL = """
    SELECT tenant_id::text AS tenant_id, details FROM acp_shared.audit_log
     WHERE action = ANY($2::text[]) AND details->>'provision_request_key' = $1
     ORDER BY created_at LIMIT 1
"""
SLUG_SQL = "SELECT tenant_id FROM shared.tenants WHERE slug = $1"
CONTENT_PLAN_SQL = """
    SELECT p.plan_key, p.membership_plan_id, pf.limit_value AS rpm
      FROM shared.plan p
      LEFT JOIN shared.plan_feature pf ON pf.plan_key = p.plan_key AND pf.feature_key = 'content.rate_limit_rpm'
     WHERE p.product = 'content' AND p.tier = $1::plan_tier_enum AND p.retired_at IS NULL
"""
INSERT_TENANT_SQL = """
    INSERT INTO shared.tenants (name, slug, plan_tier, plan_id, posts_per_week, rate_limit_rpm,
                                is_active, kind, is_canary, account_status, is_pilot, pilot_ends_at)
    VALUES ($1, $2, $3::plan_tier_enum, $4, $5, $6, true, $7, $8, $9, $10,
            CASE WHEN $10 THEN now() + make_interval(weeks => $11::int) END)
    RETURNING tenant_id
"""
PILOT_SQL = "SELECT tenant_id FROM shared.tenants WHERE is_pilot"
# slice 3: the content row the 355 bridge made (active) starts the trial instead
TRIAL_CONTENT_SQL = """
    UPDATE shared.tenant_subscription
       SET status = 'trialing', trial_ends_at = now() + make_interval(days => $2::int), updated_by = $3
     WHERE tenant_id = $1 AND product = 'content'
    RETURNING trial_ends_at
"""
BRAND_SQL = """
    INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name)
    SELECT $1, 'default' WHERE NOT EXISTS (SELECT 1 FROM shared.tenant_brand_rules WHERE tenant_id = $1)
"""
TENANT_SQL = "SELECT tenant_id, account_status FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
ADDON_PLAN_SQL = "SELECT 1 FROM shared.plan WHERE plan_key = $1 AND product = $2 AND retired_at IS NULL"
# A live add-on stays as it is unless the plan or the seats change; a canceled or paused one is active again. A new
# add-on of a trial create ($6 = the content trial's end) starts trialing with it.
UPSERT_ADDON_SQL = """
    INSERT INTO shared.tenant_subscription AS s (tenant_id, product, plan_key, status, seats, updated_by, trial_ends_at)
    VALUES ($1, $2, $3, CASE WHEN $6::timestamptz IS NULL THEN 'active' ELSE 'trialing' END, $4, $5, $6::timestamptz)
    ON CONFLICT (tenant_id, product) DO UPDATE
       SET plan_key = EXCLUDED.plan_key, seats = EXCLUDED.seats, updated_by = EXCLUDED.updated_by,
           status = CASE WHEN s.status IN ('trialing', 'active', 'past_due') THEN s.status ELSE 'active' END,
           trial_ends_at = CASE WHEN s.status = 'trialing' THEN s.trial_ends_at END,
           canceled_at = NULL
     WHERE s.plan_key IS DISTINCT FROM EXCLUDED.plan_key OR s.seats IS DISTINCT FROM EXCLUDED.seats
        OR s.status NOT IN ('trialing', 'active', 'past_due')
    RETURNING s.product
"""
# only_new (an approved add-on request): insert, or turn on a row that is not live (canceled, paused, unpaid, a trial
# that has ended: the agency's view and LIVE_SQL of addon_requests.py); a live row is never changed.
NEW_ADDON_SQL = """
    INSERT INTO shared.tenant_subscription AS s (tenant_id, product, plan_key, status, seats, updated_by)
    VALUES ($1, $2, $3, 'active', $4, $5)
    ON CONFLICT (tenant_id, product) DO UPDATE
       SET plan_key = EXCLUDED.plan_key, seats = EXCLUDED.seats, updated_by = EXCLUDED.updated_by, status = 'active',
           trial_ends_at = NULL, canceled_at = NULL
     WHERE NOT (s.status IN ('active', 'past_due') OR (s.status = 'trialing' AND s.trial_ends_at > now()))
    RETURNING s.product
"""
# An attach closes the pending add-on requests (CIS 374) of the products that are live after it: approved with the
# plan the subscription has. decided_at is never before requested_at (a request committed after this transaction
# began has a later now()).
ADDON_REQUEST_TABLE_SQL = "SELECT to_regclass('shared.tenant_addon_request') IS NOT NULL"
CLOSE_REQUESTS_SQL = """
    UPDATE shared.tenant_addon_request r
       SET status = 'approved', decided_at = GREATEST(now(), r.requested_at), decided_by = $3, decision_note = $4,
           plan_key = s.plan_key, provision_job_id = $5::uuid
      FROM shared.tenant_subscription s
     WHERE r.tenant_id = $1 AND r.product = ANY($2::text[]) AND r.status = 'pending'
       AND s.tenant_id = r.tenant_id AND s.product = r.product
       AND (s.status IN ('active', 'past_due') OR (s.status = 'trialing' AND s.trial_ends_at > now()))
    RETURNING r.request_id::text AS request_id, r.product, r.plan_key
"""
REQUEST_CLOSED_NOTE = "The add-on was turned on by Adventure Asia."
CANCEL_ADDON_SQL = """
    UPDATE shared.tenant_subscription
       SET status = 'canceled', canceled_at = now(), updated_by = $3
     WHERE tenant_id = $1 AND product = $2 AND status <> 'canceled'
    RETURNING product
"""
SUBS_SQL = """
    SELECT product, plan_key, status, seats FROM shared.tenant_subscription WHERE tenant_id = $1 ORDER BY product
"""
EVENTS_SQL = "SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = $1 AND actor = $2"


def actor_for(job_id: str) -> str:
    return f"job:{KIND}:{job_id}"[:64]


async def _create(conn, req: ProvisionRequest, actor: str,
                  steps: list[str]) -> tuple[uuid.UUID, list[str], Optional[Any]]:
    """The tenant row (and the trial start); returns (tenant_id, warnings, trial_ends_at or None)."""
    warnings: list[str] = []
    if await conn.fetchval(SLUG_SQL, req.slug) is not None:
        raise ProvisionError("slug_taken", f"slug {req.slug!r} already exists")
    plan = await conn.fetchrow(CONTENT_PLAN_SQL, req.plan_tier)
    if plan is None:
        raise ProvisionError("no_content_plan", f"no live content plan for plan_tier {req.plan_tier}")
    if plan["rpm"] is None:
        raise ProvisionError("no_rate_limit", f"plan {plan['plan_key']} sets no content.rate_limit_rpm")
    if plan["membership_plan_id"] is None:
        warnings.append("membership_plan_missing")      # quota / price show 0 / inf until it is linked (AA-797)
    pilot = req.pilot_weeks is not None
    if pilot and await conn.fetchval(PILOT_SQL) is not None:
        raise ProvisionError("pilot_taken", "Dev already has its one pilot tenant (root ADR 0019 d16)")
    trial = req.trial_days is not None
    tenant_id = await conn.fetchval(INSERT_TENANT_SQL, req.name, req.slug, req.plan_tier, plan["membership_plan_id"],
                                    req.posts_per_week, int(plan["rpm"]), req.kind, req.kind == "demo",
                                    "trial" if trial else "active", pilot, req.pilot_weeks or 0)
    await conn.execute(BRAND_SQL, tenant_id)
    steps.append("tenant_created")
    if pilot:
        steps.append("pilot_marked")
    # the 355 bridge trigger created the content subscription (active) on the tier's live plan; a trial starts it
    trial_ends_at = None
    if trial:
        trial_ends_at = await conn.fetchval(TRIAL_CONTENT_SQL, tenant_id, req.trial_days, actor)
        steps.append("content_trialing")
    else:
        steps.append("content_active")
    return tenant_id, warnings, trial_ends_at


async def _attach_target(conn, req: ProvisionRequest) -> uuid.UUID:
    row = await conn.fetchrow(TENANT_SQL, req.tenant_id)
    if row is None:
        raise ProvisionError("tenant_not_found", "no tenant with this id")
    if row["account_status"] in ("offboarding", "closed"):
        if req.mode == "detach":
            raise ProvisionError("tenant_offboarded", "an offboarded tenant has no live add-on")
        raise ProvisionError("tenant_offboarded", "restore the tenant before adding an add-on")
    return row["tenant_id"]


async def _add_ons_step(conn, tenant_id: uuid.UUID, req: ProvisionRequest, actor: str, steps: list[str],
                        trial_ends_at: Optional[Any] = None) -> None:
    for a in req.add_ons:
        if await conn.fetchval(ADDON_PLAN_SQL, a.plan_key, a.product) is None:
            raise ProvisionError("unknown_plan", f"no live plan {a.plan_key}")
        if req.only_new:
            changed = await conn.fetchval(NEW_ADDON_SQL, tenant_id, a.product, a.plan_key, a.seats, actor)
            steps.append(f"{a.product}_{'subscribed' if changed else 'already_on'}")
            continue
        changed = await conn.fetchval(UPSERT_ADDON_SQL, tenant_id, a.product, a.plan_key, a.seats, actor,
                                      trial_ends_at)
        steps.append(f"{a.product}_{'subscribed' if changed else 'unchanged'}")


async def _close_requests(conn, tenant_id: uuid.UUID, req: ProvisionRequest, actor: str, job_id: str,
                          steps: list[str]) -> list[dict]:
    """Closes the tenant's pending add-on requests of the products this attach left live; returns them (the caller
    audits each one). Nothing before CIS 374 is applied."""
    if not req.add_ons or not await conn.fetchval(ADDON_REQUEST_TABLE_SQL):
        return []
    rows = [dict(r) for r in await conn.fetch(CLOSE_REQUESTS_SQL, tenant_id, [a.product for a in req.add_ons],
                                              actor, REQUEST_CLOSED_NOTE, job_id)]
    steps.extend(f"{r['product']}_request_closed" for r in rows)
    return rows


async def _cancel_step(conn, tenant_id: uuid.UUID, req: ProvisionRequest, actor: str, steps: list[str]) -> None:
    for product in req.cancel:
        canceled = await conn.fetchval(CANCEL_ADDON_SQL, tenant_id, product, actor)
        steps.append(f"{product}_{'canceled' if canceled else 'unchanged'}")


def _result(req: ProvisionRequest, tenant_id: str, *, steps: list[str], subscriptions: list[dict], events: int,
            warnings: list[str], resumed: bool) -> dict[str, Any]:
    return {
        "kind": KIND, "mode": req.mode, "request_key": req.request_key, "tenant_id": tenant_id,
        "resumed": resumed, "steps": steps, "subscriptions": subscriptions, "events_written": events,
        "warnings": warnings,
    }


async def provision(pool, req: ProvisionRequest, *, job_id: str) -> dict[str, Any]:
    """Runs the saga's database steps in one transaction; returns the job result. Raises :class:`ProvisionError`."""
    actor = actor_for(job_id)
    actions = [ONBOARD, SUBSCRIPTION_CHANGED]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('aa.actor', $1, true)", actor)
        await conn.execute(LOCK_SQL, req.idempotency_key)
        done = await conn.fetchrow(DONE_SQL, req.request_key, actions)
        if done is not None:
            details = done["details"]
            details = json.loads(details) if isinstance(details, str) else (details or {})
            return _result(req, done["tenant_id"], steps=list(details.get("steps") or []),
                           subscriptions=list(details.get("subscriptions") or []),
                           events=int(details.get("events_written") or 0),
                           warnings=list(details.get("warnings") or []), resumed=True)
        steps: list[str] = []
        warnings: list[str] = []
        trial_ends_at = None
        if req.mode == "create":
            tenant_id, warnings, trial_ends_at = await _create(conn, req, actor, steps)
        else:
            tenant_id = await _attach_target(conn, req)
        closed: list[dict] = []
        if req.mode == "detach":
            await _cancel_step(conn, tenant_id, req, actor, steps)
        else:
            await _add_ons_step(conn, tenant_id, req, actor, steps, trial_ends_at)
            if req.mode == "attach":
                closed = await _close_requests(conn, tenant_id, req, actor, job_id, steps)
        subs = [dict(r) for r in await conn.fetch(SUBS_SQL, tenant_id)]
        events = int(await conn.fetchval(EVENTS_SQL, tenant_id, actor) or 0)
        details = {
            "provision_request_key": req.request_key, "job_id": job_id, "mode": req.mode, "steps": steps,
            "subscriptions": subs, "events_written": events, "warnings": warnings,
            "requested_by": req.requested_by,
        }
        if req.mode == "create":
            details.update(name=req.name, plan_tier=req.plan_tier, posts_per_week=req.posts_per_week, kind=req.kind,
                           trial_days=req.trial_days, trial_ends_at=trial_ends_at, pilot_weeks=req.pilot_weeks)
        # last in the transaction: the audit chain trigger holds a transaction-scoped lock (QRY-4)
        for r in closed:
            await write_audit_log(conn, tenant_id=str(tenant_id), actor=actor, action=ADDON_REQUEST_APPROVED,
                                  resource_type="tenant_addon_request", resource_id=r["request_id"],
                                  details={"product": r["product"], "plan_key": r["plan_key"], "seats": None,
                                           "provision_job_id": job_id, "closed_by_attach": req.request_key})
        await write_audit_log(conn, tenant_id=str(tenant_id), actor=actor,
                              action=ONBOARD if req.mode == "create" else SUBSCRIPTION_CHANGED,
                              resource_type="tenant", resource_id=str(tenant_id),
                              details=json.loads(json.dumps(details, default=str)))
    return _result(req, str(tenant_id), steps=steps, subscriptions=subs, events=events, warnings=warnings,
                   resumed=False)
