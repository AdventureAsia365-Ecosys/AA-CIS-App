# AA-1026 slice I6 — acc2 fallback for judge / validate stages (lane gw-acc2-fallback, S263)

Tech lead approval S263 (01/11): "thêm đường dự phòng acc2". Three CIS PRs, in merge order:

1. `feat/aa-1026-acc2-judge-fallback`: migration 331 (contract) + its integration test + a unit test that walks the
   post-331 routes through the gateway. Migration only, no readiness raise (MIG-4).
2. `feat/aa-1026-contract-smoke`: the contract smoke job + admin route, an optional `client` on the place card
   calls, CONTEXT.md, these notes. Works with or without 331 (it reads the route, else uses the planned model).
3. `feat/aa-1026-tenant-acc2-fallback` (held): migration 332, the same fallback for the four tenant judge / validate
   stages. Merge only after the tech lead says yes to the ADR 0009 decision 14 question below.

## Decisions

- **Which stages.** Every judge / validate stage that the failure-domain rule (`invariant.py`) reports in one
  domain after migration 327, computed from `tests/unit/snapshots/aa1026_routes_after_327.json` (Dev routes of 29/10
  with 327 applied): s1_brand_audit, s1_source_facts, a3_atomize, a3_place_card, a3_place_card_check, a3_place_days,
  a3_place_locate, a3_place_dedupe_check (platform) and t2_brand_audit, t10_claims, t9_faq_answer_fit, t9_faq_filter
  (tenant). Stages whose route already ends on the OpenAI API (the judge routes, a3_place_dedupe) have a second domain
  and are not touched; writers and embed stages are not touched.
- **Tenant stages split into 332.** The brief listed them as allowed ("acc2 is AWS"), but root ADR 0009 decision 14
  (Accepted 31/10) says acc2 serves chat models "never for main writer or tenant stages until decision 10 and AA-820 are
  done" (decision 10: tenant stages use only makers named in the AI register and the tenant terms; AA-820 is In
  Progress). Data scope (aws_only) is not the issue; the maker list is. So 331 carries the eight platform stages, and
  332 (same SQL shape) the four tenant ones, held for the tech lead's yes, which should also amend decision 14 (one
  line in the root ADR: "acc2 judge / validate fallback allowed on tenant stages from <date>, makers Amazon / Meta added
  to the AI register").
- **Model per stage: the cheapest acc2 model that meets the stage's output contract.** Nova Lite (0.077 / 0.308 USD per
  1M) takes a system prompt, Converse tool use (the gateway's structured-output path, with the auto-tool retry when a
  forced tool is refused) and up to 10K output tokens (a3_atomize asks 4096, 8192 on its truncation retry), and
  answers plain-text JSON for the text-parsed stages. Kimi K3 (reasoning, 3.30 / 16.50) and Nova 2 Lite / Maverick
  (dearer) bring nothing for a format contract. **Exception:** a3_place_card_check gets Llama 4 Scout (Meta,
  0.17 / 0.66), because the place card draft and its check must be different makers (S234 / migration 286) and the draft
  falls back to Nova Lite. a3_place_dedupe_check (Nova Lite, Amazon) differs from the dedupe primary's OpenAI family.
  Every pick is non-Anthropic, so the judge rule holds.
- **Fallback LAST, never ahead of acc3.** The tech lead's instruction; the migration appends and its guard compares the
  primary, earlier fallbacks, shadow and scopes with the values read before the UPDATE.
- **Data-driven skip.** 331 only touches a listed stage that still sits in one failure domain (same rule as
  `invariant.check`, in SQL: enabled deployments on enabled LLM connections the stage's scopes allow, aliases
  expanded); a stage an admin already moved, or with no row, is skipped with a NOTICE. Re-running changes nothing.
- **Contract smoke = job kind, not script.** An admin-triggered job (`llm_contract_smoke`, DRY RUN by default) is
  observable in the job runner and needs no ECS exec. It calls `LLMClient.probe` with an explicit acc2
  `VendorDeployment`: `model_tier="<key>@acc2"` cannot be used, the `generate` pin path only dispatches to the Anthropic
  adapter. The request keeps the stage's own prompt, schema, max_tokens and temperature; its stage becomes
  `adhoc_aa1026` (steering: adhoc runs log under `adhoc_<issue>`) and its role the stage's role.
- **Which pieces are "real".** System prompts, schemas, user-prompt builders and parsers are imported from the stage
  modules (atom_extraction, source_facts, places.days / locate / dedupe, faq_filter, faq_answer_fit, claim_rules).
  Place cards: `draft_card` / `check_card` themselves run with a client that forces the model. The brand audit builds
  its user prompt inline in `brand_audit_node` (and the node calls Jev in its pre-audit), so the smoke rebuilds that
  layout around a fixture page with the real system prompt, schema, temperature and the node's parsing + `derive_status`.
  The dedupe input mirrors `pair_input()`'s shape (it needs place rows). t10_claims has no caller yet; its builder and
  `parse_rows` exist and are used.
