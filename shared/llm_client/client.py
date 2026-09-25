import os
import json
import boto3
from botocore.config import Config
import openai
import structlog
from .models import LLMRequest, LLMResponse
from .prompt_cache import build_cached_system_prompt, build_cached_messages
from .pricing import BEDROCK_SONNET, BEDROCK_HAIKU, COST_TABLE, calc_cost
from .role_config import get_stage_config_sync
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
        tier = request.model_tier or (stage_cfg.model_id if stage_cfg else DEFAULT_MODEL_TIER)
        # Which satellite account is tried FIRST when acc2-native fails — 'acc3' unless the
        # stage's admin config explicitly says 'acc1' (e.g. a real acc3 outage). Unknown/no-stage
        # calls keep the pre-AA-518 hardcoded acc3-first order exactly.
        primary_acct = stage_cfg.account_route if (stage_cfg and stage_cfg.account_route) else "acc3"
        fallback_acct = "acc1" if primary_acct == "acc3" else "acc3"

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

    def _call_openai(self, request: LLMRequest, model: str) -> LLMResponse:
        # AA-209: forward sampling controls only when the caller explicitly set them. This makes the
        # GPT-4.1 judge reproducible (it passes temperature + seed) without changing behavior of
        # content/T3-fallback calls that rely on provider defaults (they never set these fields).
        kwargs = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user",   "content": request.user_prompt},
            ],
        }
        fields_set = request.model_fields_set
        if "temperature" in fields_set:
            kwargs["temperature"] = request.temperature
        if "seed" in fields_set and request.seed is not None:
            kwargs["seed"] = request.seed
        stream_sink.emit_restart(request.stage)  # AA-637
        resp = self._openai.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        # AA-637 — no token streaming on this last-resort path; a live view gets the whole text.
        _cb = stream_sink.delta_callback(request.stage)
        if _cb is not None and content:
            _cb(content)
        in_tok  = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
        cost    = self._calc_cost(model, in_tok, out_tok)
        # AA-493: OpenAI's field is "finish_reason" ("stop" | "length" | "content_filter" | ...)
        # — different name from Anthropic's "stop_reason", same purpose. Stored under the same
        # LLMResponse.stop_reason field so callers/the DB log don't need a provider branch.
        finish_reason = resp.choices[0].finish_reason

        logger.info("llm_success", provider="openai", model=model,
                    in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
                    stop_reason=finish_reason)

        return LLMResponse(
            content=content, model_used=model, provider="openai",
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            stop_reason=finish_reason,
        )

    def _calc_cost(self, model: str, in_tok: int, out_tok: int,
                   cache_read: int = 0, cache_write: int = 0) -> float:
        # AA-635 — delegates to pricing.calc_cost() (was a second copy of the same formula) so
        # cache-token pricing and any future rate fix land in one place for both mechanisms.
        return calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)
