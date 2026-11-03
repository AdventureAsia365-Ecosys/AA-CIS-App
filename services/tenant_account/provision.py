"""The ``tenant_provision`` saga (AA-1028 R1 slice 2, root ADR 0019 d1): one action creates a tenant with its content
subscription and add-ons, or adds add-ons to an existing tenant. It replaces the hand steps of the S265 design
(README §1.3 / §3.6): the CRM and Booking projections then follow the account event feed by themselves
(``crm_entitlement_pull`` in AA-Partner, ``bk_tenant_pull`` in AA-Booking).

Two modes (``ProvisionRequest``, validated again in the job because the payload comes back from the database):

- ``create``: a new ``shared.tenants`` row (the same rows as ``POST /admin/tenants``: quota ledger 10/10/50, the
  ``default`` brand rules row), with **no API key** (``api_key_hash`` NULL: a key is shown once, so it is never put
  in a job; staff issue it with ``POST /admin/tenants/{id}/generate-key``). The CIS 355 bridge trigger creates the
  content subscription on the live plan of ``plan_tier``; with ``trial_days`` the saga makes it ``trialing`` until
  ``now() + trial_days`` and the account ``trial``. Then the add-ons.
- ``attach``: add-ons for an existing tenant that is not offboarding or closed (an add-on needs a live content row,
  the CIS 353 guard).

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

KIND = "tenant_provision"
IDEMPOTENCY_PREFIX = f"{KIND}:"
TIERS = ("starter", "growth", "business")
KINDS = ("agency", "demo")
ADDON_PRODUCTS = ("crm", "tripplanner")
TRIAL_DAYS_MAX = 60
SEATS_MAX = 500
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
PLAN_KEY_RE = re.compile(r"^[a-z]+\.[a-z][a-z0-9_]*$")
QUOTA_DEFAULTS = (10, 10, 50)          # acp_quota_ledger s2 / s3 / s4, as POST /admin/tenants
ONBOARD = AdminTenantAuditAction.ONBOARD
SUBSCRIPTION_CHANGED = AdminTenantAuditAction.SUBSCRIPTION_CHANGED


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
    trial_days: Optional[int] = None
    add_ons: tuple[AddOn, ...] = field(default_factory=tuple)
    requested_by: Optional[str] = None

    @property
    def idempotency_key(self) -> str:
        return IDEMPOTENCY_PREFIX + self.request_key

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"request_key": self.request_key, "mode": self.mode,
                               "add_ons": [a.to_payload() for a in self.add_ons]}
        if self.mode == "create":
            out.update(name=self.name, slug=self.slug, plan_tier=self.plan_tier, posts_per_week=self.posts_per_week,
                       kind=self.kind, trial_days=self.trial_days)
        else:
            out["tenant_id"] = self.tenant_id
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
    add_ons = _add_ons(payload.get("add_ons"))
    requested_by = payload.get("requested_by")
    requested_by = requested_by[:40] if isinstance(requested_by, str) and requested_by else None
    if mode == "attach":
        try:
            tenant_id = str(uuid.UUID(str(payload.get("tenant_id"))))
        except ValueError:
            raise ProvisionError("invalid_tenant", "tenant_id must be a UUID") from None
        if not add_ons:
            raise ProvisionError("invalid_add_ons", "attach needs at least one add-on")
        return ProvisionRequest(request_key=key, mode=mode, tenant_id=tenant_id, add_ons=add_ons,
                                requested_by=requested_by)
    if mode != "create":
        raise ProvisionError("invalid_request", "mode must be create or attach")
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
    kind = payload.get("kind", "agency")
    if kind not in KINDS:
        raise ProvisionError("invalid_kind", f"kind must be one of {', '.join(KINDS)}")
    trial = payload.get("trial_days")
    if trial is not None and (isinstance(trial, bool) or not isinstance(trial, int)
                              or not 1 <= trial <= TRIAL_DAYS_MAX):
        raise ProvisionError("invalid_trial_days", f"trial_days: 1-{TRIAL_DAYS_MAX}")
    return ProvisionRequest(request_key=key, mode=mode, name=name.strip(), slug=slug, plan_tier=tier,
                            posts_per_week=ppw, kind=kind, trial_days=trial, add_ons=add_ons,
                            requested_by=requested_by)


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
    INSERT INTO shared.tenants (name, slug, plan_tier, plan_id, posts_per_week, api_key_hash, rate_limit_rpm,
                                is_active, kind, is_canary, account_status)
    VALUES ($1, $2, $3::plan_tier_enum, $4, $5, NULL, $6, true, $7, $8, $9)
    RETURNING tenant_id
"""
QUOTA_SQL = """
    INSERT INTO acp_shared.acp_quota_ledger (tenant_id, s2_runs_limit, s3_runs_limit, s4_blogs_limit)
    VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id) DO NOTHING
"""
BRAND_SQL = """
    INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name)
    SELECT $1, 'default' WHERE NOT EXISTS (SELECT 1 FROM shared.tenant_brand_rules WHERE tenant_id = $1)
"""
TRIAL_SQL = """
    UPDATE shared.tenant_subscription
       SET status = 'trialing', trial_ends_at = now() + make_interval(days => $2), updated_by = $3
     WHERE tenant_id = $1 AND product = 'content'
"""
TENANT_SQL = "SELECT tenant_id, account_status FROM shared.tenants WHERE tenant_id = $1::uuid FOR UPDATE"
ADDON_PLAN_SQL = "SELECT 1 FROM shared.plan WHERE plan_key = $1 AND product = $2 AND retired_at IS NULL"
# A live add-on stays as it is unless the plan or the seats change; a canceled or paused one is active again.
UPSERT_ADDON_SQL = """
    INSERT INTO shared.tenant_subscription AS s (tenant_id, product, plan_key, status, seats, updated_by)
    VALUES ($1, $2, $3, 'active', $4, $5)
    ON CONFLICT (tenant_id, product) DO UPDATE
       SET plan_key = EXCLUDED.plan_key, seats = EXCLUDED.seats, updated_by = EXCLUDED.updated_by,
           status = CASE WHEN s.status IN ('trialing', 'active', 'past_due') THEN s.status ELSE 'active' END,
           trial_ends_at = CASE WHEN s.status = 'trialing' THEN s.trial_ends_at END,
           canceled_at = NULL
     WHERE s.plan_key IS DISTINCT FROM EXCLUDED.plan_key OR s.seats IS DISTINCT FROM EXCLUDED.seats
        OR s.status NOT IN ('trialing', 'active', 'past_due')
    RETURNING s.product
"""
SUBS_SQL = """
    SELECT product, plan_key, status, seats FROM shared.tenant_subscription WHERE tenant_id = $1 ORDER BY product
"""
EVENTS_SQL = "SELECT count(*) AS n FROM shared.tenant_account_event WHERE tenant_id = $1 AND actor = $2"


