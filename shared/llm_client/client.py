import os
import json
import random
import threading
import time
import boto3
from botocore.config import Config
from typing import Optional

import openai
import structlog
from .models import LLMRequest, LLMResponse
from .prompt_cache import build_cached_system_prompt, build_cached_messages
from .pricing import BEDROCK_SONNET, BEDROCK_HAIKU, COST_TABLE, calc_cost
from .role_config import get_stage_config_sync
from .catalog import LEGACY_KEYS, CatalogModel, get_model_sync
from .schema_check import SchemaMismatch, conform
from . import stream_sink

logger = structlog.get_logger()

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-west-1")

# Default model tier — override via ECS env var DEFAULT_MODEL_TIER. Only reached when a call
# passes neither an explicit request.model_tier NOR a request.stage (AA-518) — every real call
# site in this codebase now passes at least one of those two, so this is belt-and-suspenders for
# an unforeseen caller, not the live default path anymore.
# Options: "haiku" (cheapest) | "sonnet" (premium). "gpt-4.1" (OpenAI platform API) is retired (S235,
# migration 224 disables its catalog row) and refused by the gate below.
DEFAULT_MODEL_TIER = os.environ.get("DEFAULT_MODEL_TIER", "haiku")

# BEDROCK_SONNET/BEDROCK_HAIKU/COST_TABLE relocated to pricing.py (AA-518/AA-505) so Mechanism-B
# call sites (invoke_claude() direct, e.g. T5 atomize, N7 E2-E5) can price a call without
# importing this whole client — re-exported here unchanged so nothing importing them FROM this
# module breaks.

LEGACY_ACC2_NATIVE_ENV = "LLM_LEGACY_ACC2_NATIVE"


def legacy_acc2_native_enabled() -> bool:
    """AA-843 — whether the legacy haiku/sonnet chain still tries acc2-native Bedrock first.

    acc2 is a channel-program account: Anthropic models there fail every time with
    "ValidationException … not available for channel program accounts" (~2,400 wasted attempts a
    week, ~0.1-0.2 s each). Off by default, so the chain starts at the satellite. Set the ECS env var
    LLM_LEGACY_ACC2_NATIVE=on (1/true/yes) to restore the old acc2-first order if acc2 ever gains
    model access. Read per call, not at import."""
    return os.environ.get(LEGACY_ACC2_NATIVE_ENV, "").strip().lower() in ("1", "true", "on", "yes")


class OpenAIPlatformBlocked(RuntimeError):
    """S235 — a call would go to an OpenAI platform API model whose catalog row is not enabled."""


def openai_platform_allowed(model_key: str) -> bool:
    """S235 (Nghiệp, 14/10/2026): Bedrock first; the OpenAI platform API is only the last-resort
    fallback of a stage route (gpt-6-luna-openai at the end of the judge routes). The adapter
    (`LLMClient._call_openai`) runs only for a catalog row with provider 'openai' that is enabled.
    Migration 224 retires gpt-4.1 (disabled), so the legacy gpt-4.1 tier and the legacy chain's
    GPT-4.1 last resort are refused here. Fails closed: an unreadable catalog (DB down, unit tests)
    means not allowed."""
    m = get_model_sync(model_key)
    return m is not None and m.provider == "openai" and m.enabled


def _checked_json(model_label: str, value, schema: dict) -> str:
    """AA-659 — a structured output that does not match its schema raises, so the stage route
    moves to its next model instead of a caller's error path accepting it silently."""
    try:
        return json.dumps(conform(value, schema["schema"]))
    except SchemaMismatch as e:
        raise RuntimeError(f"{model_label} output does not match schema {schema['name']!r}: {e}") from e


class BilledUnusableReply(RuntimeError):
    """AA-927: the provider billed the request(s) but the reply is unusable (schema mismatch after the
    Converse retry, empty content, invalid JSON). `billed_usage` = one dict per billed model attempt
    (model, account, tokens, cost — internal retries folded, `provider_requests` > 1), so the gateway can
    write the llm_call_log row nobody else would: the caller never sees this attempt. Built with
    `_billed_unusable()`."""

    billed_usage: tuple = ()


def _billed_unusable(message: str, billed_usage: list[dict]) -> BilledUnusableReply:
    exc = BilledUnusableReply(message)
    exc.billed_usage = billed_usage
    return exc


def _billed_usage(model_used: str, account: Optional[str], usage_totals: dict, cost: float,
                  stop_reason: Optional[str], provider_requests: int) -> dict:
    return {"model": model_used, "account": account, "tokens_in": usage_totals.get("tokens_in"),
            "tokens_out": usage_totals.get("tokens_out"), "cost_usd": cost, "stop_reason": stop_reason,
            "provider_requests": provider_requests}


def _add_converse_usage(total: dict, usage: dict) -> None:
    for wire, key in (("inputTokens", "in"), ("outputTokens", "out"), ("cacheReadInputTokens", "cache_read"),
                      ("cacheWriteInputTokens", "cache_write")):
        total[key] = total.get(key, 0) + (usage.get(wire, 0) or 0)


def _first_tool_input(blocks: list, schema: dict, model, allow_text_json: bool = False) -> dict:
    """AA-714 — the forced-tool input from a Converse response, or raise if the model skipped it.

    AA-803 (S246): with `allow_text_json` (tool choice `auto`, see `_forced_tool_choice_ok`) a model
    that answered with the JSON object as plain text instead of calling the tool is accepted too —
    the schema check (`_checked_json`) still runs on whatever comes back."""
    tool_inputs = [b["toolUse"]["input"] for b in blocks if "toolUse" in b]
    if tool_inputs:
        return tool_inputs[0]
    if allow_text_json:
        parsed = _json_object_in_text("".join(b.get("text", "") for b in blocks))
        if parsed is not None:
            return parsed
    raise RuntimeError(f"{model.model_key} returned no tool call for schema {schema['name']!r}")


