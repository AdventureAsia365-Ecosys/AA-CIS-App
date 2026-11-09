# AA-900 — lane l18 (S271): W0 config fingerprint, R1 freeze, L0 warn-only, answered-model stamps

Root ADR 0020 roadmap W0 ("Widen config_fp"), S271 critic fixes F4 ("freeze writing-memory mining until R1 exists")
and "Change control". No migration, no model call, no output change except the declared L0 warn-only switch.

## Decisions

- **Fingerprint shape.** `config_fp` sits next to the legacy keys of `writing_memory_release.fingerprint` (and of the
  R2 / R0 run fingerprints, same function): `{v: 2, sha, parts: {component: sha16}, stage_shas, mode_flags_set,
  route_config_version}`. One sha16 per component keeps the stored JSON well under the 8 KiB CIS 287 limit and lets
  the admin see which component and which stage moved.
- **Path = stage prefixes, not a hand list.** a1 `s1_`; t2 `t2_`, `t3_`; t9 `t8_`, `t9_`, `t10_`; plus the eval judge
  `writing_memory_eval` for every writer. A new repair or check stage joins the fingerprint without a code change; an
  unrelated stage (a3_*, t7_*, supply_*) never moves it.
- **Components.** routes (every role-config column that changes who answers, fallback order kept, shadow columns out;
  plus `llm_route_policy.hold_fallback_writes`), stage_params (all `stage_model_params` columns), catalog (catalog +
  deployment rows of every model the routes name; probe state out), route_control (order_mode only), mode_flags,
  prompts (deployed source sha of each manifest stage on the path, `prompt_registry.prompt_hash_of`), memory_items
  (for a tenant release: the active platform release's items_sha256), l0_lexicon (active lexicon rules), brand_config
  (AA master house style for A1, the tenant's for a tenant release).
- **Reverify reasons without a migration.** The CIS 287 CHECK allows four reasons; components map onto them
  (`config_fp.REVERIFY_REASON`): routes / params / catalog / route control / mode flags -> `model_route`; prompts /
  memory items / L0 / house style -> `prompt_version_mismatch`. The detail (components, stages) goes into the audit
  row, the scan result and `GET /admin/writing-memory/releases/{id}.config_drift`.
- **Pre-W0 releases are not flagged.** A stored fingerprint without `config_fp` is not a change (same rule as the
  legacy keys), so the deploy does not flag every active release at once; the next draft records the wide one.
- **Mode flags.** Explicit per-writer list read from the code plus the gateway and memory switches and the seed-align
  per-class prefix families. Values are normalised and hashed, only the names are stored. A guard test fails when a
  `*_ENV = "..."` constant in a writer module is not classified (output-changing or `NOT_OUTPUT_FLAGS`).
- **Mode flags are the worker's (fix pass).** The api and worker task definitions carry different env (the worker
  alone has T2_JUDGE_GATE, T2_JUDGE_ANCHORS, T3_BACKGROUND_FACTS, T9_FAQ_LLM_FILTER, S1_WRITER_V2), and releases are
  drafted in the api. Each config_fp names its `service` (`config_fp.set_service`, called by the worker's `_main`
  and by the api's in-process worker in local dev; default "api"). mode_flags is compared only on a worker scan,
  against the release's own fingerprint when the worker recorded it, else against the release's worker-run evidence
  (R2, then shadow, then R0 run fingerprints, read in `releases._evidence_fingerprints`). An api scan and the admin
  drawer never compare it (`config_drift.mode_flags_compared` says so). Chosen over a DB row of worker switches
  (would need a contract migration on `shared.job_worker`, MIG-4: two PRs) and over copying the switches onto the api
  task (Infra drift risk). No migration.
- **Unavailable is never a change (fix pass).** A component that is "unavailable" on either side is skipped; a
  component whose input depends on a failed read is unavailable too (catalog when routes failed, routes when the
  route policy failed, prompts when any stage's source could not be read); `changed_stages` names nothing then.
- **Scans after the actions themselves (fix pass).** `releases.activate` / `rollback` / `retire_without_previous`
  run `reverify_scan_in_tx` (savepoint, never raises) at the end of their transaction; the L0 activate / rollback
  endpoints, the staff brand endpoints (`/brands*`, `/admin/tenants/{id}/brand`) and `PATCH /settings/seo` run
  `reverify_scan_safe` after their save; the staff branch of `POST /brand-identity` scans on its own connection
  inside the save transaction (AA-859 keeps MAIN acquired once). The tenant branch stays off MAIN: the worker's new
  hourly tick (`Worker._reverify_scan`, REVERIFY_EVERY_S = 3600, bounded 60 s) catches it, and also a DB Migrate
  change of a route / params / prompt row applied without a deploy.
- **R1 record** = a finished `prompt_eval_runs` row, pipeline `writing_memory`, run_kind `r1`, verdict `pass`, for the
  writer (existing CIS 284 vocabulary, no new table). Read failures fail closed (frozen).
- **What the R1 freeze covers.** Mining (refused at $0 as the first reason; a run drafts only for writers with a
  record), the preference-summary drafter (same stage), first activation of a release with content
  (`releases.check`, so the drawer shows it before the click) and L0 lexicon activation. Not frozen: rollback,
  retire, memory switch, empty releases (release 0, rollback drill), R0, R2 / shadow evidence runs.
- **Break-glass switches** (env, api + worker): `WRITING_MEMORY_R1_GATE=off` (freeze off), `WRITING_MEMORY_L0_ENFORCE=on`
  (L0 blocks again).
- **L0 warn-only mechanism.** The prompt keeps the learned phrases (no prompt change); the gates read
  `gating_forbidden(pack)` = the pack's forbidden words minus the phrases `apply_to_pack` added. A learned hit becomes
  an `F2_style_warn` finding (an existing warn-only gate: no registry or taxonomy change). A learned phrase that is
  also a platform-truth / stock-superlative word stays on its own layer (judged once); one the tenant listed itself
  keeps blocking. R0 replays run the gates inside `l0.measuring_as_blocking()` so R0's counts do not depend on the
  live mode.
- **Answered-model stamps.** T9: the finalize step already read the final writer row for the AI mark; it now reads
  one jsonb (`ai_marking.WRITER_ANSWER_SQL`: model, fallback_used, route_position, connection_key, effort) and stamps
  `writer_input.writer_answer` (admin-only; the tenant mark keeps the model only). fallback_used is true when the
  ledger says so or the route position is > 0. T2: `generation_meta.writer_answer` from the kept draft's
  `model_used` / `fallback_used`. A1 already writes `generated_content.model_editorial` + `fallback_used`: unchanged.
  #1350's `route_degraded` marks (judge / instrument) are not repeated.

## Changed

- New: `services/writing_memory/config_fp.py`, `services/writing_memory/r1_gate.py`.
- `releases.py`: `current_fingerprint(..., scope, tenant_id)` adds `config_fp`; `reverify_reasons` adds component
  reasons; `config_drift`; the scan passes the release owner and records components / stages; `check()` adds the R1
  freeze for releases with content.
- `mining.py` (`decide_run` / `run_gate` / `run_mine`), `preference.py` (`run_preference`): R1 freeze.
- `l0.py`: `L0_ENFORCE_ENV`, `l0_blocking`, `learned_added`, `gating_forbidden`, `warn_only_phrases`,
  `measuring_as_blocking`. `replay.py`: R0 measures as blocking.
- `acp_content_writing/quality_gates.py`: `gating_forbidden`, `learned_hits`; lexicon, platform-truth and signature
  layers read the gating list. `sentence_repair.check_replacement` likewise.
- `ai_marking.py` (`WRITER_ANSWER_SQL`, `writer_answer`, `ANSWER_KEY`), `service._finalize_piece`.
- `insertion.py` (`writer_answer`, `generation_meta(answer=)`), `jobs/t2_rewrite_job.py`.
- Routers: `admin_writing_memory` (L0 R1 check on GET / POST candidate, preference 409, `config_drift` on GET
  release); `admin_llm_ops` (`_wm_reverify` on any path stage; also after the stage switch, a catalog patch and a
  connection change); `admin_llm_route_control` (scan after save / preset / revert).
- Tests: `tests/unit/test_aa900_w0_config_fp.py`, `tests/integration/test_aa900_w0_config_fp_db.py`, the writer
  answer case in `test_aa1040_ai_marking_sql.py`; adjusted `test_aa917_mining`, `test_aa918_l0`,
  `test_aa1040_ai_marking`, `test_aa919_migration_287`. Integration conftest defaults `WRITING_MEMORY_R1_GATE=off`
  for the modules that test release / mining / L0 mechanics; the W0 test turns it on.
- CONTEXT.md: gateway params note, writing memory (R1 freeze, L0 warn-only), Release (config_fp, stamps).

## Tradeoffs

- Mode flags of a release the api drafted are compared against its R2 evidence, not the draft: the R2 run is the
  one that measured the release, so its env is the one that matters. A release whose evidence carries no
  worker-recorded fingerprint (release 0 whose R0 ran from a script or the api, a lexicon-only release whose R0 run
  records none) is not compared on mode_flags; a rules / repair-hint release always has a worker R2 run.
- The hourly worker scan costs ~12 reads per active release ($0); a tenant brand save is caught within an hour, not
  at once (AA-859 keeps the tenant path off the admin pool).

- Reason mapping onto four codes loses granularity in the column, kept in the audit row / API instead: avoids a
  contract migration (MIG-4) in W0.
- The route-control version id is recorded but not hashed: every save of any stage bumps it, which would flag
  releases for unrelated stages (the requirement "an unrelated stage does not move it" wins); the rows are hashed.
- Prefix-based paths may include a stage a writer never calls (t9_faq_* for t9_social): a change there flags a
  release that did not need it. Conservative on purpose.
- Brand pack / SEO of each tenant is not part of a PLATFORM T2 / T9 release (they are per-request inputs, not one
  config); the Facts Entries snapshot is per object, not config. Both left out.
- The per-request `writer_answer` read reuses the AI-mark ledger query (one read, not two).

## Should know

- Fix pass: four switches of the sibling W0 lanes are classified here ahead of their PRs (S1_JUDGE_LIVE -> a1,
  T2_INTERIM -> t2, T10_F9_POLICY_MODE and T9_LAST_ATTEMPT_MODE -> t9_blog / t9_social), so the env-switch guard
  stays green whichever of #1350 / the t2s1 / t9 PRs merges first. The cassette lane's `LLM_CASSETTE`
  (`shared/llm_client/cassette.py`, record-only) is pre-classified as not output-changing. A later lane that adds a `*_ENV` constant in a
  writer module must classify it in `config_fp.py` (the guard test names it).
- Fix pass: `reverify_scan` results and audit rows carry `mode_flags_compared` next to `components` / `stages`.

- PR #1352 (fix/aa900-w0-eval-interim) edits `r2.py`, `eval_judge.py`, `config.py`, `writing_memory_job.py`: this lane
  touches none of them. `r2.py`'s `fingerprint()` calls `releases.current_fingerprint(conn, writer)` and gets the
  wide fingerprint automatically (platform owner). PR #1350 edits `t2_rewrite_job.py` (a different hunk, ~line 324)
  and `faq_filter.py` (not touched here). Expect no textual conflict.
- After deploy every active release keeps its pre-W0 fingerprint (no config_fp): nothing flags until a new draft.
- With no R1 run on Dev today, the summary shows mining frozen ("no writer has a passing R1 run yet"), and a
  rules / L0 activation is refused with `r1_missing`. That is the intended W0 state.
- Pre-existing, not from this lane: `tests/integration/test_aa1042_route_control_db.py::test_preset_puts_the_anthropic
  _api_first_for_claude_models` fails on main too when run after `test_aa1002_migration_304.py` +
  `test_aa1042_guide_db.py` + `test_aa1042_migration_370.py` in one process (passes alone).
- Not done here (later waves): requiring an eval run id / break-glass reason on llm-config / switch / catalog UI edits
  of gated stages (route control already has it), and a UI line for `config_drift` in the Releases drawer.
