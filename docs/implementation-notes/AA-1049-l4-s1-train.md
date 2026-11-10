# AA-1049 — Quality W3, S1 train (lane l4, S276)

Scope: the $0 parts of the S1 control-plane rewire, per the tech lead's S275 decision "build-zero-cost" (the issue stays
open; the S1 enforce switch of the final contract moves to AA-1048). Plan: root
`docs/audits/2026-11-06-S271-writing-quality-audit/data/design-review.json` (workflow `s1` `pick.phases` P2/P3 and
`arch.roadmap[3]` item "S1: ..."), root ADR 0020, D8 / D9. Follows the T9 and T2 trains: one opt-in switch whose
default is main byte for byte, the shared route table, a scripted full-chain parity golden recorded on main, a dry
store and a declared-diff replay.

## Decisions

- **One switch, opt-in, worker only.** `S1_CONTROL` (`s1_train.CONTROL_ENV`, in `config_fp.MODE_FLAGS["a1"]`). Only the
  exact value `contract` (case and spaces ignored) turns the train on; unset, `graph` or any other value is main's
  path, and `config_fp` records the switch only as `contract` (`_s1_control_value`, like `T2_ORCHESTRATOR`). The reader
  goes through `trial_flags.get` like the T9 / T2 switches, but `trial_flags.ALLOWED` gets no S1 entry: an S1 job runs
  for the AA master tenant, never an audit tenant, so a per-job trial would always be refused. A trial is the worker
  env (Infra nullable variable, same pattern as `t2_orchestrator`), after the tech lead's go.
- **Scope = an A1 draft of the S1 writer v2** (`s1_writer_v2_live.applies` for the writer call, `is_v2` after it).
  A tenant (T2) rewrite, the Bedrock Batch seed and a legacy v1 draft never read the switch.
- **(a) Code composes the contract fields; the prompt stays byte-identical.** `s1_writer_v2_live.write` calls
  `s1_train.compose_page` after the meta call: one day block per planned day in plan order with the `Day N — Title`
  heading numbered by code (an extra or duplicate writer day is dropped, a range object keeps its range heading when
  all its days are planned), the writer's own fact line removed (`strip_writer_fact_lines`: a line made only of
  `Label: value` clauses, or such a run at the end of a prose line; prose is never touched) and the source day's
  verbatim fact lines appended (`s1_writer_v2.source_fact_lines` minus DO NOT USE, the arm-E helpers) when the plan has
  source day blocks or a single day; the SEO title fitted at a word boundary (`fit_seo_title`); a meta still out of
  the policy band after the writer's meta call and its one retry replaced by `meta_template` (name, source duration
  and country, a subset of the page's own highlights in page order; accepted only when `meta_problems` passes). Rule 7
  of the writer prompt still asks for a fact line and code replaces it: a prompt change is a paid A/B (W4/W5), and a
  byte-identical prompt keeps every stored writer output a valid replay cassette.
- **A day the writer dropped stays missing.** Code numbers and orders the days but never fills a missing day's
  narrative: validate's day check still fires `ITINERARY_DAY_COUNT_MISMATCH` (hard on v2), so the class router can
  redraft. Filling it with source text would publish a day nobody wrote, more permissive than main.
- **"Slug at a word boundary" is the SEO title on S1.** An S1 Master has no slug column
  (`silver_aa_internal.generated_content`); the T9 slug builder already exists (`seo_builder`, `T9_SEO_BUILDER`). The
  S1 composition fits `seo_title` (varchar 70, policy max 60) at a word boundary instead.
- **(b) S1 rows in the shared route table.** `route_table.yaml` section `s1` (version `s1-route-table-v1`, default
  `hold`): exactly one row per S1 code of the taxonomy (`taxonomy.yaml` `a1_codes`, 60 codes), actions
  repair_sentence / cut / regenerate_section / rewrite / hold. Lint: `route_table.s1_coverage_errors` (no missing, no
  extra, no doubled row, every code with a taxonomy class) and `s1_schema_errors`; `rewrite` only on day-structure and
  truncation classes. The publish gate does not read the table, so no row can publish a tour. The route-table change
  is recorded in `change_control.lock.json` with `parity:tests/unit/test_aa1049_s1_train.py`.