- **Pass rule per stage** (what the stage's code needs, not just "valid JSON"): brand audit = the four list keys the
  node reads; source facts = at least one normalized day; atomize = an `atoms` list; place card = a dict with
  `display_name` and a facts list; check = `identity_ok` and one verdict per fact (else `check_card` keeps nothing);
  days = one entry per day block; locate = every name answered (null allowed), none malformed; dedupe = a verdict of
  same / different / unsure; claims = a row for every sentence; FAQ fit / filter = their own parsers.

## Changed

- `api/migrations/331_llm_judge_validate_acc2_fallback.sql` (PR 1), `332_llm_tenant_judge_validate_acc2_fallback.sql`
  (PR 3, held).
- `services/jobs/llm_contract_smoke_job.py`, `services/jobs/__init__.py`, `api/routers/admin_job_runner.py`
  (`POST /admin/job-runner/llm-contract-smoke/run`), `api/core/staff_authz.py` (ai.admin), `docs/api/openapi.json`
  (regenerated with the pinned fastapi 0.161.1 / pydantic 2.14.1), `services/acp_shared/place_cards.py` (optional
  `client`; default path byte-identical: `LLMClient()` when None).
- Tests: `tests/integration/test_aa1026_migration_331.py`, `tests/unit/test_aa1026_acc2_fallback_routes.py`,
  `tests/unit/snapshots/aa1026_routes_after_331.json`, `tests/unit/test_aa1026_contract_smoke.py`,
  `tests/unit/test_aa737_worker_throughput.py` (background kind list), and for 332
  `tests/integration/test_aa1026_migration_332.py`.
- Older integration tests that re-apply migration 324 (`test_aa1017_migration_324.py`, `_320_321.py`,
  `test_aa1002_migration_304.py`) now take the acc2 candidates off the routes around the re-run
  (`tests/integration/_acc2_routes.py`): 324's guard ("no stage routes to a candidate") describes the state before any
  route change and refuses a re-run once 331 is in the replay. The replay expectations of 286 (place card routes) and
  259 (rows naming the Converse keys) include the 331 / 332 rows. Full integration suite on the 332 branch, local
  Postgres 16: 2031 passed; the only errors are `test_007_rls_isolation.py` (needs the CI `aa_cis_admin` password),
  and `test_aa1018_p2_db.py::test_cis_app_writes_access_state_and_the_probe_stamp` fails on origin/main too when run
  after the 322 / 311 modules in a partial batch (order dependence, not this change; it passes in the full run).
- `SAFE_DEFAULTS` (role_config.py) not changed: without the DB the catalog is unreadable and a catalog key like
  `nova-lite` is skipped anyway (lesson S222 #617), so adding it there would change nothing.

## Tradeoffs

- **Judge calibration not done.** ADR 0009 decision 12's ladder asks for judge calibration (40 labelled items) and an
  A/B before an acc2 judge route; the tech lead's yes covers the fallback position now. A brand audit answered by Nova
  Lite is a fallback verdict only while acc3 refuses; it still fails closed to `manual_check` on an unusable reply.
- **Nova Lite for coordinates.** a3_place_locate asks the model for coordinates; a small model knows fewer places, but
  every proposed point must be confirmed by the geocoder (AA-997), so a weak answer costs a re-try, not a wrong pin.
- **The brand-audit user prompt is a rebuild,** not the node's string: running the node would call Jev (paid) and need
  a full graph state. The system prompt and schema (what the format contract depends on) are the real ones.
- **No env switch on the live smoke.** Its cost is a cent; the guard is ai.admin + an explicit `dry_run: false` + a
  0.10 USD estimate cap + a spent cap between stages.

## Should know

- **Numbering:** 330 belongs to AA-1031 (`330_staff_crm_grant.sql`). The CIS preflight refuses 331 until 330 is on main;
  merge AA-1031 first, or label PR 1 `migration:out-of-order`. 332 needs 331 on main first.
- **Order after merge:** Infra bot copy of 331 → DB Migrate check / apply (job tables idle) → `GET
  /admin/llm-availability` (`invariant.breaches` down by eight; the four tenant stages still listed) → deploy of PR 2 →
  smoke DRY RUN → smoke live (all twelve stages; the tenant four run on their planned model before 332, which is what
  332's decision needs) → bot/db-schema-reference PR → root route page recapture.
- **If a stage fails the smoke:** re-point its last fallback with `PATCH /admin/llm-config/{stage}/route` to the next
  model (llama4-scout, then llama4-maverick; never kimi-k3 for a format contract), and keep the place-card pair on two
  makers. No new migration needed.
- The smoke counts on the acc2 deployment circuit (it is a probe): a refused call can open it like the daily probe.
- Not done here (follow-ups): ADR 0009 decision 14 also says the OpenAI API is the judges' warm path "ahead of acc2";
  the judge routes that end on the OpenAI API have no acc2 entry yet, so with the OpenAI API unfunded they stay in one
  usable domain. Adding acc2 after the OpenAI API needs an order decision (S235 "OpenAI API last") from the tech lead.
