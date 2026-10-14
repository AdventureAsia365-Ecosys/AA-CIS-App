# S235 — OpenAI platform API only as last-resort fallback; GPT-4.1 retired

Branch `chore/s235-bedrock-only`. No Linear issue picked yet (the orchestrator assigns one; AA-801
is a different scope and is not referenced).

## Decisions

- **Policy (Nghiệp, 14/10/2026, revised the same day).** First instruction was "Bedrock only, nothing
  through the OpenAI platform API". Revised scope: Bedrock first; the OpenAI platform API stays as the
  **last-resort fallback for a Bedrock outage** — never a primary, never a shadow. GPT-4.1 is retired.
- **Tenant data.** Tenant content can reach OpenAI only during a Bedrock outage, through the last
  fallback of a judge route. That is Nghiệp's decision. The DPA/ZDR status of the OpenAI API account
  still has to be recorded: by default OpenAI does not train on API data, and zero data retention
  (ZDR) is only available on request. The orchestrator raises this with Nghiệp.
- **Where the rule is enforced.**
  1. Data: migration 224 (`api/migrations/224_openai_last_resort_only.sql`).
  2. Defaults: `_JUDGE_ROUTE_DEFAULT = ("gpt-6-luna", "gpt-6-luna-openai")` in `role_config.py`.
  3. Runtime: there is one gate in front of the adapter, `openai_platform_allowed()` in `client.py`.
     `_call_openai` refuses any model whose catalog row is not `provider='openai'` AND `enabled`. It
     fails closed when the catalog cannot be read.
  4. Admin: `PATCH /admin/llm-config/{stage}` rejects an OpenAI-API primary.
     `PATCH .../route` rejects an OpenAI-API shadow, or one that is not the last fallback.
     `PATCH /admin/llm-catalog/gpt-4.1 {enabled:true}` returns 422 (`RETIRED_KEYS`).
- **Migration 224 is generic, with two fixed keys.** It removes `gpt-4.1` everywhere. It keeps an
  OpenAI-API key (catalog `provider='openai'`) only when that key is the last element of
  `fallback_model_ids`. It clears `gpt-4.1` shadows and disables the `gpt-4.1` catalog row. If an
  OpenAI-API primary or shadow remains, it RAISEs: on Dev it changes nothing in any route (gpt-4.1
  is in none), and only the catalog row flips. A fresh DB, in CI, also loses the judges'
  `gpt-4.1` shadow.
- **The legacy Haiku/Sonnet chain no longer falls back to GPT-4.1 (T3).** Before, every writer call
  ended in a GPT-4.1 OpenAI call after acc2 → acc3 → acc1 failed. Now it raises. I did not add an
  OpenAI last resort for writers: writers are Anthropic, and an OpenAI fallback would change vendor
  without anyone noticing (ADR 0005 §4). Only judge routes list the OpenAI API, and they list it
  explicitly.
- **Vendor separation is unchanged.** Judges are GPT-5.6 Luna → GPT-6 Luna (Bedrock) → GPT-6 Luna
  (OpenAI API), all non-Anthropic. No Anthropic model judges Anthropic-written content.
- **Accounts.** Converse catalog models (the Luna judges) have **no acc3→acc1 fallback**.
  `CatalogModel.account_for()` picks one account, and the Luna rows list acc3 only, because
  OpenAI-on-Bedrock is enabled on acc3 only. The acc2 → acc3 → acc1 chain exists only for the legacy
  `haiku`/`sonnet` keys. When acc3 is down, the judge route goes straight to the OpenAI API last
  resort. The TripPlanner fork (below) does iterate accounts per model.

## Changed

- `shared/llm_client/role_config.py`: the judge default drops `gpt-4.1`.
- `shared/llm_client/client.py`:
  - adds `OpenAIPlatformBlocked` and `openai_platform_allowed()`, and gates `_call_openai`;
  - the legacy T3 is skipped (event `t3_skipped_gpt41_retired`);
  - when the route is served by provider `openai`, the new warning event `llm_openai_fallback_used`
    is logged (stage, model, position, the earlier errors).
- `shared/llm_client/catalog.py`: adds `RETIRED_KEYS = {"gpt-4.1"}` and `CatalogPolicyBlocked`.
  `set_catalog_price` refuses to enable a retired key, before any UPDATE or audit row.
- `api/routers/admin_llm_ops.py`: enforces the last-resort-only rule on the model and route PATCH.
  Maps `CatalogPolicyBlocked` to 422. The static (catalog-down) judge option `gpt-4.1` now shows as
  not available.
