# Model catalog in the DB + Bedrock Converse adapter, no silent cross-vendor fallback (AA-658)

- **Status:** Accepted (28/09/2026, S200).
- **Implements:** ecosystem ADR 0001, decision 2 (single Model Gateway) — `AA-Ecosys/docs/adr/`.
  This ADR records the App-level shape of its first phase (P0) only.

## Context

Adding one model today means editing four places by hand: `pricing.py` (prices), the
`_WRITER_OPTIONS` / `_JUDGE_OPTIONS` lists in `admin_llm_ops.py` (dropdown),
`_INFERENCE_PROFILES` in `bedrock_satellite.py` (per-account profile), and the acc3 invoker IAM.
An unknown model is silently priced as Sonnet.

Every Bedrock call builds an Anthropic-native body, so the OpenAI models on Bedrock (GPT-5.6 Luna,
GPT-6 Astra) cannot be called at all. The new models (Sonnet 5, Opus 5.5, Luna, Astra) are
enabled on acc3 only.

The admin dropdown also misleads: judge stages are hardcoded to GPT-4.1 (`brand_fit.py`,
`judge_client.py`), and `s1_brand_audit` calls the OpenAI API directly, so picking a
Bedrock-only model there would either do nothing or fail.

## Decision

1. **`shared.llm_model_catalog` is the single source** for the dropdown and for prices. One row per
   **Model Key**. The existing keys `haiku`, `sonnet` and `gpt-4.1` are kept unchanged, so no
   `llm_role_config` data moves. New keys: `sonnet-5`, `opus-5-5`, `gpt-5.6-luna`, `gpt-6-astra`,
   `cohere-embed-v4`.
2. **Per-account profiles live in one JSONB column** (`bedrock_profile_ids`, e.g.
   `{"acc3": "global.anthropic.claude-sonnet-5"}`). The new models list acc3 only.
3. **A new Converse adapter** in `LLMClient` handles every catalog row with
   `api_style = 'converse'`, on the acc3 satellite session. The existing Anthropic-native Haiku /
   Sonnet path is left untouched until parity is proven.
4. **No silent fallback for Converse models in P0.** A failure raises. Falling from a GPT judge to
   Haiku, or from Sonnet 5 to GPT-4.1, would change vendor without anyone noticing. Fallback chains
   become explicit route config in AA-659.
5. **The dropdown only offers what a stage can really execute.** Each catalog row declares
   `callable_via`. A stage whose call site goes through `LLMClient` without a pinned
   `model_tier` (the writer stages) may pick Converse models. Judge stages stay on GPT-4.1, and
   the new models are shown there as blocked with the reason "needs AA-659".
6. **Prices come from the catalog**, cached in-process for 5 minutes. `pricing.py` is only the
   fallback when the DB is unreachable. An unknown model logs a warning instead of silently using
   Sonnet rates.
7. **A model without a verified price is seeded `enabled = false`.** Opus 5.5 and GPT-6 Astra
   (no price on the public Bedrock page on 28/09/2026) stay disabled until a real price is entered.
8. **Converse calls log as `provider = 'bedrock-satellite'`, `account = 'acc3'`**, reusing the
   existing `llm_call_log` values.
9. **The catalog table is owned by CIS.** TripPlanner and AA-Booking may read it; they never write
   it. The gateway code stays in this repo until a second app actually needs it (P5).

## Considered options

- *Keep the lists in code, add the new models by hand.* Rejected: it repeats the four-place edit
  for every future model, and prices keep drifting from the bill (AA-635).
- *Fall back from a failed Converse call into the existing Haiku chain.* Rejected: see decision 4.
- *Show every model for every stage.* Rejected: the UI would claim changes that never happen
  (the same lesson as AA-636).

## Consequences

- Adding a model is a catalog row plus the acc3 IAM allow-list (AA-657), not a code change.
- A writer stage switched to a Converse model has no fallback until AA-659 lands. That is
  accepted for P0, because the switch is an explicit admin action.
- Five call sites still bypass `LLMClient` and write no cost log (export, column mapper, H3,
  embeddings, TripPlanner). They are moved behind the gateway in AA-659.
