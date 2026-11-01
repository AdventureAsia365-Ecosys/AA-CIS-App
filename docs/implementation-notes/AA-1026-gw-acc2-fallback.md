# AA-1026 slice I6 — acc2 fallback for judge / validate stages (lane gw-acc2-fallback, S263)

Tech lead approval S263 (01/11): "thêm đường dự phòng acc2". Three CIS PRs, in merge order (changed in the review
fix pass: the code PR now goes FIRST, so the contract smoke runs before any route changes):

1. `feat/aa-1026-contract-smoke`: the contract smoke job + admin route (with a per-stage model override), the brand
   audit's hold of an acc2 `pass`, an optional `client` on the place card calls, CONTEXT.md, these notes. Works with
   or without 331 (it reads the route, else uses the planned model). Merge, deploy, then smoke DRY RUN and live.
2. `feat/aa-1026-acc2-judge-fallback`: migration 331 (contract) + its integration test + a unit test that walks the
   post-331 routes through the gateway. Migration only, no readiness raise (MIG-4). Its order gate refuses a stage
   whose latest contract smoke on the pick did not pass, so it can only be applied after step 1's live smoke.
3. `feat/aa-1026-tenant-acc2-fallback` (held): migration 332, the same fallback (and the same order gate) for the four
   tenant judge / validate stages. Merge only after the tech lead says yes to the ADR 0009 decision 14 question below.

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

- **Review fix pass (two major findings).** (1) *An uncalibrated judge must not publish.* `s1_brand_audit` is a judge
  whose `pass` publishes a tour, and Nova Lite has no decision 12 calibration. Kept the route (option b of the review):
  `brand_audit_node.hold_uncalibrated_pass` turns a `pass` answered on a connection in
  `UNCALIBRATED_JUDGE_CONNECTIONS` (`{"acc2"}`) into `manual_check` + `BRAND_AUDIT_UNCALIBRATED_JUDGE` (taxonomy:
  `other.degraded_detector`) with a reviewer note; `flagged` / `manual_check` from acc2 stand. So while acc3 refuses,
  every brand audit still runs and can flag, and a clean one goes to the Review Queue instead of publishing. The test
  is the reply's `connection_key` (the gateway sets it to the task-role connection on the Converse path), not
  `fallback_used`: an admin could make acc2 the primary and the verdict would be no more calibrated. It covers
  `t2_brand_audit` too (same node, 332). Dropped option (a) (take s1_brand_audit out of 331): it leaves the brand audit
  with no second domain, which is exactly the breach the slice closes. (2) *Smoke before apply.* The planned order
  applied 331 first; now the code PR merges and deploys first, the smoke runs (DRY RUN, then live on the planned
  models), and only then 331. The order is a guard, not a note: 331 / 332 refuse a stage they would change unless the
  latest `shared.llm_call_log` row with `stage = 'adhoc_aa1026'`, `quality_signal->>'contract_smoke' = <stage>`,
  `model = <pick>` and connection acc2 has `parser_ok = true` (a later failed run cancels an earlier pass). A
  database that never logged a call (CI / local replay) skips the gate with a NOTICE; Dev has hundreds of thousands
  of rows. To re-pick a failing stage before the apply, the smoke takes `models: {stage: key}`.
- **Ladder rungs skipped knowingly.** The tech lead's S263 yes puts the seven validate stages live on the format smoke
  alone (no calibration, A/B or shadow rung); the brand audit judge goes live only with its `pass` held. Recorded in
  331's header and in the root ADR 0009 decision 14.
- **Migration 333 — "AWS first, outside last" (tech lead, S263).** Every judge / validate route that ends on the
  OpenAI API hop (`gpt-6-luna-openai`, 327) gets an acc2 pick right BEFORE that hop; the OpenAI API stays the last
  rung. After 331 / 332 no route has an acc2 hop after the OpenAI API hop, and eight have none: `s1_judge`, `t2_judge`,
  `t7_debate_judge`, `t10_judge`, `writing_memory_eval`, `writing_memory_mine` (Nova Lite), `a3_place_dedupe` (Llama 4
  Scout: its check stage falls back to Nova Lite, two makers) from the Dev route snapshot, and `t10_adjudicate` (Nova
  Lite), which the 29/10 snapshot predates (migration 308 seeds it ending on the OpenAI API; the S260 Dev evidence lists
  it with `vendor_direct`). The file is generic over the listed stages: it finds the first OpenAI API hop (a key whose
  deployments are all on kind `openai` vendor_direct connections), requires the route to END on such hops, removes the
  pick wherever it is and inserts it right before that hop (so an acc2 hop an admin put after the OpenAI API moves
  too). Same guards as 331 / 332 plus "only the pick moved"; same order gate. Its smoke cases ship in the code PR
  "stage parsers read the first complete JSON value" (deployed first).

