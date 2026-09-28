from pydantic import BaseModel
from typing import Optional

class LLMRequest(BaseModel):
    system_prompt: str
    user_prompt:   str
    few_shots:     list[dict] = []
    max_tokens:    int = 4096
    temperature:   float = 0.7
    # model_tier controls which Bedrock model to start from:
    #   "haiku"  → skip T1, go directly to T2 (Haiku) — fast/cheap
    #   "sonnet" → try T1 (Sonnet) first, fall back to T2 then T3
    #   "gpt-4.1"→ OpenAI direct, no Bedrock fallback
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
    # response_format; Bedrock Converse -> one forced tool whose input is the schema. The
    # response content is then the JSON text of that object.
    json_schema:   Optional[dict] = None

class LLMResponse(BaseModel):
    content:       str
    model_used:    str
    provider:      str
    input_tokens:  int = 0
    output_tokens: int = 0
    cost_usd:      float = 0.0
    fallback_used: bool = False
    # AA-296/397 — account satellite thực sự đã trả response ("acc1"/"acc3"), None nếu qua
    # acc2 native hoặc GPT-4.1; khác fallback_used (chất lượng thấp hơn ý định). Trước AA-397
    # đây là bool `satellite_used` — đổi sang str|None để phân biệt acc1 vs acc3.
    satellite_account: Optional[str] = None
    # AA-288: tokens read from / written to the Bedrock prompt cache for this call. Only ever
    # non-zero from _call_bedrock (acc2, use_cache=True) — satellite/OpenAI calls don't cache,
    # so they keep the 0 default rather than a caller having to know which provider ran.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # AA-493: why generation stopped — Anthropic's `stop_reason` ("end_turn" | "max_tokens" |
    # "stop_sequence" | ...) for both _call_bedrock (native) and _call_bedrock_satellite
    # (acc1/acc3), OpenAI's `finish_reason` ("stop" | "length" | ...) for _call_openai. None only
    # when a provider genuinely omits the field (never observed, but not assumed). This is the
    # only way to tell "LLM finished writing" apart from "LLM got cut off at max_tokens" — see
    # AA-493/AA-341 for the truncation-vs-compression ambiguity this closes.
    stop_reason: Optional[str] = None
