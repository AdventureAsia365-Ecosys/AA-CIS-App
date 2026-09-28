# Stage routes: explicit fallback chains and a shadow model; judges move to GPT-5.6 Luna (AA-659)

- **Status:** Accepted (28/09/2026, S200).
- **Implements:** ecosystem ADR 0001 decision 2; builds on ADR 0005.

## Context

After ADR 0005 the catalog decides *which* models exist, but each stage still had one model and no
way to recover when it failed, except the hand-written Haiku/Sonnet chain inside `LLMClient`. The
four judge stages bypassed the admin config entirely: `brand_fit.py` pinned GPT-4.1,
`judge_client.py` read `JUDGE_MODEL`, and `brand_audit_node.py` called the OpenAI API directly.

Nghiệp decided on 28/09 to move the judges off GPT-4.1:
- use GPT-5.6 Luna on Bedrock first, because it is available on acc3 today;
- keep GPT-6 Luna through the OpenAI API;
- keep A/B-checking the switch.

GPT-6 Luna on Bedrock is not usable yet (acc3 agreement not accepted).

## Decision

1. **A stage route is `model_id` + an ordered `fallback_model_ids` list** (`shared.llm_role_config`).
   `LLMClient` tries them in order, skips catalog rows that are disabled, and never adds a model
   the route does not list. When every entry fails it raises an error naming each attempt.
   `fallback_used` is true whenever the answer did not come from the first entry.
2. **A fallback may be a different model than the primary** when the admin lists it. For the
   judges this is a deliberate choice: GPT-5.6 Luna → GPT-6 Luna (Bedrock, once enabled) →
   GPT-6 Luna (OpenAI API). GPT-4.1 is **not** in the chain.
3. **Shadow model:** `shadow_model_id` + `shadow_sample_pct`. The shadow runs on a background
   thread after the primary returns. It never changes the output. Both outputs go to
   `shared.llm_shadow_log`, and the shadow's cost goes to `llm_call_log`. After the switch GPT-4.1
   is the shadow at 100%, so every judge call is still A/B-compared.
4. **An explicit `model_tier` on a request runs alone:** no route, no shadow (scripts, AA-237).
5. **The judges no longer have their own model code paths.** Writer/judge isolation
   (ADR-2026-014/027) is kept by the vendor rule enforced in admin: writers are Anthropic, judges
   are not.
6. **Structured output** (`json_schema` on the request): strict `response_format` on the OpenAI
   API, a single forced tool on Bedrock Converse. A forced tool does not enforce the schema: on
   the first live run GPT-5.6 Luna left out a required key. So **the gateway checks every
   structured output against its schema**, and a mismatch raises and moves the route on. Reasoning models get `max_completion_tokens`,
   and `temperature`/`seed` only when the catalog allows them. An empty answer raises, so the
   route moves on instead of passing an empty string to a gate.
7. **`SAFE_DEFAULTS` keep GPT-4.1 for the judges with no route.** If the DB cannot be read, the
   judges run on the last known-good setup.

## Consequences

- The switch (migration 171) and a rollback are data changes, not deploys.
- Judge scores may be less reproducible: the Luna models ignore `temperature`/`seed`. This is
  measured from the shadow log before AA-644 closes.
- `brand_audit` still reports `pass` when every model in its route fails (its existing graceful
  path). The route makes that less likely, but it is not a hard fail.
- Shadow threads are best-effort: a deploy that kills the process can lose a shadow row.