## Changed

- `api/migrations/331_llm_judge_validate_acc2_fallback.sql` (PR 2), `332_llm_tenant_judge_validate_acc2_fallback.sql`
  (PR 3, held); both carry the smoke order gate.
- `services/content_generation/brand_audit_node.py` (`hold_uncalibrated_pass`, `UNCALIBRATED_JUDGE_CONNECTIONS`),
  `services/quality/taxonomy.yaml` (`BRAND_AUDIT_UNCALIBRATED_JUDGE`), tests in `tests/test_brand_audit_node.py`.
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
  A/B before an acc2 judge route; the tech lead's yes covers the fallback position now. So a brand audit answered by
  Nova Lite can flag or hold, never pass: its `pass` becomes `manual_check` (more Review Queue work while acc3 refuses,
  in exchange for no tour published on an uncalibrated verdict). Calibrating Nova Lite (40 items) and removing `acc2`
  from `UNCALIBRATED_JUDGE_CONNECTIONS` lifts it.
- **Order gate reads the ledger.** A migration that depends on `shared.llm_call_log` rows is unusual; the alternative
  (a note in the runbook) is what the review rejected. The empty-ledger exemption keeps CI / local replays working;
  the integration tests exercise both sides.
- **Nova Lite for coordinates.** a3_place_locate asks the model for coordinates; a small model knows fewer places, but
  every proposed point must be confirmed by the geocoder (AA-997), so a weak answer costs a re-try, not a wrong pin.
- **The brand-audit user prompt is a rebuild,** not the node's string: running the node would call Jev (paid) and need
  a full graph state. The system prompt and schema (what the format contract depends on) are the real ones.
- **No env switch on the live smoke.** Its cost is a cent; the guard is ai.admin + an explicit `dry_run: false` + a
  0.10 USD estimate cap + a spent cap between stages.

- **333 changes judges with no code hold.** Unlike the brand audits (an acc2 `pass` is held as manual_check), the
  brand fit judges (`s1_judge` / `t2_judge`: part of the quality score), `t7_debate_judge`, the F9 judge `t10_judge`
  (blocking side: CTA / fact / integrity), `t10_adjudicate` (off by default) and the writing memory judges answer on
  Nova Lite while acc3 refuses, instead of GPT-6 Luna on the OpenAI API, with no calibration. That is the tech lead's
  call; the revert is one admin route PATCH per stage (drop the acc2 hop; the OpenAI API hop is still there).

## Should know

- **Numbering:** 330 belongs to AA-1031 (`330_staff_crm_grant.sql`). The CIS preflight refuses 331 until 330 is on main;
  merge AA-1031 first, or label PR 1 `migration:out-of-order`. 332 needs 331 on main first.
- **Order after merge:** PR 1 (smoke + brand audit hold) merge → deploy api + worker → smoke DRY RUN → smoke live
  (all twelve stages; the tenant four run on their planned model before 332, which is what 332's decision needs) →
  PR 2 (331) merge → Infra bot copy → DB Migrate check / apply (job tables idle; the order gate passes only with the
  smoke rows) → `GET /admin/llm-availability` (`invariant.breaches` down by eight; the four tenant stages still
  listed) → bot/db-schema-reference PR → root route page recapture.
- **If a stage fails the smoke before the apply:** smoke the next model for it (`models: {"<stage>":
  "llama4-scout"}`, then llama4-maverick; never kimi-k3 for a format contract; keep the place-card pair on two makers)
  and change that stage's pick in 331 (and its snapshot / tests) before merging PR 2, or drop the stage from 331.
  After the apply, re-point with `PATCH /admin/llm-config/{stage}/route` (no new migration).
- The smoke counts on the acc2 deployment circuit (it is a probe): a refused call can open it like the daily probe.
- ADR 0009 decision 14 says the OpenAI API is the judges' warm path "ahead of acc2"; the tech lead's S263 order
  ("AWS first, outside last") reverses that for the judge / validate routes: migration 333 puts acc2 before the
  OpenAI API hop. The root ADR text needs the matching amendment (root repo, not this PR).
- 333 apply order: the code PR deployed, the smoke DRY RUN then live for the eight 333 stages (each must pass on its
  planned pick: `model_source` is `planned` because their routes name no acc2 model yet), then 331 -> 332 -> 333 (333
  refuses without 332 registered). After the apply, `GET /admin/llm-availability`, and a smoke re-run of the eight
  shows `model_source: route` with the same picks.
