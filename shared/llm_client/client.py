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
# Options: "haiku" (cheapest) | "sonnet" (premium) | "gpt-4.1" (OpenAI)
DEFAULT_MODEL_TIER = os.environ.get("DEFAULT_MODEL_TIER", "haiku")

# BEDROCK_SONNET/BEDROCK_HAIKU/COST_TABLE relocated to pricing.py (AA-518/AA-505) so Mechanism-B
# call sites (invoke_claude() direct, e.g. T5 atomize, N7 E2-E5) can price a call without
# importing this whole client — re-exported here unchanged so nothing importing them FROM this
# module breaks.

def _checked_json(model_label: str, value, schema: dict) -> str:
    """AA-659 — a structured output that does not match its schema raises, so the stage route
    moves to its next model instead of a caller's error path accepting it silently."""
    try:
        return json.dumps(conform(value, schema["schema"]))
    except SchemaMismatch as e:
        raise RuntimeError(f"{model_label} output does not match schema {schema['name']!r}: {e}") from e


class LLMClient:
    """
    Fallback chain (AA-397/398 — acc3 satellite thêm vào làm satellite chính,
    acc1 lùi xuống fallback; native T1/T2 trên acc2 KHÔNG đổi):
    T1:    Claude Sonnet 4.5 (Bedrock, acc2 native) + prompt caching
    T1.5a: Claude Sonnet     (Bedrock satellite, acc3 — AA-397)
    T1.5b: Claude Sonnet     (Bedrock satellite, acc1 — AA-296, nay là fallback)
    T2:    Claude Haiku 4.5  (Bedrock, acc2 native) + prompt caching
    T2.5a: Claude Haiku      (Bedrock satellite, acc3 — AA-397)
    T2.5b: Claude Haiku      (Bedrock satellite, acc1 — AA-296, nay là fallback)
    T3:    GPT-4.1           (OpenAI)
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
        # Which satellite account is tried FIRST when acc2-native fails — 'acc3' unless the
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

        if len(chain) == 1:
            resp = self._call_key(request, chain[0], primary_acct, fallback_acct)
        else:
            resp = self._call_route(request, chain, primary_acct, fallback_acct)

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

    def _call_route(self, request: LLMRequest, chain: list, primary_acct: str, fallback_acct: str) -> LLMResponse:
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
                resp = self._call_key(request, key, primary_acct, fallback_acct)
            except Exception as e:
                logger.warning("llm_route_attempt_failed", stage=request.stage, model=key,
                               position=position, error=str(e)[:300])
                errors.append(f"{key}: {str(e)[:200]}")
                continue
            if position > 0:
                resp.fallback_used = True
            logger.info("llm_route_used", stage=request.stage, model=key, position=position)
            return resp
        raise RuntimeError(f"All models in the route failed for stage {request.stage!r}: "
                           + "; ".join(errors))

    def _start_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                      primary_acct: str, fallback_acct: str) -> None:
        # A plain thread does not inherit contextvars, so the shadow never writes to the
        # live-progress stream sink bound to the primary call.
        threading.Thread(
            target=self._run_shadow,
            args=(request.model_copy(), cfg, primary, primary_acct, fallback_acct),
            daemon=True,
        ).start()

    def _run_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                    primary_acct: str, fallback_acct: str) -> None:
        from .shadow import record_shadow_sync
        t0 = time.monotonic()
        resp, error = None, None
        try:
            resp = self._call_key(request, cfg.shadow_model_id, primary_acct, fallback_acct)
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

        if tier == "sonnet":
            # T1: Claude Sonnet — premium quality
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
                logger.info("t1_5a_satellite_used", model=BEDROCK_SONNET, account=primary_acct, reason="acc2 T1 failed")
                return resp
            except Exception as e:
                logger.warning("t1_5a_satellite_failed_trying_t1_5b", model=BEDROCK_SONNET,
                               account=primary_acct, error=str(e))

            # T1.5b: Claude Sonnet qua satellite FALLBACK account
            try:
                resp = self._call_bedrock_satellite(request, model=BEDROCK_SONNET, account=fallback_acct)
                resp.fallback_used = False
                resp.satellite_account = fallback_acct
                logger.info("t1_5b_satellite_used", model=BEDROCK_SONNET,
                           account=fallback_acct, reason="acc2 T1 + T1.5a failed")
                return resp
            except Exception as e:
                logger.warning("t1_5b_satellite_failed_trying_t2", model=BEDROCK_SONNET,
                               account=fallback_acct, error=str(e))

        # T2: Claude Haiku — fast / default tier, or Sonnet fallback
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
            logger.info("t2_5a_satellite_used", model=BEDROCK_HAIKU, account=primary_acct, reason="acc2 T2 failed")
            return resp
        except Exception as e:
            logger.warning("t2_5a_satellite_failed_trying_t2_5b", model=BEDROCK_HAIKU,
                           account=primary_acct, error=str(e))

        # T2.5b: Claude Haiku qua satellite FALLBACK account
        try:
            resp = self._call_bedrock_satellite(request, model=BEDROCK_HAIKU, account=fallback_acct)
            resp.fallback_used = tier == "sonnet"
            resp.satellite_account = fallback_acct
            logger.info("t2_5b_satellite_used", model=BEDROCK_HAIKU,
                       account=fallback_acct, reason="acc2 T2 + T2.5a failed")
            return resp
        except Exception as e:
            logger.warning("t2_5b_satellite_failed_trying_t3", model=BEDROCK_HAIKU, account=fallback_acct, error=str(e))

        # T3: GPT-4.1 — last resort for all tiers
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

        stream_sink.emit_restart(request.stage)
        on_delta = None if schema else stream_sink.delta_callback(request.stage)
        rt = get_satellite_client("bedrock-runtime", account=account)

        in_tok = out_tok = cache_read = cache_write = 0
        stop_reason = None
        if on_delta is None:
            resp = rt.converse(**kwargs)
            blocks = resp["output"]["message"]["content"]
            if schema:
                tool_inputs = [b["toolUse"]["input"] for b in blocks if "toolUse" in b]
                if not tool_inputs:
                    raise RuntimeError(f"{model.model_key} returned no tool call for schema {schema['name']!r}")
                content = _checked_json(model.model_key, tool_inputs[0], schema)
            else:
                content = "".join(block.get("text", "") for block in blocks)
            usage = resp.get("usage", {})
            stop_reason = resp.get("stopReason")
        else:
            resp = rt.converse_stream(**kwargs)
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
        if not content:
            raise RuntimeError(f"{model.model_key} returned empty content (stopReason={stop_reason})")
        in_tok = usage.get("inputTokens", 0) or 0
        out_tok = usage.get("outputTokens", 0) or 0
        cache_read = usage.get("cacheReadInputTokens", 0) or 0
        cache_write = usage.get("cacheWriteInputTokens", 0) or 0
        cost = self._calc_cost(model.model_key, in_tok, out_tok,
                               cache_read=cache_read, cache_write=cache_write)

        logger.info("llm_success", provider="bedrock-satellite", api="converse",
                    model=model.model_key, account=account,
                    in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost, stop_reason=stop_reason)

        return LLMResponse(
            # "satellite-" prefix: call_log.py strips it and records provider=bedrock-satellite.
            content=content, model_used=f"satellite-{model.model_key}", provider="bedrock-satellite",
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            fallback_used=False, satellite_account=account,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            stop_reason=stop_reason,
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
        if catalog_model is not None and not content:
            # A reasoning model can spend the whole token budget thinking and return nothing.
            raise RuntimeError(f"{model_label} returned empty content (finish_reason={finish_reason})")
        if request.json_schema:
            try:
                parsed = json.loads(content)
            except ValueError as e:
                raise RuntimeError(f"{model_label} returned invalid JSON for schema "
                                   f"{request.json_schema['name']!r}: {e}") from e
            content = _checked_json(model_label, parsed, request.json_schema)

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