def _json_object_in_text(text: str) -> Optional[dict]:
    """The JSON object in a plain-text answer (optionally inside a ``` fence), or None."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    if not t.startswith("{"):
        return None
    try:
        value = json.loads(t)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# AA-803 (S246): Claude models whose Messages API answers a forced tool choice (`any` / `tool`) with a
# 400 — Opus 5.5, Sonnet 5.5, Fable 5.1 / Mythos 5.1. Haiku 5.5 is NOT one of them: it accepts a forced
# tool (with thinking on, the response then starts with the tool call and carries no thinking block), so
# it keeps the forced tool — fewer output tokens and a reliable schema on its high-volume stages, the same
# request shape Haiku 4.5 gets. Matched against the Bedrock
# inference-profile id (e.g. "global.anthropic.claude-sonnet-5-5"), so every account's profile of the
# same model matches. A model outside this list that rejects forced tool use is caught at runtime
# (`_tool_choice_rejected`) and remembered for the life of the process.
_NO_FORCED_TOOL_CHOICE_MARKERS = (
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1",
)
_FORCED_TOOL_REJECTED: set[str] = set()


def _forced_tool_choice_ok(model, profile_id: str) -> bool:
    if model.model_key in _FORCED_TOOL_REJECTED:
        return False
    pid = (profile_id or "").lower()
    return not any(marker in pid for marker in _NO_FORCED_TOOL_CHOICE_MARKERS)


def _tool_choice_rejected(exc: BaseException) -> bool:
    """AA-803: Bedrock's 400 for a forced tool choice the model does not take, e.g.
    "ValidationException ... tool_choice: type "tool" and "any" are not supported for this model"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "validationexception" in text and ("tool_choice" in text or "toolchoice" in text) and any(
        w in text for w in ("not supported", "unsupported", "not allowed", "thinking", "deprecated"))


def _auto_tool_instruction(schema: dict) -> str:
    return (f"Return your answer by calling the tool {schema['name']!r} exactly once, with the complete "
            "object as its input. Do not answer in plain text.")


def _use_auto_tool_choice(kwargs: dict, schema: dict) -> None:
    """Switch a Converse request from a forced tool to `auto` + an explicit instruction. The original
    system block stays first and unchanged (prompt-cache prefix); the instruction is a second block."""
    kwargs["toolConfig"] = {**kwargs["toolConfig"], "toolChoice": {"auto": {}}}
    kwargs["system"] = [*kwargs["system"], {"text": _auto_tool_instruction(schema)}]


def _converse_with_param_downgrades(call, kwargs: dict, model_key: str, schema: Optional[dict]):
    """`_converse_dropping_rejected_temperature`, plus AA-803: when Bedrock rejects the forced tool
    choice of a structured call, switch `kwargs` to `auto` (`_use_auto_tool_choice`), remember the model
    and retry ONCE. A ValidationException is not billed."""
    try:
        return _converse_dropping_rejected_temperature(call, kwargs, model_key)
    except Exception as e:  # noqa: BLE001 — botocore ClientError; anything else is re-raised below
        tool_choice = (kwargs.get("toolConfig") or {}).get("toolChoice") or {}
        if not schema or "auto" in tool_choice or not _tool_choice_rejected(e):
            raise
        logger.warning("converse_tool_choice_rejected", model=model_key, error=str(e)[:200],
                       action="retry_with_auto_tool_choice; add the model to _NO_FORCED_TOOL_CHOICE_MARKERS")
        _FORCED_TOOL_REJECTED.add(model_key)
        _use_auto_tool_choice(kwargs, schema)
        return _converse_dropping_rejected_temperature(call, kwargs, model_key)


def _temperature_rejected(exc: BaseException) -> bool:
    """AA-801: Bedrock's answer to a temperature the model no longer takes, e.g. Claude Sonnet 5:
    "ValidationException ... temperature is deprecated for this model"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "validationexception" in text and "temperature" in text and any(
        w in text for w in ("deprecated", "not supported", "unsupported", "not allowed"))


def _converse_dropping_rejected_temperature(call, kwargs: dict, model_key: str):
    """AA-801: one Converse call; when Bedrock rejects the temperature we sent (the catalog row still
    says `supports_temperature`), retry ONCE without it and drop it from `kwargs` for any follow-up
    call. A ValidationException is not billed, so the retry costs nothing extra. The real fix is the
    catalog flag (migration 227 for sonnet-5); this keeps a stale flag from failing every call."""
    try:
        return call(**kwargs)
    except Exception as e:  # noqa: BLE001 — botocore ClientError; anything else is re-raised below
        inference = kwargs.get("inferenceConfig") or {}
        if "temperature" not in inference or not _temperature_rejected(e):
            raise
        logger.warning("converse_temperature_rejected", model=model_key, error=str(e)[:200],
                       action="retry_without_temperature; set supports_temperature=false in the catalog")
        kwargs["inferenceConfig"] = {k: v for k, v in inference.items() if k != "temperature"}
        return call(**kwargs)


def _billed_usage_of(exc: BaseException) -> list:
    """The billed attempts carried by `exc` or anything in its cause / context chain (the legacy chain
    wraps a T3 OpenAI failure in its own RuntimeError)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        billed = getattr(exc, "billed_usage", None)
        if billed:
            return list(billed)
        exc = exc.__cause__ or exc.__context__
    return []


