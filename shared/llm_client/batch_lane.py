"""AA-1076 — the gateway Batch lane: half-price bulk calls on the direct vendor APIs (Anthropic Message Batches first,
OpenAI Batch second, behind one interface).

Who uses it: bulk work nobody waits on (S1 attempt 1 in a wave, A3 re-atomize waves, ad-hoc backfills). Tenant-facing
stages (``t<n>_*``) never batch: they stay synchronous. The durable driver is the ``llm_batch`` job kind
(``services/jobs/llm_batch_job.py``): prepare -> submit -> poll (the job reschedules itself, so the batch survives every
worker restart) -> ingest -> hand the replies to the consumer (``services/llm_batch/consumers.py``).

What this module owns (provider-neutral):

- **Target** (``resolve_target``): the stage's route, model first (``[model_id, *fallback_model_ids]``), and per
  model its deployment plan (``vendor_route.deployment_plan``: data scopes, served roles, the pii rule, circuits and
  connection budgets, exactly as an on-demand call); the first deployment on an ``anthropic`` or ``openai``
  connection is the batch target. A route with no direct-API deployment cannot batch (BatchUnavailable: run it on
  demand).
- **Guards before submit** (``guard``): the lane switch (``LLM_BATCH_LANE``, default on), the tenant-stage rule, the
  stage kill switch, the app profile's AI switch, then the cost cap of the batch (``estimate_usd``: the gateway's own
  worst-case estimate per call, ``client.estimate_call_usd`` = prompt chars / 4 in + the whole max_tokens out, at the
  batch rate) checked against the caller's cap AND the central LLM budgets (``central_budget.check``). Nothing is sent
  when any of them refuses.
- **Requests** (``build_request``): the same LLMRequest shaping the gateway does before a provider call (stage x model
  parameters, the reasoning-model token floor, the model's output ceiling), then the provider body the on-demand
  adapter builds (``anthropic_messages.build_params`` / ``client.openai_chat_body``). Prompt caching inside a batch
  follows the deployment's ``request_defaults.cache_system_prompt`` (as on demand), or the caller's ``cache_system``.
- **Pricing and ledger** (``price`` / ``ledger_rows``): one ``shared.llm_call_log`` row per request of the batch,
  priced with ``pricing.quote(..., Usage(batch=True))`` (50% of every token: ``pricing_mode`` '<basis>+batch'), with
  ``**billed_fields(resp)`` of the reply built for it; a request with nothing billed (errored / expired / canceled /
  missing) is a 0 USD row (``gateway_failure_row``). ``quality_signal`` carries ``batch_id`` (ours), the provider
  batch id, ``custom_id``, the consumer and the outcome; the admin view sums spend per batch from it.
- **Hand-off** (``bind_replies`` / ``prefetched``): a consumer job binds the usable replies of its items (keyed by
  ``prompt_sha`` of stage + system + user) and the caller's normal code asks ``prefetched`` before its live call. A
  reply is used only when the prompt the caller builds NOW is byte-identical to the one the batch sent; anything else
  (a stale prompt, a failed / expired / cancelled item, a retry with feedback) runs on demand: retries and fallbacks
  stay outside the batch. The ledger row of a prefetched reply is the batch row: the caller writes none.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Iterator, Optional

import structlog

from . import app_profile, call_log, central_budget, connections, data_scope, pricing
from .catalog import LEGACY_KEYS, get_model_sync
from .models import ANTHROPIC_API_LABEL_PREFIX, LLMRequest, LLMResponse
from .pricing import Usage
from .role_config import get_stage_config_sync
from .vendor_route import deployment_plan, split_pin

logger = structlog.get_logger()

SWITCH_ENV = "LLM_BATCH_LANE"           # off / false / 0 / no closes submit (poll + ingest of a sent batch stay open)
PROVIDERS = ("anthropic", "openai")      # connection kinds the lane has a batch adapter for, in that order
TENANT_STAGE_RE = re.compile(r"^t\d+_")  # tenant-facing T-stages stay synchronous
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_CAP_USD = 100.0                      # a single batch never carries a cap above this (ask for two batches)
UNIT_KIND = "batch_request"


class BatchUnavailable(RuntimeError):
    """The batch cannot be sent (switch, stage rule, no direct deployment, provider refusal). Nothing was billed."""


class BatchCapExceeded(BatchUnavailable):
    """The estimate of the batch is above its cap (or a central LLM budget would be passed)."""


def closed_reason() -> Optional[str]:
    if (os.environ.get(SWITCH_ENV) or "on").strip().lower() in ("off", "false", "0", "no"):
        return f"the gateway batch lane is switched off ({SWITCH_ENV}=off on the task definition)"
    return None


# ── items, target, results ───────────────────────────────────────────────────────────────────────────────────────

@dataclass
class BatchItem:
    custom_id: str
    system: str
    user: str
    max_tokens: int
    json_schema: Optional[dict] = None
    tour_id: Optional[str] = None
    meta: dict = field(default_factory=dict)


@dataclass
class BatchTarget:
    stage: str
    role: str
    model_key: str
    connection_key: str
    provider: str          # anthropic | openai (the connection kind)
    wire_id: str
    # the stage the ledger rows carry when it is not the route's stage (an ad-hoc backfill: adhoc_<issue>)
    ledger_stage: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BatchTarget":
        return cls(**{k: d[k] for k in ("stage", "role", "model_key", "connection_key", "provider", "wire_id")},
                   ledger_stage=d.get("ledger_stage"))

    @property
    def log_stage(self) -> str:
        return self.ledger_stage or self.stage

    @property
    def label(self) -> str:
        """The ledger model label (call_log strips it to the Model Key and derives the provider)."""
        return f"{ANTHROPIC_API_LABEL_PREFIX}{self.model_key}" if self.provider == "anthropic" else self.model_key


@dataclass
class ItemResult:
    custom_id: str
    outcome: str                        # succeeded | errored | expired | canceled | missing
    text: str = ""
    stop_reason: Optional[str] = None
    provider_model: Optional[str] = None
    request_id: Optional[str] = None
    input_tokens: int = 0               # uncached
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    inference_geo: Optional[str] = None
    reasoning_tokens: Optional[int] = None
    error_type: Optional[str] = None
    error: Optional[str] = None

    @property
    def billed(self) -> bool:
        return bool(self.input_tokens or self.output_tokens or self.cache_read_tokens or self.cache_write_tokens)

    @property
    def usable(self) -> bool:
        """A reply the consumer may use: succeeded, text, not refused, not cut at its ceiling."""
        return (self.outcome == "succeeded" and bool(self.text) and self.stop_reason not in ("refusal", "max_tokens",
                                                                                               "length"))


def prompt_sha(stage: str, system: str, user: str) -> str:
    """The identity of one prompt (stage + system + user): a prefetched reply is used only for the same bytes."""
    h = hashlib.sha256()
    for part in (stage or "", system or "", user or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def resolve_target(stage: str) -> BatchTarget:
    """The direct-API deployment a batch of ``stage`` runs on (module docstring). Raises BatchUnavailable."""
    cfg = get_stage_config_sync(stage)
    if cfg is None or getattr(cfg, "stage", None) in (None, "unknown"):
        raise BatchUnavailable(f"stage {stage!r} has no route (shared.llm_role_config)")
    scopes = data_scope.normalize(getattr(cfg, "allowed_scopes", None))
    pii = data_scope.normalize_pii_class(getattr(cfg, "pii_class", None))
    role = getattr(cfg, "role", None) or "writer"
    chain = [cfg.model_id, *tuple(getattr(cfg, "fallback_model_ids", ()) or ())]
    tried = []
    for key in chain:
        if not key or key in LEGACY_KEYS or split_pin(key):
            tried.append(f"{key}: not a catalog key")
            continue
        model = get_model_sync(key)
        if model is None or not model.enabled:
            tried.append(f"{key}: not in the catalog or disabled")
            continue
        plan = deployment_plan(model, scopes, role=role, pii_class=pii)
        for target in plan or []:
            if target.connection.kind in PROVIDERS:
                return BatchTarget(stage=stage, role=role, model_key=model.model_key,
                                   connection_key=target.connection.connection_key, provider=target.connection.kind,
                                   wire_id=target.deployment.wire_id)
        tried.append(f"{key}: no usable Anthropic / OpenAI API deployment")
    raise BatchUnavailable(f"stage {stage!r} cannot batch: " + "; ".join(tried) + " (run it on demand)")


def stage_refusal(stage: str) -> Optional[str]:
    """Why ``stage`` may not batch now (the tenant rule, the stage kill switch, the app's AI switch), else None."""
    if TENANT_STAGE_RE.match(stage or ""):
        return f"stage {stage!r} is tenant-facing: tenant stages stay synchronous"
    cfg = get_stage_config_sync(stage)
    if cfg is not None and getattr(cfg, "enabled", True) is False:
        return f"stage {stage!r} is switched off ({getattr(cfg, 'disabled_reason', None) or 'no reason given'})"
    off = app_profile.refusal(stage)
    if off is not None:
        return str(off)
    return None


def build_request(target: BatchTarget, item: BatchItem):
    """(LLMRequest as the gateway would send it on this model, the catalog model, the deployment)."""
    from . import stage_params, token_floor
    model = get_model_sync(target.model_key)
    if model is None:
        raise BatchUnavailable(f"model {target.model_key!r} left the catalog")
    request = LLMRequest(system_prompt=item.system, user_prompt=item.user, stage=target.stage,
                         max_tokens=item.max_tokens, json_schema=item.json_schema)
    request = stage_params.apply(request, model)
    request = token_floor.apply_floor(request, model)
    if model.max_output_tokens and request.max_tokens > model.max_output_tokens:
        request = request.model_copy(update={"max_tokens": model.max_output_tokens})
    return request, model, pricing.deployment_for(target.model_key, target.connection_key)


def estimate_usd(target: BatchTarget, items: list[BatchItem]) -> float:
    """The worst-case cost of the batch: the gateway's per-call estimate at the batch rate, summed."""
    from .client import estimate_call_usd
    total = 0.0
    for item in items:
        request, _model, _dep = build_request(target, item)
        total += estimate_call_usd(request, target.model_key)
    return round(total * pricing.BATCH_DISCOUNT, 6)


def guard(target: BatchTarget, items: list[BatchItem], cap_usd: float) -> float:
    """Every check before submit (module docstring); returns the estimate. Raises BatchUnavailable or
    BatchCapExceeded."""
    closed = closed_reason()
    if closed:
        raise BatchUnavailable(closed)
    refused = stage_refusal(target.stage)
    if refused:
        raise BatchUnavailable(refused)
    if not items:
        raise BatchUnavailable("the batch has no request")
    bad = [i.custom_id for i in items if not CUSTOM_ID_RE.match(i.custom_id or "")]
    if bad:
        raise BatchUnavailable(f"custom ids must match {CUSTOM_ID_RE.pattern}: {bad[:3]}")
    if len({i.custom_id for i in items}) != len(items):
        raise BatchUnavailable("custom ids must be unique within a batch")
    try:
        cap = float(cap_usd)
    except (TypeError, ValueError):
        raise BatchCapExceeded("a batch needs a cost cap in USD") from None
    if not (0 < cap <= MAX_CAP_USD):
        raise BatchCapExceeded(f"the cost cap must be above 0 and at most {MAX_CAP_USD} USD (got {cap_usd})")
    estimate = estimate_usd(target, items)
    if estimate > cap:
        raise BatchCapExceeded(f"the batch estimate {estimate:.4f} USD (worst case, batch rate, {len(items)} "
                               f"requests on {target.model_key}) is above its cap {cap:.4f} USD: nothing was sent")
    if central_budget.active():
        try:
            central_budget.check(target.stage, estimate)
        except central_budget.CentralBudgetExceeded as e:
            raise BatchCapExceeded(f"a central LLM budget refuses the batch: {e}") from e
    return estimate


# ── provider calls ───────────────────────────────────────────────────────────────────────────────────────────────

def _connection(target: BatchTarget) -> connections.ProviderConnection:
    conn = connections.get_connection_sync(target.connection_key)
    if conn is None or not conn.enabled:
        raise BatchUnavailable(f"connection {target.connection_key!r} is missing or disabled")
    if conn.kind != target.provider:
        raise BatchUnavailable(f"connection {target.connection_key!r} is a {conn.kind!r} connection now")
    return conn


def _adapter(target: BatchTarget):
    if target.provider == "anthropic":
        from .adapters import anthropic_batches
        return anthropic_batches
    if target.provider == "openai":
        from .adapters import openai_batches
        return openai_batches
    raise BatchUnavailable(f"no batch adapter for {target.provider!r}")


def provider_body(target: BatchTarget, item: BatchItem, *, cache_system: Optional[bool] = None) -> dict:
    """The provider request body of one item: the on-demand adapter's own builder."""
    request, model, dep = build_request(target, item)
    if target.provider == "anthropic":
        from . import effort_rule, stage_params
        from .adapters import anthropic_messages
        defaults = effort_rule.anthropic_request_defaults(target.stage, model, getattr(dep, "request_defaults", None))
        if cache_system is not None:
            defaults = {**(defaults or {}), "cache_system_prompt": bool(cache_system)}
        extra = {"capabilities": model.capabilities} if getattr(model, "capabilities", None) else {}
        mode = stage_params.reasoning_mode_for(target.stage, model.model_key)
        if mode is not None:
            extra["reasoning_mode"] = mode
        return anthropic_messages.build_params(
            wire_id=target.wire_id, system=request.system_prompt, user=request.user_prompt,
            max_tokens=request.max_tokens, stage=target.stage, json_schema=request.json_schema,
            request_defaults=defaults, **extra)
    from .client import openai_chat_body
    body, _effort = openai_chat_body(request, target.wire_id, model)
    return body


def submit(target: BatchTarget, items: list[BatchItem], *, batch_id: str, cache_system: Optional[bool] = None) -> str:
    """Send the batch; returns the provider batch id. Call ``guard`` first. Raises BatchUnavailable."""
    conn = _connection(target)
    adapter = _adapter(target)
    try:
        if target.provider == "anthropic":
            reqs = [adapter.BatchRequest(i.custom_id, provider_body(target, i, cache_system=cache_system))
                    for i in items]
            return adapter.submit(conn, reqs)
        lines = [(i.custom_id, provider_body(target, i)) for i in items]
        return adapter.submit(conn, lines, metadata={"aa_batch_id": batch_id, "stage": target.stage})
    except BatchUnavailable:
        raise
    except Exception as e:
        raise BatchUnavailable(f"submit refused on {target.connection_key}: {type(e).__name__}: {str(e)[:300]}") from e


def status(target: BatchTarget, provider_batch_id: str):
    return _adapter(target).status(_connection(target), provider_batch_id)


def cancel(target: BatchTarget, provider_batch_id: str) -> None:
    _adapter(target).cancel(_connection(target), provider_batch_id)


def results(target: BatchTarget, provider_batch_id: str, custom_ids: list[str],
            batch_status: Optional[str] = None) -> dict[str, ItemResult]:
    """Every item's result (one per custom id, in any order from the provider); an item no result names is
    ``expired`` / ``canceled`` when the batch ended that way, else ``missing``."""
    adapter = _adapter(target)
    got = {o.custom_id: o for o in adapter.results(_connection(target), provider_batch_id)}
    out: dict[str, ItemResult] = {}
    missing = "missing"
    if target.provider == "openai":
        missing = {"expired": "expired", "cancelled": "canceled"}.get(batch_status or "", "missing")
    for cid in custom_ids:
        o = got.get(cid)
        if o is None:
            out[cid] = ItemResult(custom_id=cid, outcome=missing, error=f"no result for {cid}")
            continue
        out[cid] = ItemResult(
            custom_id=cid, outcome=o.outcome, text=o.text or "", stop_reason=o.stop_reason,
            provider_model=o.provider_model, request_id=o.request_id, input_tokens=o.input_tokens,
            output_tokens=o.output_tokens, cache_read_tokens=o.cache_read_tokens,
            cache_write_tokens=o.cache_write_tokens, cache_write_1h_tokens=o.cache_write_1h_tokens,
            inference_geo=o.inference_geo, reasoning_tokens=(o.extra or {}).get("reasoning_tokens"),
            error_type=o.error_type, error=o.error)
    return out


# ── pricing, replies, ledger ─────────────────────────────────────────────────────────────────────────────────────

def price(target: BatchTarget, r: ItemResult) -> pricing.Quote:
    """The batch-rate cost of one billed result, on its deployment (price overrides first)."""
    usage = Usage(input_tokens=r.input_tokens, output_tokens=r.output_tokens, cache_read_tokens=r.cache_read_tokens,
                  cache_write_tokens=r.cache_write_tokens, cache_write_1h_tokens=r.cache_write_1h_tokens,
                  batch=True, inference_geo=r.inference_geo)
    return pricing.quote(target.model_key, usage, pricing.deployment_for(target.model_key, target.connection_key))


def response_of(target: BatchTarget, r: ItemResult, *, prompt_hash: Optional[str] = None) -> LLMResponse:
    """The LLMResponse of one billed result, shaped like the on-demand adapter's (tokens_in = uncached + cache)."""
    q = price(target, r)
    return LLMResponse(
        content=r.text or "", model_used=target.label, provider=target.provider,
        input_tokens=r.input_tokens + r.cache_read_tokens + r.cache_write_tokens, output_tokens=r.output_tokens,
        cost_usd=q.cost_usd, cache_read_tokens=r.cache_read_tokens, cache_write_tokens=r.cache_write_tokens,
        cache_write_1h_tokens=r.cache_write_1h_tokens, pricing_mode=q.pricing_mode,
        connection_key=target.connection_key, stop_reason=r.stop_reason, provider_model=r.provider_model,
        request_id=r.request_id, deployment_wire_id=target.wire_id, reasoning_tokens=r.reasoning_tokens,
        prompt_hash=prompt_hash)


_ERROR_CLASS = {"expired": "deadline", "canceled": "other", "missing": "other"}


def _failure_class(r: ItemResult) -> str:
    if r.outcome in _ERROR_CLASS:
        return _ERROR_CLASS[r.outcome]
    et = (r.error_type or "").lower()
    if "rate_limit" in et or "overloaded" in et or et == "429":
        return "throttled"
    if "billing" in et or "quota" in et:
        return "quota_exhausted"
    if "authentication" in et or "permission" in et or et in ("401", "403"):
        return "access_denied"
    return "other"


def ledger_rows(target: BatchTarget, results_by_id: dict[str, ItemResult], items: list[dict], *, batch_id: str,
                provider_batch_id: str, consumer: str) -> list[dict]:
    """One ``record_calls`` row per item of the batch (``items``: [{custom_id, tour_id, prompt_sha}], the payload's
    list, in that order). A billed result: its batch-rate cost with ``**billed_fields(resp)``; status ok, truncated (cut
    at its ceiling) or billed_unusable (refused / empty). Nothing billed: a 0 USD error row."""
    rows: list[dict] = []
    for it in items:
        cid = it["custom_id"]
        r = results_by_id.get(cid) or ItemResult(custom_id=cid, outcome="missing", error="no result")
        signal = {"batch": True, "batch_id": str(batch_id), "provider_batch_id": str(provider_batch_id),
                  "custom_id": cid, "consumer": consumer, "batch_outcome": r.outcome}
        common = {"tour_id": it.get("tour_id"), "units": 1, "unit_kind": UNIT_KIND}
        if not r.billed:
            rows.append(call_log.gateway_failure_row(
                stage=target.log_stage, role=target.role, model=target.label, status="error",
                error_class=_failure_class(r), error=f"batch {r.outcome}: {r.error or ''}"[:300],
                provider=target.provider, connection_key=target.connection_key, deployment_wire_id=target.wire_id,
                quality_signal_extra=signal, **common))
            continue
        resp = response_of(target, r)
        status_ = call_log.STATUS_OK
        if r.outcome == "succeeded" and r.stop_reason in ("max_tokens", "length"):
            status_ = call_log.STATUS_TRUNCATED
        elif not r.usable:
            status_ = call_log.STATUS_BILLED_UNUSABLE
            signal.update({"billed_unusable": True, "error": str(r.error or r.stop_reason or "no text")[:300]})
        rows.append(dict(
            stage=target.log_stage, role=target.role, model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, quality_signal=signal,
            stop_reason=resp.stop_reason, fallback_used=False, provider=target.provider, status=status_,
            **common, **call_log.billed_fields(resp)))
    return rows


SPEND_SQL = """
SELECT count(*)::int AS rows, coalesce(sum(cost_usd), 0)::float AS usd,
       count(*) FILTER (WHERE coalesce(cost_usd, 0) > 0)::int AS billed
  FROM shared.llm_call_log
 WHERE stage = $1 AND created_at >= $2 AND quality_signal->>'batch_id' = $3
"""


async def ledgered(db, stage: str, since, batch_id: str) -> dict:
    """The ledger rows already written for ``batch_id`` (so an ingest re-run never writes them twice; the admin view's
    spend). ``since``: the batch job's created_at (bounds the scan to the stage index)."""
    row = await db.fetchrow(SPEND_SQL, stage, since, str(batch_id))
    return {"rows": int(row["rows"] or 0), "usd": float(row["usd"] or 0.0), "billed": int(row["billed"] or 0)}


# ── hand-off to the caller's normal code ─────────────────────────────────────────────────────────────────────────

_REPLIES: ContextVar[Optional[dict]] = ContextVar("llm_batch_replies", default=None)


def reply_record(target: BatchTarget, r: ItemResult, sha: str, *, batch_id: str) -> dict:
    """What a consumer job carries for one usable reply (JSON, in its payload)."""
    resp = response_of(target, r)
    return {"prompt_sha": sha, "stage": target.stage, "batch_id": str(batch_id), "custom_id": r.custom_id,
            "response": resp.model_dump(exclude_none=True)}


@contextmanager
def bind_replies(records: Optional[list[dict]]) -> Iterator[None]:
    """Make ``records`` (``reply_record`` dicts) answerable by ``prefetched`` for the code run inside (threads started
    with a copied context and asyncio tasks included)."""
    table = {r["prompt_sha"]: r for r in (records or []) if isinstance(r, dict) and r.get("prompt_sha")}
    token = _REPLIES.set(table or None)
    try:
        yield
    finally:
        _REPLIES.reset(token)


def prefetched(stage: str, system: str, user: str) -> Optional[LLMResponse]:
    """The batch reply for exactly this prompt, else None (the caller then calls live). Never raises."""
    table = _REPLIES.get()
    if not table:
        return None
    try:
        rec = table.get(prompt_sha(stage, system, user))
        if rec is None or rec.get("stage") != stage:
            logger.info("llm_batch_reply_not_used", stage=stage, reason="no reply for this prompt (stale or not sent)")
            return None
        # provider_requests 0: the reply stands for no NEW billed request (its batch row is in the ledger already)
        resp = LLMResponse(**{**rec["response"], "provider_requests": 0})
        logger.info("llm_batch_reply_used", stage=stage, batch_id=rec.get("batch_id"), custom_id=rec.get("custom_id"))
        return resp
    except Exception as e:  # noqa: BLE001 — a bad record means a live call, never a failure
        logger.warning("llm_batch_reply_unreadable", stage=stage, error=str(e)[:200])
        return None


def already_ledgered(resp) -> bool:
    """True for a reply ``prefetched`` returned: its llm_call_log row is the batch's; the caller writes none."""
    n = getattr(resp, "provider_requests", 1)
    return isinstance(n, int) and not isinstance(n, bool) and n == 0