- **(c) local_fix replaces flag_fix (and its `_rerepair_meta` x3 loop) on the train's path.** `graph.fix_node` is the
  `flag_fix` node of every graph: main's `flag_fix_node`, or `s1_train.local_fix_node` when the train is active. No
  model call: `regenerate_section` rows run the forbidden strip, the title fit, the meta fit and then the meta
  template; `cut` rows delete an itinerary prose sentence whose meal / clock claim the source day does not back
  (`source_backed_meal_line`, the source clock times) while its day keeps another prose sentence (headings and fact
  lines are never cut), and an "Optional ..." highlight while 3+ remain. Then the repair contract over the edit
  (`graph._contract_after_edit`, a breach reverts the field) and the detectors that fired are re-run: the brand
  pre-audit here, validate and grounding in revalidate. Whatever is left is `manual_check` (review), never "fixed";
  there is no second repair round (`graph._grounding_recheck` skips its sentence re-repair while the train is
  active). The grounding node's own source-aware sentence repair (one `s1_flag_fix` call, repair_sentence rows) stays:
  it already sees the per-unit source and re-checks.
- **(d) the judge stays off.** `judge_node.s1_judge_off_live_path` is true for every A1 v2 draft while the train is
  active, so `S1_JUDGE_LIVE=on` (the break glass) does not bring the call back there, and the brand audit takes the
  deterministic pre-audit (no `s1_brand_audit` LLM call). Off: unchanged.
- **(e) the class router.** `graph.should_retry` asks `s1_train.route_after_judge` when the train is active: an
  enforced code (`hard_block_codes`) whose row is `rewrite` redrafts once (2 drafts in total, `MAX_DRAFTS`), then the
  hitl exit (the shared checks tail); without one, the score decides as on main (saturated: `done`).
- **Observability.** The train's report (`state["s1_train"]`: compose, local_fix routes / edits / left) is passed
  through `_rewrite_tour` and stored as `generated_content.metadata.s1_train` (absent when the switch is off).
- **(e) dry store / replay.** `scripts/quality/replay_aa1049_s1.py` drives the production `_rewrite_tour` over stored
  S1 v2 Masters with the stored page as the writer cassette and the stored meta as the meta cassette; any other model
  call (flag_fix, the grounding repair, judge, LLM brand audit, nudge) is a counted MISS, Jev answers skipped, nothing
  is written (the outcome is what the store / publish steps read: `_is_publishable`, codes, statuses, the final
  contract, the sha of the stored text). Declared ids: D-s1-compose, D-s1-calls, D-s1-route, D-s1-codes, D-s1-status
  (stricter only). Never declared: a looser outcome (held -> publishable, a contract verdict that becomes publish) on
  a row the base met no miss on; a branch that makes more non-writer calls or more than 2 drafts.
  `scripts/quality/export_aa1049_s1.py` (ecsx, read-only transaction) exports the newest v2 Master per tour of the
  master tenant (not hand-edited, last DAYS days) with its raw_tours source as the job builds it and the master
  tenant's forbidden words; `scripts/quality/replay_aa1049_s1_local.sh` decodes it and runs base / repeat / on.

## Changed

- New: `services/content_generation/s1_train.py`; `scripts/quality/replay_aa1049_s1.py`, `export_aa1049_s1.py`,
  `replay_aa1049_s1_local.sh` (force-added: `scripts/` is gitignored, like the T9 / T2 replays).