def log_gateway_billed_failure(request: LLMRequest, *, role: str, billed: list[dict], error: str,
                               extra: Optional[dict] = None) -> None:
    """AA-927: the gateway's own llm_call_log rows for billed-but-unusable attempts (see
    `LLMClient._call_key_logged`): one row per billed attempt, `gateway_attempt: true`,
    `billed_unusable: true`, no ok-rate key. Written fire-and-forget on a background thread so a slow or
    down database never delays the route's next model. Module-level so the AA-729 harness can see it.
    Never raises."""
    from .call_log import _in_background, billed_unusable_rows, record_calls
    try:
        rows = []
        for b in billed:
            rows += billed_unusable_rows(
                stage=request.stage or "unstaged", role=role, billed=[b], error=error,
                quality_signal_extra={**(extra or {}), "gateway_attempt": True, "model": b.get("model"),
                                      "account": b.get("account"),
                                      **({"provider_requests": b["provider_requests"]}
                                         if (b.get("provider_requests") or 1) > 1 else {})})
        if rows:
            _in_background(record_calls, {"rows": rows})
    except Exception as e:  # noqa: BLE001 — a log write never breaks the call it records
        logger.warning("llm_gateway_billed_failure_log_failed", stage=request.stage, error=str(e)[:200])


class LLMClient:
    """
    Legacy haiku/sonnet chain (AA-397/398 — acc3 is the primary satellite, acc1 the fallback;
    a stage's account_route can swap them):
    T1:    Claude Sonnet 4.5 (Bedrock, acc2 native) + prompt caching — AA-843: only when
                                 LLM_LEGACY_ACC2_NATIVE is on (default off; acc2 always refuses)
    T1.5a: Claude Sonnet     (Bedrock satellite, primary account, acc3 by default)
    T1.5b: Claude Sonnet     (Bedrock satellite, the other account)
    T2:    Claude Haiku 4.5  (Bedrock, acc2 native) + prompt caching — AA-843: same flag as T1
    T2.5a: Claude Haiku      (Bedrock satellite, primary account)
    T2.5b: Claude Haiku      (Bedrock satellite, the other account)
    T3:    GPT-4.1           (OpenAI platform API) — S235: retired; runs only if the gpt-4.1
                                 catalog row is enabled again (openai_platform_allowed)
    """

    def __init__(self):
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=Config(
                read_timeout=300,
                connect_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        self._openai = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY")
        )

    def generate(self, request: LLMRequest) -> LLMResponse:
        # AA-637 — report call boundaries to a bound live-progress sink (no-op without one).
        stream_sink.emit_call_start(request.stage)
        ok = False
        try:
            resp = self._generate(request)
            ok = True
            return resp
        finally:
            stream_sink.emit_call_end(request.stage, ok)

    def _generate(self, request: LLMRequest) -> LLMResponse:
        # AA-518 — request.stage (when set) resolves the admin's per-stage config; an explicit
        # request.model_tier still wins over it (AA-237's opt-in haiku->sonnet auto-upgrade, and
        # any other real per-request override), same relationship model_tier already had with
        # the DEFAULT_MODEL_TIER env var before this task.
        stage_cfg = get_stage_config_sync(request.stage) if request.stage else None
        # Which satellite account is tried FIRST (AA-843: the first account the legacy chain tries
        # unless LLM_LEGACY_ACC2_NATIVE puts acc2-native ahead of it) — 'acc3' unless the
        # stage's admin config explicitly says 'acc1' (e.g. a real acc3 outage). Unknown/no-stage
        # calls keep the pre-AA-518 hardcoded acc3-first order exactly.
        primary_acct = stage_cfg.account_route if (stage_cfg and stage_cfg.account_route) else "acc3"
        fallback_acct = "acc1" if primary_acct == "acc3" else "acc3"

        # AA-659 / ADR 0006 — the stage route is model_id + fallback_model_ids. An explicit
        # request.model_tier still wins and runs alone (no route, no shadow).
        if request.model_tier:
            chain = [request.model_tier]
        elif stage_cfg:
            chain = [stage_cfg.model_id, *stage_cfg.fallback_model_ids]
        else:
            chain = [DEFAULT_MODEL_TIER]

        role = (stage_cfg.role if stage_cfg and stage_cfg.role else "writer")
        if len(chain) == 1:
            resp = self._call_key_logged(request, chain[0], primary_acct, fallback_acct, role=role, position=0)
        else:
            resp = self._call_route(request, chain, primary_acct, fallback_acct, role=role)

        if (stage_cfg and not request.model_tier and stage_cfg.shadow_model_id
                and random.random() * 100 < stage_cfg.shadow_sample_pct):
            self._start_shadow(request, stage_cfg, resp, primary_acct, fallback_acct)
        return resp

    def _call_key(self, request: LLMRequest, key: str, primary_acct: str, fallback_acct: str) -> LLMResponse:
        # AA-658 / ADR 0005 — non-legacy Model Keys resolve through the catalog, with no hidden
        # fallback; a stage that wants one lists it in its route (AA-659).
        if key not in LEGACY_KEYS:
            return self._call_catalog_model(request, key, primary_acct)
        return self._legacy_chain(request, key, primary_acct, fallback_acct)

    def _call_key_logged(self, request: LLMRequest, key: str, primary_acct: str, fallback_acct: str, *,
                         role: str, position: int, extra: Optional[dict] = None) -> LLMResponse:
        """AA-927: `_call_key`, and when the attempt raised AFTER the provider billed it
        (BilledUnusableReply), write that attempt's llm_call_log row here before re-raising — the caller
        only ever sees the reply that came back, so no one else can. One row per billed attempt,
        `billed_unusable: true`, no ok-rate key (call_log.OK_RATE_KEYS)."""
        try:
            return self._call_key(request, key, primary_acct, fallback_acct)
        except Exception as e:
            billed = _billed_usage_of(e)
            if billed:
                log_gateway_billed_failure(request, role=role, billed=billed, error=str(e),
                                           extra={"position": position, **(extra or {})})
            raise

    def _call_route(self, request: LLMRequest, chain: list, primary_acct: str, fallback_acct: str,
                    role: str = "writer") -> LLMResponse:
        errors = []
        for position, key in enumerate(chain):
            if key not in LEGACY_KEYS:
                m = get_model_sync(key)
                if m is None or not m.enabled:
                    reason = "not in catalog" if m is None else (m.blocked_reason or "disabled")
                    logger.info("llm_route_skip", stage=request.stage, model=key, reason=reason)
                    errors.append(f"{key}: skipped ({reason})")
                    continue
            try:
                resp = self._call_key_logged(request, key, primary_acct, fallback_acct, role=role,
                                             position=position)
            except Exception as e:
                logger.warning("llm_route_attempt_failed", stage=request.stage, model=key,
                               position=position, error=str(e)[:300])
                errors.append(f"{key}: {str(e)[:200]}")
                continue
            if position > 0:
                resp.fallback_used = True
            if resp.provider == "openai":
                # S235 — the OpenAI platform API is a last resort for a Bedrock outage: tenant
                # content left AWS. Fixed event name for a CloudWatch metric filter / alarm
                # (proposed in docs/implementation-notes/S235-bedrock-only.md); llm_call_log gets
                # provider='openai' + fallback_used=true from the caller's record_call.
                logger.warning("llm_openai_fallback_used", stage=request.stage, model=key,
                               position=position, errors=errors)
            logger.info("llm_route_used", stage=request.stage, model=key, position=position)
            return resp
        raise RuntimeError(f"All models in the route failed for stage {request.stage!r}: "
                           + "; ".join(errors))

    def _start_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                      primary_acct: str, fallback_acct: str) -> None:
        # A plain thread does not inherit contextvars, so the shadow never writes to the
        # live-progress stream sink bound to the primary call. The job id and tenant ARE carried
        # over explicitly (AA-741 G1) so the shadow's llm_call_log row is attributed like the
        # primary's.
        from .call_log import current_job_id, current_tenant_id
        threading.Thread(
            target=self._run_shadow,
            args=(request.model_copy(), cfg, primary, primary_acct, fallback_acct,
                  current_job_id(), current_tenant_id()),
            daemon=True,
        ).start()

    def _run_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                    primary_acct: str, fallback_acct: str,
                    job_id: Optional[str] = None, tenant_id: Optional[str] = None) -> None:
        from .call_log import bind_job, bind_tenant
        with bind_job(job_id), bind_tenant(tenant_id):
            self._run_shadow_bound(request, cfg, primary, primary_acct, fallback_acct)

    def _run_shadow_bound(self, request: LLMRequest, cfg, primary: LLMResponse,
                          primary_acct: str, fallback_acct: str) -> None:
        from .shadow import record_shadow_sync
        t0 = time.monotonic()
        resp, error = None, None
        try:
            resp = self._call_key_logged(request, cfg.shadow_model_id, primary_acct, fallback_acct,
                                         role=cfg.role or "writer", position=0, extra={"shadow": True})
        except Exception as e:
            error = str(e)[:1000]
        record_shadow_sync(
            stage=cfg.stage, role=cfg.role, request=request, primary=primary,
            shadow_model=cfg.shadow_model_id, shadow=resp, error=error,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    def _legacy_chain(self, request: LLMRequest, tier: str, primary_acct: str, fallback_acct: str) -> LLMResponse:
        # Direct GPT-4.1 — no Bedrock fallback (explicit choice)
        if tier == "gpt-4.1":
            try:
                return self._call_openai(request, model="gpt-4.1")
            except Exception as e:
                logger.error("gpt41_direct_failed", error=str(e))
                raise RuntimeError(f"GPT-4.1 failed: {e}") from e

        # AA-843 — acc2-native (T1/T2) only behind LLM_LEGACY_ACC2_NATIVE; by default the chain
        # starts at the primary satellite. Event names stay the same; only `reason` says why the
        # satellite was the first stop.
        acc2_native = legacy_acc2_native_enabled()

        if tier == "sonnet":
            # T1: Claude Sonnet — premium quality (acc2-native, off by default)
            if acc2_native:
                try:
                    resp = self._call_bedrock(request, model=BEDROCK_SONNET, use_cache=True)
                    return resp
                except Exception as e:
                    if "AccessDeniedException" in str(e) or "not authorized" in str(e).lower():
                        logger.warning("t1_sonnet_not_subscribed", model=BEDROCK_SONNET,
                                       hint="Enable cross-region inference profile in AWS Marketplace (AA-50)")
                    else:
                        logger.warning("t1_failed_trying_t2", model=BEDROCK_SONNET, error=str(e))

            # T1.5a: Claude Sonnet qua satellite PRIMARY account (acc3 unless stage config says acc1)
            try:
                resp = self._call_bedrock_satellite(request, model=BEDROCK_SONNET, account=primary_acct)
                resp.fallback_used = False    # KHÔNG phải fallback — vẫn đúng Sonnet, đúng ý định
                resp.satellite_account = primary_acct
                logger.info("t1_5a_satellite_used", model=BEDROCK_SONNET, account=primary_acct,
                            reason="acc2 T1 failed" if acc2_native else "acc2 native off (AA-843)")
                return resp
            except Exception as e:
                logger.warning("t1_5a_satellite_failed_trying_t1_5b", model=BEDROCK_SONNET,
                               account=primary_acct, error=str(e))

            # T1.5b: Claude Sonnet qua satellite FALLBACK account
            try:
                resp = self._call_bedrock_satellite(request, model=BEDROCK_SONNET, account=fallback_acct)
                resp.fallback_used = False
                resp.satellite_account = fallback_acct
                logger.info("t1_5b_satellite_used", model=BEDROCK_SONNET, account=fallback_acct,
                            reason="acc2 T1 + T1.5a failed" if acc2_native else "T1.5a failed")
                return resp
            except Exception as e:
                logger.warning("t1_5b_satellite_failed_trying_t2", model=BEDROCK_SONNET,
                               account=fallback_acct, error=str(e))

        # T2: Claude Haiku — fast / default tier, or Sonnet fallback (acc2-native, off by default)
        if acc2_native:
            try:
                resp = self._call_bedrock(request, model=BEDROCK_HAIKU, use_cache=True)
                resp.fallback_used = tier == "sonnet"  # only a fallback when Sonnet was intended
                return resp
            except Exception as e:
                logger.warning("t2_failed_trying_t2_5a", model=BEDROCK_HAIKU, error=str(e))

        # T2.5a: Claude Haiku qua satellite PRIMARY account
        try:
            resp = self._call_bedrock_satellite(request, model=BEDROCK_HAIKU, account=primary_acct)
            # giữ đúng logic gốc T2: chỉ coi là fallback nếu ý định ban đầu là sonnet
            resp.fallback_used = tier == "sonnet"
            resp.satellite_account = primary_acct
            logger.info("t2_5a_satellite_used", model=BEDROCK_HAIKU, account=primary_acct,
                        reason="acc2 T2 failed" if acc2_native else "acc2 native off (AA-843)")
            return resp
        except Exception as e:
            logger.warning("t2_5a_satellite_failed_trying_t2_5b", model=BEDROCK_HAIKU,
                           account=primary_acct, error=str(e))

        # T2.5b: Claude Haiku qua satellite FALLBACK account
        try:
            resp = self._call_bedrock_satellite(request, model=BEDROCK_HAIKU, account=fallback_acct)
            resp.fallback_used = tier == "sonnet"
            resp.satellite_account = fallback_acct
            logger.info("t2_5b_satellite_used", model=BEDROCK_HAIKU, account=fallback_acct,
                        reason="acc2 T2 + T2.5a failed" if acc2_native else "T2.5a failed")
            return resp
        except Exception as e:
            logger.warning("t2_5b_satellite_failed_trying_t3", model=BEDROCK_HAIKU, account=fallback_acct, error=str(e))

        # T3: GPT-4.1 — last resort for all tiers. S235: GPT-4.1 is retired (migration 224 disables
        # its catalog row), so a Bedrock outage on a Haiku/Sonnet writer raises here. Writers are
        # Anthropic; an OpenAI last resort for a writer would change vendor unseen (ADR 0005 §4) —
        # only the judge routes list an OpenAI-API last resort, explicitly.
        if not openai_platform_allowed("gpt-4.1"):
            logger.error("t3_skipped_gpt41_retired", tier=tier,
                         reason="GPT-4.1 retired (S235); no OpenAI last resort for legacy tiers")
            raise RuntimeError("All LLM providers failed (Bedrock tiers down; GPT-4.1 retired)")
        try:
            resp = self._call_openai(request, model="gpt-4.1")
            resp.fallback_used = True
            logger.warning("t3_fallback_used", model="gpt-4.1",
                           reason=f"T2 failed (tier={tier})")
            return resp
        except Exception as e:
            logger.error("t3_failed_all_providers_down", error=str(e))
            raise RuntimeError("All LLM providers failed") from e

    def _call_bedrock(
        self, request: LLMRequest, model: str, use_cache: bool = False
    ) -> LLMResponse:
        system = (
            build_cached_system_prompt(request.system_prompt)
            if use_cache
            else [{"type": "text", "text": request.system_prompt}]
        )
        messages = (
            build_cached_messages(
                request.few_shots if hasattr(request, "few_shots") else [],
                request.user_prompt,
            )
            if use_cache
            else [{"role": "user", "content": request.user_prompt}]
        )

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": messages,
        }
        if "temperature" in request.model_fields_set and request.temperature is not None:
            body["temperature"] = request.temperature   # S224: was silently dropped

        # AA-637 — a new provider attempt: drop any partial text a failed attempt already showed.
        stream_sink.emit_restart(request.stage)
        on_delta = stream_sink.delta_callback(request.stage)
        response = self._bedrock.invoke_model_with_response_stream(
            modelId=model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )

        # AA-224: stream accumulation. Non-stream invoke_model held the HTTP connection open for
        # the full generation (default 60s read_timeout) -> Sonnet branded prompts timed out ->
        # silent fallback to Haiku. Streaming returns chunks incrementally so the socket never
        # idles past read_timeout. Usage now arrives across events: input_tokens in
        # message_start, output_tokens (cumulative) in message_delta.
        content_parts = []
        in_tok = out_tok = cache_read = cache_write = 0
        stop_reason = None
        for event in response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            ctype = chunk.get("type")
            if ctype == "message_start":
                u = chunk.get("message", {}).get("usage", {})
                in_tok      = u.get("input_tokens", 0)
                cache_read  = u.get("cache_read_input_tokens", 0) or 0
                cache_write = u.get("cache_creation_input_tokens", 0) or 0
            elif ctype == "content_block_delta":
                d = chunk.get("delta", {})
                if d.get("type") == "text_delta":
                    content_parts.append(d.get("text", ""))
                    if on_delta is not None:
                        on_delta(d.get("text", ""))
            elif ctype == "message_delta":
                out_tok = chunk.get("usage", {}).get("output_tokens", out_tok)
                # AA-493: stop_reason ("end_turn" | "max_tokens" | "stop_sequence" | ...) arrives
                # on this same event's `delta`, alongside the cumulative output_tokens above —
                # confirmed against the real streaming schema while building this fix (STEP0).
                stop_reason = chunk.get("delta", {}).get("stop_reason", stop_reason)
        content = "".join(content_parts)
        cost    = self._calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)

        logger.info("llm_success", provider="bedrock", model=model,
                    in_tokens=in_tok, out_tokens=out_tok,
                    cache_read=cache_read, cache_write=cache_write,
                    cost_usd=cost, stop_reason=stop_reason)

        return LLMResponse(
            content=content, model_used=model, provider="bedrock",
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            # AA-288: cache_read/cache_write were parsed above and logged, but discarded before
            # this fix — the caller had no way to know a cache hit/write happened at all.
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            stop_reason=stop_reason,
        )

    def _call_bedrock_satellite(self, request: LLMRequest, model: str, account: str = "acc1") -> LLMResponse:
        """AA-296/397 — gọi Claude qua satellite account chỉ định (acc1 hoặc acc3),
        dùng khi acc2 không có Anthropic model (TrueIDC channel-program org chặn).
        Xem shared/llm_client/bedrock_satellite.py cho chi tiết AssumeRole chain +
        bug 2-dạng-ARN đã fix trong IAM policy.

        request.system_prompt được forward qua tham số system= của invoke_claude()
        (Anthropic Messages API "system" field riêng, không nối vào user prompt) —
        khớp với cách _call_bedrock (acc2, T1) gửi system qua build_cached_system_prompt.
        """
        from .bedrock_satellite import invoke_claude, BedrockUnavailable
        model_key = "sonnet" if model == BEDROCK_SONNET else "haiku"
        stream_sink.emit_restart(request.stage)  # AA-637
        try:
            result = invoke_claude(
                request.user_prompt,
                model=model_key,
                max_tokens=request.max_tokens,
                system=request.system_prompt,
                account=account,
                # AA-637 — only set when a live-progress sink streams this stage; None keeps the
                # exact pre-AA-637 non-streaming invoke_model request.
                on_delta=stream_sink.delta_callback(request.stage),
                # S224: forward an explicitly requested temperature (was silently dropped).
                temperature=(request.temperature if "temperature" in request.model_fields_set else None),
            )
        except BedrockUnavailable as e:
            raise RuntimeError(f"Satellite Bedrock failed: {e}") from e

        in_tok = result.usage.get("input_tokens", 0)
        out_tok = result.usage.get("output_tokens", 0)
        # AA-324: previously never read here at all -- cache_read_tokens/cache_write_tokens
        # always defaulted to 0 on every satellite LLMResponse regardless of what Bedrock
        # actually returned in usage, independently of whether the request-side caching
        # (invoke_claude()'s system= wiring, fixed same task) was even working. Mirrors
        # _call_bedrock()'s (acc2-native T1) own extraction above exactly.
        cache_read  = result.usage.get("cache_read_input_tokens", 0) or 0
        cache_write = result.usage.get("cache_creation_input_tokens", 0) or 0
        cost = self._calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)

        logger.info("llm_success", provider="bedrock-satellite", model=result.model_used,
                    in_tokens=in_tok, out_tokens=out_tok,
                    cache_read=cache_read, cache_write=cache_write,
                    cost_usd=cost, latency_ms=result.latency_ms, stop_reason=result.stop_reason)

        return LLMResponse(
            content=result.text,
            model_used=f"satellite-{result.model_used}",
            provider="bedrock-satellite",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            fallback_used=False,  # set lại đúng ở generate() tuỳ ngữ cảnh gọi
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            stop_reason=result.stop_reason,
        )

    def _call_catalog_model(self, request: LLMRequest, model_key: str, preferred_acct: str) -> LLMResponse:
        model = get_model_sync(model_key)
        if model is None:
            raise RuntimeError(f"Model {model_key!r} is not in shared.llm_model_catalog "
                               "(or the catalog is unreachable)")
        if not model.enabled:
            raise RuntimeError(f"Model {model_key!r} is disabled in the catalog: "
                               f"{model.blocked_reason or 'no reason given'}")
        if model.api_style == "openai_chat":
            return self._call_openai(request, model=model.wire_model or model.model_key, catalog_model=model)
        if model.api_style != "converse":
            raise RuntimeError(f"Model {model_key!r} has api_style={model.api_style!r}, "
                               "which LLMClient cannot call through the catalog path")
        account = model.account_for(preferred_acct)
        if account is None or account == "acc2":
            raise RuntimeError(f"Model {model_key!r} has no satellite account in bedrock_profile_ids")
        return self._call_bedrock_converse(request, model, account)

    def _call_bedrock_converse(self, request: LLMRequest, model: CatalogModel, account: str) -> LLMResponse:
        """AA-658 — Bedrock Converse / ConverseStream on a satellite session. One wire format for
        every vendor on Bedrock (Anthropic and OpenAI models alike)."""
        from .bedrock_satellite import get_satellite_client

        profile_id = model.bedrock_profile_ids[account]
        inference = {"maxTokens": request.max_tokens}
        if model.max_output_tokens:
            inference["maxTokens"] = min(request.max_tokens, model.max_output_tokens)
        if model.supports_temperature and "temperature" in request.model_fields_set:
            inference["temperature"] = request.temperature
        kwargs = {
            "modelId": profile_id,
            "system": [{"text": request.system_prompt}],
            "messages": [{"role": "user", "content": [{"text": request.user_prompt}]}],
            "inferenceConfig": inference,
        }
        schema = request.json_schema
        if schema:
            # AA-659 — structured output on Converse: one tool, forced, whose input is the schema.
            kwargs["toolConfig"] = {
                "tools": [{"toolSpec": {"name": schema["name"],
                                        "description": "Return the result as this object.",
                                        "inputSchema": {"json": schema["schema"]}}}],
                "toolChoice": {"tool": {"name": schema["name"]}},
            }
            # AA-803 (S246): Sonnet 5.5 / Opus 5.5 / Fable 5.1 / Mythos 5.1 400 on a forced tool choice —
            # ask them with `auto` and an explicit instruction instead (`_forced_tool_choice_ok`).
            if not _forced_tool_choice_ok(model, profile_id):
                _use_auto_tool_choice(kwargs, schema)
        # AA-803: no `thinking` / `budget_tokens` field is ever sent (no additionalModelRequestFields), so
        # adaptive-thinking-only models (Haiku 5.5, Sonnet 5.5, Opus 5.5) run their default, and the
        # last message is always the user's (no assistant prefill, which those models reject).

        stream_sink.emit_restart(request.stage)
        on_delta = None if schema else stream_sink.delta_callback(request.stage)
        rt = get_satellite_client("bedrock-runtime", account=account)

        in_tok = out_tok = cache_read = cache_write = 0
        stop_reason = None
        # AA-927: every billed Converse request of this attempt (the schema retry makes it 2).
        billed_tok: dict = {}
        provider_requests = 1

        def billed_failure(message: str, cause: Optional[BaseException] = None):
            t = billed_tok
            cost_ = self._calc_cost(model.model_key, t.get("in", 0), t.get("out", 0),
                                    cache_read=t.get("cache_read", 0), cache_write=t.get("cache_write", 0))
            exc = _billed_unusable(message, [_billed_usage(
                f"satellite-{model.model_key}", account,
                {"tokens_in": t.get("in", 0) + t.get("cache_read", 0) + t.get("cache_write", 0),
                 "tokens_out": t.get("out", 0)}, cost_, stop_reason, provider_requests)])
            exc.__cause__ = cause
            return exc

        if on_delta is None:
            resp = _converse_with_param_downgrades(rt.converse, kwargs, model.model_key, schema)
            _add_converse_usage(billed_tok, resp.get("usage", {}))
            stop_reason = resp.get("stopReason")
            # AA-927: from here on the first request is billed — any raise below (schema mismatch after
            # the retry, the retry request itself failing, a malformed body) carries the billed usage.
            try:
                blocks = resp["output"]["message"]["content"]
                if schema:
                    auto = "auto" in kwargs["toolConfig"]["toolChoice"]
                    # AA-714: Converse does not enforce the tool schema, so a model may omit a required
                    # key. Retry ONCE with an explicit instruction before giving up — far cheaper and
                    # more accurate than failing over to the next (pricier, different-vendor) model.
                    try:
                        content = _checked_json(model.model_key, _first_tool_input(blocks, schema, model, auto),
                                                schema)
                    except RuntimeError as e:
                        logger.warning("converse_schema_retry", model=model.model_key, stage=request.stage,
                                       error=str(e)[:200])
                        retry = dict(kwargs)
                        retry["messages"] = [
                            kwargs["messages"][0],
                            {"role": "assistant", "content": [{"text": "(previous answer was missing a "
                                                                       "required field)"}]},
                            {"role": "user", "content": [{"text":
                                f"Call the tool {schema['name']!r} again and include EVERY required key: "
                                f"{', '.join(schema['schema'].get('required', []))}. Return the full object."}]},
                        ]
                        resp = rt.converse(**retry)
                        provider_requests = 2
                        _add_converse_usage(billed_tok, resp.get("usage", {}))
                        stop_reason = resp.get("stopReason")
                        blocks = resp["output"]["message"]["content"]
                        content = _checked_json(model.model_key, _first_tool_input(blocks, schema, model, auto),
                                                schema)
                else:
                    content = "".join(block.get("text", "") for block in blocks)
            except Exception as e:
                raise billed_failure(str(e), e) from e
        else:
            resp = _converse_dropping_rejected_temperature(rt.converse_stream, kwargs, model.model_key)
            parts = []
            usage = {}
            for event in resp["stream"]:
                if "contentBlockDelta" in event:
                    text = event["contentBlockDelta"].get("delta", {}).get("text", "")
                    if text:
                        parts.append(text)
                        on_delta(text)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
            content = "".join(parts)
            # A stream that broke before its metadata event has no usage: cost unknown, not logged
            # (AA-927 documented exclusion — the exception surfaces as a route / caller failure).
            _add_converse_usage(billed_tok, usage)
        if not content:
            message = f"{model.model_key} returned empty content (stopReason={stop_reason})"
            if billed_tok.get("in") or billed_tok.get("out") or billed_tok.get("cache_read"):
                raise billed_failure(message)
            raise RuntimeError(message)
        in_tok = billed_tok.get("in", 0)
        out_tok = billed_tok.get("out", 0)
        cache_read = billed_tok.get("cache_read", 0)
        cache_write = billed_tok.get("cache_write", 0)
        # AA-927: after a schema retry these are the sums of BOTH billed requests (the first reply was
        # billed too) — before AA-927 only the second request was priced.
        cost = self._calc_cost(model.model_key, in_tok, out_tok,
                               cache_read=cache_read, cache_write=cache_write)
        # S234 (AA-772/PR0): Converse's `inputTokens` is only the UNCACHED part of the prompt.
        # Bedrock caches long prompt prefixes of the Luna models on its own (no cachePoint sent),
        # so llm_call_log showed tokens_in of 2-118 for 2-5k-character prompts (t10_judge,
        # t2_judge, a3_atomize on gpt-6-luna / gpt-5.6-luna) while cost_usd already priced the
        # cached tokens. `input_tokens` now carries the whole prompt; the cache split stays in
        # cache_read_tokens / cache_write_tokens, and the cost above is unchanged.
        prompt_tok = in_tok + cache_read + cache_write

        logger.info("llm_success", provider="bedrock-satellite", api="converse",
                    model=model.model_key, account=account,
                    in_tokens=prompt_tok, uncached_in_tokens=in_tok, cache_read=cache_read,
                    cache_write=cache_write, out_tokens=out_tok, cost_usd=cost, stop_reason=stop_reason)

        return LLMResponse(
            # "satellite-" prefix: call_log.py strips it and records provider=bedrock-satellite.
            content=content, model_used=f"satellite-{model.model_key}", provider="bedrock-satellite",
            input_tokens=prompt_tok, output_tokens=out_tok, cost_usd=cost,
            fallback_used=False, satellite_account=account,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            stop_reason=stop_reason, provider_requests=provider_requests,
        )

    def _call_openai(self, request: LLMRequest, model: str,
                     catalog_model: Optional[CatalogModel] = None) -> LLMResponse:
        # AA-209: forward sampling controls only when the caller explicitly set them. This makes the
        # GPT-4.1 judge reproducible (it passes temperature + seed) without changing behavior of
        # content/T3-fallback calls that rely on provider defaults (they never set these fields).
        # AA-659: catalog models (e.g. gpt-6-luna-openai) are reasoning models — they take
        # max_completion_tokens, and temperature/seed only when the catalog says they are supported.
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user",   "content": request.user_prompt},
            ],
        }
        if catalog_model is None:
            kwargs["max_tokens"] = request.max_tokens
        else:
            kwargs["max_completion_tokens"] = request.max_tokens
        sampling_ok = catalog_model is None or catalog_model.supports_temperature
        fields_set = request.model_fields_set
        if sampling_ok and "temperature" in fields_set:
            kwargs["temperature"] = request.temperature
        if sampling_ok and "seed" in fields_set and request.seed is not None:
            kwargs["seed"] = request.seed
        if request.json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.json_schema["name"], "strict": True,
                                "schema": request.json_schema["schema"]},
            }
        model_label = catalog_model.model_key if catalog_model else model
        # S235 — the single gate in front of the OpenAI platform API: every path into this adapter
        # (legacy gpt-4.1 tier, T3, catalog openai_chat rows, shadows) stops here unless the
        # model's catalog row is provider 'openai' AND enabled.
        if not openai_platform_allowed(model_label):
            raise OpenAIPlatformBlocked(
                f"{model_label}: OpenAI platform API is disabled (S235 Bedrock-only policy)")
        stream_sink.emit_restart(request.stage)  # AA-637
        resp = self._openai.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        # AA-637 — no token streaming on this last-resort path; a live view gets the whole text.
        _cb = stream_sink.delta_callback(request.stage)
        if _cb is not None and content:
            _cb(content)
        in_tok  = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
        cost    = self._calc_cost(model_label, in_tok, out_tok)
        # AA-493: OpenAI's field is "finish_reason" ("stop" | "length" | "content_filter" | ...)
        # — different name from Anthropic's "stop_reason", same purpose. Stored under the same
        # LLMResponse.stop_reason field so callers/the DB log don't need a provider branch.
        finish_reason = resp.choices[0].finish_reason

        def billed_failure(message: str) -> BilledUnusableReply:
            # AA-927: the request was billed even though the reply is unusable.
            return _billed_unusable(message, [_billed_usage(
                model_label, None, {"tokens_in": in_tok, "tokens_out": out_tok}, cost, finish_reason, 1)])

        if catalog_model is not None and not content:
            # A reasoning model can spend the whole token budget thinking and return nothing.
            raise billed_failure(f"{model_label} returned empty content (finish_reason={finish_reason})")
        if request.json_schema:
            try:
                parsed = json.loads(content)
            except ValueError as e:
                raise billed_failure(f"{model_label} returned invalid JSON for schema "
                                     f"{request.json_schema['name']!r}: {e}") from e
            try:
                content = _checked_json(model_label, parsed, request.json_schema)
            except RuntimeError as e:
                raise billed_failure(str(e)) from e

        logger.info("llm_success", provider="openai", model=model_label,
                    in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
                    stop_reason=finish_reason)

        return LLMResponse(
            content=content, model_used=model_label, provider="openai",
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            stop_reason=finish_reason,
        )

    def _calc_cost(self, model: str, in_tok: int, out_tok: int,
                   cache_read: int = 0, cache_write: int = 0) -> float:
        # AA-635 — delegates to pricing.calc_cost() (was a second copy of the same formula) so
        # cache-token pricing and any future rate fix land in one place for both mechanisms.
        return calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)
