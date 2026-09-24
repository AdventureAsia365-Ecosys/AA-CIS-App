"""AA-518/AA-505 — shared Bedrock/OpenAI pricing table, extracted out of client.py so both
LLMClient (Mechanism A) and the Mechanism-B call sites that invoke shared/llm_client/
bedrock_satellite.py::invoke_claude() directly (T5 atomize, N7 E2-E5, s1_from_atom.py, DFS gap
research) can compute a real cost_usd for shared.llm_call_log without duplicating the table or
importing client.py (which would be a heavier/circular-risking import for those modules).

Values themselves are UNCHANGED from client.py's own COST_TABLE — this is a pure relocation, not
a repricing. client.py re-exports the same names so nothing importing from client.py breaks.
"""

# Bedrock model IDs (cross-region inference profiles)
# AA-348 — BEDROCK_SONNET names the acc2-NATIVE (T1) Sonnet model only. When a call falls
# through to a satellite account (T1.5a/T1.5b, shared/llm_client/bedrock_satellite.py — the
# common path in practice, native Sonnet is blocked for channel-program accounts, AA-291/AA-329),
# the model actually invoked is a DIFFERENT, newer version: `global.anthropic.claude-sonnet-4-6`
# (see bedrock_satellite.py's INFERENCE_PROFILE_SONNET / _INFERENCE_PROFILES). This is a
# deliberate, live-verified difference between the two tiers (bedrock_satellite.py's own
# 16/07/2026 STATUS note), NOT a copy-paste bug — investigated and confirmed still the real state
# under AA-348. client.py still passes this constant into the satellite call path, but only as a
# TIER SELECTOR / COST_TABLE key, never as a claim about the literal model string that tier
# invokes — check bedrock_satellite.py directly if you need the real satellite model version.
BEDROCK_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# $ per 1K tokens, {"in": ..., "out": ...}
# AA-635 — Haiku was {"in": 0.00025, "out": 0.00125}, i.e. Claude *3* Haiku's $0.25/$1.25 per MTok.
# The model actually invoked everywhere is Claude Haiku 4.5 ($1/$5 per MTok), so every Haiku call
# was logged at 25% of what AWS bills. Sonnet 4.5 (acc2-native) and Sonnet 4.6 (satellite) share
# $3/$15, so the one Sonnet entry prices both tiers correctly.
COST_TABLE = {
    BEDROCK_SONNET: {"in": 0.003, "out": 0.015},
    BEDROCK_HAIKU: {"in": 0.001, "out": 0.005},
    "gpt-4.1": {"in": 0.002, "out": 0.008},
}

# AA-635 — Anthropic prompt-cache pricing, as multiples of the model's base input rate. Anthropic
# usage reports cache tokens SEPARATELY from `input_tokens` (which excludes them), so a caller that
# prices only input/output tokens misses every cache write (billed at 1.25x) and read (0.1x).
# 1.25x is the 5-minute-TTL write rate — the only TTL prompt_cache.py sets.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

# Mechanism-B callers (invoke_claude) pass the short model key ("sonnet"/"haiku"), not the full
# Bedrock model id — this maps that key to the same COST_TABLE entry Mechanism A uses, so both
# mechanisms price identically instead of drifting. AA-635: also maps the `model_used` labels
# bedrock_satellite.py returns ("haiku-4-5"/"sonnet-4-6", optionally "satellite-"-prefixed), which
# some log call sites pass straight through — those used to miss the table and fall back to Sonnet.
_SHORT_KEY_TO_MODEL_ID = {
    "sonnet": BEDROCK_SONNET, "haiku": BEDROCK_HAIKU,
    "sonnet-4-6": BEDROCK_SONNET, "haiku-4-5": BEDROCK_HAIKU,
    "satellite-sonnet-4-6": BEDROCK_SONNET, "satellite-haiku-4-5": BEDROCK_HAIKU,
}


def calc_cost(model: str, in_tok: int, out_tok: int,
              cache_read: int = 0, cache_write: int = 0) -> float:
    """`model` accepts either a full Bedrock model id, a short key ("sonnet"/"haiku"), a
    bedrock_satellite `model_used` label, or "gpt-4.1". Unknown model falls back to Sonnet-tier
    rates (matches client.py's own prior `_calc_cost` fallback exactly) rather than raising —
    pricing must never be the reason an LLM call fails.

    `cache_read`/`cache_write` (AA-635) are Anthropic's `cache_read_input_tokens` /
    `cache_creation_input_tokens`, priced off the model's input rate. Default 0 keeps every
    existing caller's result unchanged."""
    resolved = _SHORT_KEY_TO_MODEL_ID.get(model, model)
    rates = COST_TABLE.get(resolved, {"in": 0.003, "out": 0.015})
    cache_cost = rates["in"] * (
        (cache_write or 0) * CACHE_WRITE_MULTIPLIER + (cache_read or 0) * CACHE_READ_MULTIPLIER
    )
    return round((in_tok * rates["in"] + out_tok * rates["out"] + cache_cost) / 1000, 6)