- `services/content_generation/s1_writer_v2_live.py` (the compose call, switch only), `graph.py` (`fix_node`, the
  router hook in `should_retry`, no second grounding repair round on the train, the compose report in state),
  `judge_node.py` (`s1_judge_off_live_path`), `services/acp_content_writing/route_table.py|yaml` (`s1` section),
  `services/writing_memory/config_fp.py` (MODE_FLAGS a1 + normaliser), `api/routers/v1_pipeline.py` (pass `s1_train`
  through), `api/routers/admin_pipeline.py` (`metadata.s1_train`), `services/quality/change_control.lock.json`.
- Tests: `tests/unit/test_aa1049_s1_train.py`; harness `tests/unit/fixtures/aa1049_s1_harness.py` and its golden
  `aa1049_s1_parity_golden.json`, RECORDED ON MAIN (5cc016cd) in the first commit of the branch, before any code of the
  train; the replay base `aa1049_s1_replay_main_base.json` recorded by running the replay on a `git archive` export of
  main (5cc016cd); `tests/unit/quality/test_aa1049_route_table.py` (S1 rows); `tests/integration/test_aa1049_s1_export_db.py`.

## Parity proof

The harness drives the real `_rewrite_tour` (LangGraph, writer v2, validate, brand audit, grounding, flag_fix,
revalidate, final contract) with scripted answers over 7 scenarios (clean, meal in prose, wrong writer fact line, meta
out of band twice, dropped day, unsupported number, elephant). The trace = every model request (stage + sha16 of the
prompt) and the outcome fields of the result plus the publish gate. Unset and `graph`: equal to the main golden on all
7; 7 unknown values (`""`, `on`, `true`, `1`, `contracts`, `con tract`, `linear`) equal it on 2 scenarios each.

## Switch-on numbers (synthetic, $0)

Harness, switch on vs main: meal in prose and wrong fact line publish with 0 `s1_flag_fix` calls (main: 1 flag_fix
call each); meta out of band publishes with the code template (main: 4 flag_fix / re-repair calls, still held); a
dropped day redrafts once and publishes (main: saturated score, no redraft, held); two dropped drafts go to the hitl
exit; the number keeps its one grounding repair call; elephant is `policy_hold` with no edit. Replay smoke (5 stored
synthetic Masters, base = main 5cc016cd): defaults 0 declared / 0 undeclared / 5 equal, exit 0; switch on + opt-in:
D-s1-compose 3, D-s1-status 1, MISS 2 (main met a flag_fix miss), 0 undeclared, exit 3.

## Tradeoffs

- **Voice / format codes are held, not rewritten.** Main's flag_fix rewrites a flagged field with a source-blind LLM
  call and revalidate marks it "fixed" without re-running the audit. The train has no model rewrite for them: a
  flagged code no deterministic step clears becomes `manual_check`. Stricter (declared D-s1-status); the replay counts
  how often. A source-aware sentence repair for them would be a new repair prompt (W4/W5, paid A/B).
- **No source_gate, no typed review queue, no approve / edit / promote through the contract, no FS-slice grounding
  units, no widened grounding fields.** Each is either an enforce change (AA-1048) or a new paid instrument.
- **The meta template reads list-like** ("<name> in <country>: a, b and c."). It only replaces a meta that is still
  out of band after the writer's two meta calls, where main spends up to 4 repair calls and still often holds.
- **The replay replays the KEPT text** (after main's own repairs), not the writer's raw reply: rows main repaired show
  their repair call as a MISS on the base side.

## Should know

- The golden pins prompt shas and the outcome of the A1 path: a later PR that changes the A1 path on purpose re-records
  it on its own commit (`python -m tests.unit.fixtures.aa1049_s1_harness tests/unit/fixtures/aa1049_s1_parity_golden.json`),
  never to make the test pass.
- Found while building the harness (main, not changed here): `validate_node` crashes on a writer reply whose
  `subtitle` is JSON null (`generated.get("subtitle", "").lower()`), and the rewrite fails as a whole.
- Live steps after merge: no switch flip, no paid smoke. The free real-data replay: export with ecsx, then the local
  runner (see the PR body).
