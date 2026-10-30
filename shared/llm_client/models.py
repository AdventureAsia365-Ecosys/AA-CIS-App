from pydantic import BaseModel
from typing import Optional

# AA-1002 (gateway slice S2): the LLMResponse.model_used label of a call served by the Anthropic Messages API
# ("anthropic-api-sonnet-5-5"). Like "satellite-", call_log strips it (llm_call_log.model = the Model Key) and derives
# provider = 'anthropic' from it, so every caller that logs only `model=resp.model_used` records the right provider.
ANTHROPIC_API_LABEL_PREFIX = "anthropic-api-"
# AA-1002 (S9): the label of a reply from an Azure OpenAI connection ("azure-openai-<catalog key>").
AZURE_OPENAI_LABEL_PREFIX = "azure-openai-"
VENDOR_LABEL_PREFIXES = (ANTHROPIC_API_LABEL_PREFIX, AZURE_OPENAI_LABEL_PREFIX)

class LLMRequest(BaseModel):
    system_prompt: str
    user_prompt:   str
    few_shots:     list[dict] = []
    max_tokens:    int = 4096
    temperature:   float = 0.7
    # model_tier: a per-request model override (a catalog Model Key, or a legacy "haiku" / "sonnet"
    # tier; "gpt-4.1" is retired, S235). None — the normal case — uses the `stage` route below.
    # AA-518 (02/09/2026): default changed "haiku" -> None. Every real caller in this codebase
    # already set this explicitly (grep-confirmed before the change — see docs/implementation-
    # notes/AA-518.md "s1_generate stage" note), so the old "haiku" default was already dead
    # weight for every existing call site; None now means "no per-request override — read the
    # admin's stage config instead" (generate()'s own new `stage` param), an explicit tier still
    # wins over config exactly like AA-237's opt-in haiku->sonnet auto-upgrade always has.
    model_tier:    Optional[str] = None
    # AA-518 — which shared.llm_role_config row generate() should fall back to when model_tier
    # is unset. None (every pre-AA-518 caller) keeps the old DEFAULT_MODEL_TIER env-var fallback,
    # unchanged.
    stage:         Optional[str] = None
    # AA-209: optional sampling seed. Forwarded to OpenAI only when explicitly set, so the judge
    # can run reproducibly while content calls that omit it keep provider-default behavior.
    seed:          Optional[int] = None
    # AA-659 — structured output: {"name": str, "schema": dict}. OpenAI -> strict json_schema
    # response_format; Bedrock Converse -> one forced tool whose input is the schema; S251: a
    # legacy haiku/sonnet key -> an instruction with the schema in the system prompt, the reply
    # checked the same way. The response content is then the JSON text of that object.
    json_schema:   Optional[dict] = None

class LLMResponse(BaseModel):
    content:       str
    model_used:    str
    provider:      str
    input_tokens:  int = 0
    output_tokens: int = 0
    cost_usd:      float = 0.0
    fallback_used: bool = False
    # AA-296/397 — the satellite account that actually answered ("acc3", the only one since
    # AA-998), None for acc2 native or GPT-4.1; not the same as fallback_used (lower quality than
    # intended). Before AA-397 this was a bool `satellite_used`.
    satellite_account: Optional[str] = None
    # AA-288: tokens read from / written to the Bedrock prompt cache for this call. Non-zero from
    # _call_bedrock (acc2, use_cache=True), the Claude satellite path, and — S234 — the Converse
    # path, where Bedrock caches long Luna prompts on its own. On the Anthropic paths
    # `input_tokens` EXCLUDES these (Anthropic's convention); on the Converse path `input_tokens`
    # is the whole prompt (uncached + cache read + cache write) so llm_call_log.tokens_in is real.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # AA-1002 (S6): the 1-hour-TTL part of cache_write_tokens (Anthropic API), and how cost_usd was priced
    # (pricing.Quote.pricing_mode: override / catalog / code_table / estimate, +batch, +geo_us; None = not said,
    # call_log derives the basis from the model label).
    cache_write_1h_tokens: int = 0
    pricing_mode: Optional[str] = None
    # AA-1002 (S6): the vendor-API connection that answered ('openai-direct', 'anthropic-direct'); None on Bedrock,
    # whose AWS account stays in satellite_account. Never put a connection key in satellite_account: the pipeline
    # copies it to silver_aa_internal.generated_content.satellite_account (CHECK: an AWS account key or NULL).
    # call_log.billed_fields() passes it on; the row stores it as `account` when the caller passes none.
    connection_key: Optional[str] = None
    # AA-493: why generation stopped — Anthropic's `stop_reason` ("end_turn" | "max_tokens" |
    # "stop_sequence" | ...) for both _call_bedrock (native) and _call_bedrock_satellite
    # (acc3), OpenAI's `finish_reason` ("stop" | "length" | ...) for _call_openai. None only
    # when a provider genuinely omits the field (never observed, but not assumed). This is the
    # only way to tell "LLM finished writing" apart from "LLM got cut off at max_tokens" — see
    # AA-493/AA-341 for the truncation-vs-compression ambiguity this closes.
    stop_reason: Optional[str] = None
    # AA-927: how many billed provider requests this one response stands for. 2 when the Converse
    # schema retry (AA-714) ran: the first, unusable reply was billed too, and its tokens + cost are
    # folded into this response (so the caller's single llm_call_log row carries both).
    provider_requests: int = 1
    # AA-1002 (S2): the model id the provider says answered (Anthropic `response.model`), when it reports one. The
    # gateway prices by the catalog Model Key; this is the evidence of what really ran. None on the other paths.
    provider_model: Optional[str] = None