- `CONTEXT.md`: the Stage Route section now describes the S235 rule and the live routes. Before, it
  still claimed `s1_brand_audit` used GPT-4.1 as its primary.
- Tests:
  - new: `tests/unit/test_s235_openai_last_resort.py` and
    `tests/integration/test_s235_openai_last_resort.py`;
  - rewritten: `test_aa714_judge_safe_defaults.py`;
  - adapter tests in `test_aa209`/`test_aa659*` open the gate explicitly;
  - `test_aa686` route tests use a Bedrock shadow.

## Tradeoffs

- **DB unreachable on a cold process: judges now raise.** AA-714 kept `gpt-4.1` last, because it is a
  legacy key that needs no catalog row. Without it, every judge key is a catalog key and is skipped as
  "not in catalog". This is accepted for three reasons:
  - the catalog keeps its last good copy in memory after one successful load;
  - `shared.job` and the judge's result writes need the same DB anyway;
  - the alternative was keeping a retired OpenAI model as the outage path.
- **The adapter is kept, not deleted.** It is live code for `gpt-6-luna-openai` (`openai_chat`).
  With `gpt-4.1` disabled, the legacy branches `tier == "gpt-4.1"` and T3 are effectively dead. I
  left them behind the gate rather than deleting them: removing them is a separate cleanup.
- **`OPENAI_API_KEY` stays injected** (ECS api and worker, `modules/ecs/main.tf`), because the last
  resort needs it.

## Should know

- Live `s1_brand_audit`/`t2_brand_audit` routes have **no** OpenAI last resort: GPT-5.6 Luna → GPT-6
  Luna. The SAFE_DEFAULTS for those two stages do include it, because the defaults share
  `_JUDGE_ROUTE_DEFAULT`. That gap existed before this change. Migration 224 does not add a last
  resort where there was none.
- What gets logged when the OpenAI last resort serves a call:
  - `LLMResponse.fallback_used=True` (position > 0) and `provider='openai'`;
  - every judge call site passes `fallback_used=resp.fallback_used` to `record_call`: `judge_node`,
    `brand_audit_node`, `brand_fit`, `quality_gates` (T10), `debate` (T7);
  - `_normalize_model_provider("gpt-6-luna-openai")` gives `provider='openai'`, `account` NULL.
  So `llm_call_log` shows `provider='openai' AND fallback_used`.
- **Observability proposal (not applied — Infra/observability follow-up).** Copy the pattern of the
  AA-756 `log_write_failed` filters in `AA-CIS-Infra/accounts/aa365/observability.tf`:
  - `aws_cloudwatch_log_metric_filter` "openai_fallback_used" on `/ecs/aa-cis-dev` with the pattern
    `"\"llm_openai_fallback_used\""`;
  - metric `openai_fallback_used`, namespace `aa-cis/ops`, value 1;
  - `aws_cloudwatch_metric_alarm`: Sum > 0 over 300 s, `treat_missing_data = notBreaching`, actions
    to `aws_sns_topic.alerts`.

  Renaming the event in the app silently disables the alarm, so keep the names in sync. Optional
  admin counter: `SELECT stage, count(*), max(created_at) FROM shared.llm_call_log WHERE provider =
  'openai' AND created_at > now() - interval '30 days' GROUP BY stage`. It could go on the External
  Spend / LLM Ops page.
- **TripPlanner fork** (`AA-TripPlanner-Web/backend/shared/llm_gateway.py`, read-only here): it is
  Bedrock only. `_text_call` supports `anthropic_native` and `converse` and raises on any other
  `api_style`, so an `openai_chat` row can never be called from TripPlanner. Its `tp_*` routes list
  no OpenAI model. No change is needed.
- **Other OpenAI paths found:**
  - `requirements.txt` has `openai>=1.16.2`;
  - CI passes `OPENAI_API_KEY: sk-test-dummy`;
  - the `eval-regression` workflow runs the S1 graph through `LLMClient` (writer `model_tier="haiku"`
    plus the judge stage routes), so it follows the same routes and gates;
  - `scripts/` has no OpenAI calls;
  - `tests/unit/test_aa685_no_raw_model_calls.py` already fails on any `chat.completions.create`
    outside `shared/llm_client/`;
  - `.tmp-session/thu/aa-soscial-media-*/…/openai_client.py` is Ms. Thư's external reference repo,
    not deployed by us.
- **Infra:** the migration needs an identical copy in `AA-CIS-Infra/db/migrations/cis/`, and the
  DB Migrate workflow must run before the app PR is merged.