def actor_for(job_id: str) -> str:
    return f"job:{KIND}:{job_id}"[:64]


async def _create(conn, req: ProvisionRequest, actor: str, steps: list[str]) -> tuple[uuid.UUID, list[str]]:
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
    status = "trial" if req.trial_days else "active"
    tenant_id = await conn.fetchval(INSERT_TENANT_SQL, req.name, req.slug, req.plan_tier, plan["membership_plan_id"],
                                    req.posts_per_week, int(plan["rpm"]), req.kind, req.kind == "demo", status)
    await conn.execute(QUOTA_SQL, tenant_id, *QUOTA_DEFAULTS)
    await conn.execute(BRAND_SQL, tenant_id)
    steps.append("tenant_created")
    # the 355 bridge trigger created the content subscription (active) on the tier's live plan
    if req.trial_days:
        await conn.execute(TRIAL_SQL, tenant_id, req.trial_days, actor)
        steps.append("content_trial")
    else:
        steps.append("content_active")
    return tenant_id, warnings


async def _attach_target(conn, req: ProvisionRequest) -> uuid.UUID:
    row = await conn.fetchrow(TENANT_SQL, req.tenant_id)
    if row is None:
        raise ProvisionError("tenant_not_found", "no tenant with this id")
    if row["account_status"] in ("offboarding", "closed"):
        raise ProvisionError("tenant_offboarded", "restore the tenant before adding an add-on")
    return row["tenant_id"]


async def _add_ons_step(conn, tenant_id: uuid.UUID, req: ProvisionRequest, actor: str, steps: list[str]) -> None:
    for a in req.add_ons:
        if await conn.fetchval(ADDON_PLAN_SQL, a.plan_key, a.product) is None:
            raise ProvisionError("unknown_plan", f"no live plan {a.plan_key}")
        changed = await conn.fetchval(UPSERT_ADDON_SQL, tenant_id, a.product, a.plan_key, a.seats, actor)
        steps.append(f"{a.product}_{'subscribed' if changed else 'unchanged'}")


def _result(req: ProvisionRequest, tenant_id: str, *, steps: list[str], subscriptions: list[dict], events: int,
            warnings: list[str], resumed: bool) -> dict[str, Any]:
    return {
        "kind": KIND, "mode": req.mode, "request_key": req.request_key, "tenant_id": tenant_id,
        "resumed": resumed, "steps": steps, "subscriptions": subscriptions, "events_written": events,
        "warnings": warnings,
        # a key is shown once and never stored in a job: staff issue it on the tenant (create mode only)
        "api_key": "not_issued" if req.mode == "create" else None,
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
        if req.mode == "create":
            tenant_id, warnings = await _create(conn, req, actor, steps)
        else:
            tenant_id = await _attach_target(conn, req)
        await _add_ons_step(conn, tenant_id, req, actor, steps)
        subs = [dict(r) for r in await conn.fetch(SUBS_SQL, tenant_id)]
        events = int(await conn.fetchval(EVENTS_SQL, tenant_id, actor) or 0)
        details = {
            "provision_request_key": req.request_key, "job_id": job_id, "mode": req.mode, "steps": steps,
            "subscriptions": subs, "events_written": events, "warnings": warnings,
            "requested_by": req.requested_by,
        }
        if req.mode == "create":
            details.update(name=req.name, plan_tier=req.plan_tier, posts_per_week=req.posts_per_week, kind=req.kind,
                           trial_days=req.trial_days)
        # last in the transaction: the audit chain trigger holds a transaction-scoped lock (QRY-4)
        await write_audit_log(conn, tenant_id=str(tenant_id), actor=actor,
                              action=ONBOARD if req.mode == "create" else SUBSCRIPTION_CHANGED,
                              resource_type="tenant", resource_id=str(tenant_id),
                              details=json.loads(json.dumps(details, default=str)))
    return _result(req, str(tenant_id), steps=steps, subscriptions=subs, events=events, warnings=warnings,
                   resumed=False)
