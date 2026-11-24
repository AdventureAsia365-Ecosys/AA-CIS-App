# Writing memory

> Detail page of [CONTEXT.md](../../CONTEXT.md): governed writing memory: rules, examples, gates (platform and tenant). Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

### Writing memory (platform + tenant)

What the writers (A1/S1, T2, T9 blog, T9 social) learn from, under release control. Model weights
never change. See ADR 0010. The signal store `shared.writing_signal` exists since CIS 277 (AA-916, WM-03) and the
rule store `shared.writing_rule` since CIS 280 (AA-917, WM-09), the golden set and the eval run ledger since CIS 284
(AA-919, WM-17), the releases, the shadow outputs and the per-writer memory switch since CIS 287 (AA-919, WM-18 /
WM-20), and the pinned exemplars since CIS 313 (AA-921, WM-27).

**Writing Signal**:
One recorded piece of evidence about a writer's output: an admin sentence edit (before / after,
gate, problem), a tenant edit, or an approve / reject / regenerate, with its reason. Captured
silently; the tenant portal asks no feedback question (D7). Tenant signals live only in deletable
tables. Stored in `shared.writing_signal` (CIS 277): one row per signal, `scope` platform (A1, `tenant_id` NULL) or
tenant (T2 / T9, `tenant_id` set); RLS ENABLE + FORCE with `tenant_isolation` for `aa_app_user` (own tenant rows,
SELECT + INSERT only) and the X1 parity policies for `cis_app` / `cis_owner`, so every `cis_app` read filters
`tenant_id` explicitly. Tenant rows are deleted by the AA-839 tenant erasure (`services/erasure/inventory.py`);
`acp_shared.audit_log` keeps only the signal id and the sha256 hashes.
Captured by `services/writing_memory/signals.py` `record_writer_feedback` (AA-916) on: the tenant's T9 hand edit
(PATCH /v1/content-writing/pieces/{id}, best-effort, on the tenant pool), the admin A1 field edit (PATCH
/admin/tours/{tour_id}/generated/{content_id}, one signal per changed field), the admin sentence cut / rewrite / edit
of a T9 piece or a T2 version (with an optional reason code; `fact_not_in_source` on a T2 sentence also labels Jev
`a1_claim_supported` truth=false), and reject / dismiss / regenerate of a review row (reason-only, no text). The admin
history reads the text back from the signal (legacy audit rows keep their own text, D16). Reason codes and the chip
preselected for a gate: `services/writing_memory/reason_codes.py`. Every discarded T9 attempt is kept in
`content_piece.discarded_attempts`, and `writer_input.writer_prompt_version` stamps the T9 system prompt.
_Avoid_: lesson, feedback (alone), training data.

**Writing Rule**:
One learned constraint for a writer, of one type: `lexicon` (ban or replace a word or phrase),
`writer_instruction` (a line in the writer prompt), `repair_hint` (guidance for the repair pass)
or `preference_summary` (a tenant's ≤ 5-sentence voice summary; AA-921 WM-28, `services/writing_memory/preference.py`:
drafted on the judge route from that tenant's pass signals only, linted like a rule plus `unsourced_claim` — a promise
word, a specific or an add-verb quarantines it, stored `rejected` with `quarantine: true` — and released like any tenant
rule; the drafting is the `writing_memory_mine` job with `mode: preference_summary`, off unless the worker sets
`WRITING_MEMORY_PREF_SUMMARY`, else `needs_llm_path` and no call). A rule may only cut, ban,
rephrase or format; it never adds content, and fact gates (F1, F17, F9_cta_fact, Jev) outrank it.
Stored in `shared.writing_rule` (CIS 280): `scope` platform / tenant with the same CHECK and RLS shape as the signal
store (FORCE, `tenant_isolation` for `aa_app_user` but no tenant-pool privilege: tenants never see rules); status
`candidate` → `approved` → `active`, or `rejected` / `expired` (`expires_at` 60 days); approved / active need a human
approver (DB CHECK); one rule ≤ 25 words (a preference summary ≤ 5 sentences, tenant only); the approved + active
rules of a writer hold ≤ 400 tokens (platform) or ≤ 250 tokens per tenant (trigger `writing_rule_token_cap`); a
platform rule drawn from tenant evidence needs ≥ 3 tenants (`support_tenants`, D9). Provenance: `signal_ids`, support
counts, `lint`; the key that survives a gate rename is `reason_code` + `gate_class` (the qtax-v1 class). Tenant rules
go with the tenant (AA-839 erasure inventory).
Mined by the `writing_memory_mine` job (AA-917, `services/writing_memory/mining.py`): pass signals are clustered in
code ($0) by label — the reason code, else the gate's qtax class, plus every phrase a human edit removed — and a
cluster over threshold (tenant: ≥ 2 edits on ≥ 2 pieces; platform from tenant edits: ≥ 3 tenants; A1: ≥ 2
reviewers, or a gate failing on ≥ 5% of the last 30 days' A1 pieces; `services/writing_memory/config.py`) gets one
drafter call on stage `writing_memory_mine` (judge route, masked text only, ≤ 50 calls / ≤ $1 a run, ≤ $25 a
calendar month over every writing-memory stage). The draft is linted (`lint.py`: only constrains, a lexicon
replacement adds no specific, no 5-gram of tenant text or tenant brand in a platform rule, PREDICT: explains ≥ 2
held-out edits) and stored `candidate` (or `rejected` by the lint) with its provenance; a rejected draft is clamped
to the 280 limits, stored redacted when it held tenant text, and not redrafted until new signals join its cluster.
Automatic runs are off unless the worker sets `WRITING_MEMORY_AUTO_MINE`; when on: ≥ 10 new pass signals since the
last run, ≤ 2 a month, never during an open rerun wave. A manual run (admin, after a confirm step) is the 3rd. A
failed run that spent money counts as a run.
**R1 freeze** (AA-900 W0, root ADR 0020, `services/writing_memory/r1_gate.py`): until a writer has an R1 record (a
finished `prompt_eval_runs` row, pipeline `writing_memory`, run_kind `r1`, verdict `pass`), mining and the
preference-summary drafter refuse at $0 (`mining.decide_run` first check; a run drafts only for writers with a
record), and a first activation of a release with content or of an L0 lexicon rule is refused with code `r1_missing`
(the Releases drawer and the candidate drawer show why). Rollback, retire, the memory switch, empty releases (release
0, the rollback drill), R0 and R2 / shadow runs are not frozen. Break-glass: `WRITING_MEMORY_R1_GATE=off` (api and
worker); the summary's `mine_trigger.r1` shows the switch and the frozen writers.
Signals reach `pass` through the fact filter (`writing_signal_fact_filter`, `fact_filter.py`, $0, daily from the
api ops sampler while signals are pending): an edit that adds a number, time, price or promise word the text and
the source lack fails (quarantined), an added name is grey (admin confirms), a style edit passes. The backfill
(`writing_signal_backfill`, `backfill.py`) copies the pre-AA-916 admin sentence audit rows, T2 tenant-edit versions,
T9 failed attempts (system signals, evidence for R0) and A1 brand-audit lessons, idempotently. Admin page
`/admin/lab/writing-memory` (was `/admin/writing-memory`; Signals and Candidates tabs, plus a Guide tab:
`frontend/app/admin/lab/writing-memory/guide.ts`
on the kit GuidePanel; `api/routers/admin_writing_memory.py`, admin only). A rule
reaches a writer through a release (AA-919), except an L0 lexicon rule (below).
**R0** (AA-918, `services/writing_memory/replay.py`, job `writing_memory_r0`, $0, no model call): the replay of one
rule over stored T9 texts (final text, discarded attempts, the writer's text before a human edit), gate ledgers,
repair logs and signals — how often it would fire, which stored failures quote its phrase (prevented), the signals it
matches, and whether its cluster recurred ≥ k in 30 days, k being the mining threshold of its basis
(`mining.cluster_threshold`, one function for both). The baseline sanity check re-runs the deterministic T10 gates on
stored texts against their stored ledgers (F9 verdicts passed through: `run_quality_gates(f9_results=…)`). The
result (counts and ids only) is kept in `lint.r0`, tied to the rule's text by `rule_sha`.
**L0 lexicon fast path** (AA-918, `services/writing_memory/l0.py`, ADR 0010 decision 6, D11): an `active` `lexicon`
rule of `t9_blog` / `t9_social` joins the brand pack's forbidden words at T9 `start_write` (prompt "Never use" line +
the F2_lexicon gate, source F2_brand_forbidden; a replacement also gets "Write X instead of Y" on the WORDS line), never
a post-gate substitution; skipped when it is a declared signature word, or in the CTA / brand name / trip name; the
piece's `writer_input.writing_rules` lists what it got. Activation = an R0 on the current text with `l0_ok` (phrase
drops to 0, no new hard stop, a replacement adds no number / place / time / price / service) + one admin click;
rollback = one click (`active` → `rejected`, `rollback: …`). No active rule: the pack and prompt are unchanged.
AA-900 W0 (ADR 0020: a gate holds only with a passing measurement record): the learned phrases are **warn-only** by
default; the prompt still names them, but the gates block on the tenant's own words only (`l0.gating_forbidden`,
`quality_gates.gating_forbidden`) and report a learned phrase as an `F2_style_warn` finding (`learned_hits`; a
platform-truth or stock-superlative word stays on its own layer). `WRITING_MEMORY_L0_ENFORCE=on` restores the blocking
F2_lexicon finding once W2 has measured it. R0 replays measure a rule as blocking whatever the switch
(`l0.measuring_as_blocking`).
_Avoid_: lesson (the frozen AA-132 rows), output rule (the retired N8 table), guideline.

**Pinned Exemplar**:
One fixed, entity-masked example output (`[NAME]`, `[N]`, `[PRICE]`, `[TIME]`, `[URL]`) pinned in a Release and labelled
"voice only, reuse no fact". T2 and T9 get at most two per channel per Release; S1 gets none. No nearest-neighbour
retrieval. Stored since CIS 313 (AA-921, `services/writing_memory/exemplars.py`): the pool `shared.writing_exemplar`
(scope platform / tenant like the rule store, RLS ENABLE + FORCE, no tenant-pool privilege; status candidate / pinned /
never, each change with the admin's reason and an audit row) filled at $0 by the refresh from the "use as example"
signals (WM-07) whose fact filter passed (an approved T9 piece's text, or the edited T2 field, masked, whole sentences up
to 120 words; a "never use" signal excludes its source); `shared.writing_memory_release_exemplar` the links a `rules`
draft of T2 / T9 writes in its own transaction (pinned first, then the newest candidates; trigger
`writing_memory_release_exemplar_guard`: same owner, writer and channel, never a `never` one, never updated). The
examples are part of the release's `items_sha256` (`exemplars.release_sha256`; a release without examples keeps the
rules-only hash), loaded and checked by `insertion.attach_items`, rendered after the tenant rules (T9 user prompt, T2
block; never in the cached T9 `SYSTEM_PROMPT`), tenant first with the platform release's examples as fill, never
another tenant's. A copied 6-word run of an example is a QA trace `exemplar:<id>` (`eval_checks.exemplar_copies`) in R2,
the shadow and the post-activation check. Admin: the Exemplars tab of `/admin/writing-memory` and the Tenant 360 "Voice
memory" card (quality tab).
_Avoid_: few-shot (implies retrieval or unpinned examples), sample, golden item (an AA-729 eval item).

**Release**:
A versioned bundle of Writing Rules and Pinned Exemplars for one writer and Scope (`shared.writing_memory_release`,
CIS 287, AA-919 WM-18; ids only, the rule text stays in `writing_rule`). It passes the gates R0 ($0 replay of stored
text), R1 (re-score old outputs), R2 (golden-set run) and shadow, then becomes `active` only by one human click,
never automatically. Statuses `draft` → `evaluated` → `approved` → `active` → `retired` (+ `rejected`); a `reverify`
flag when the golden set, a model route or the gate taxonomy changed since its evidence (it keeps serving; a re-run
needs approval). Since AA-900 W0 the fingerprint also holds `config_fp` (`services/writing_memory/config_fp.py`): one
sha per component over every route row, `stage_model_params` row, catalog / deployment row and route order mode of the
writer's path (stage prefixes s1_ / t2_ t3_ / t8_ t9_ t10_ plus the eval judge), the output-changing env switches
(names stored, values hashed), the deployed prompt source of each registered path stage, the other active release a
tenant release renders after, the active L0 lexicon rules and the owner's house style (`tenant_brand_rules` +
`tenant_seo_config`: the AA master for A1, the tenant for a tenant release). A changed component maps to
`model_route` or `prompt_version_mismatch` (the CIS 287 vocabulary); the scan's audit row and `GET
/releases/{id}.config_drift` name the components and path stages. The env switches are the worker's (the writers run
there; the api task carries other env): each record names its service, and only a worker scan compares them, against
the release's own fingerprint when the worker drafted it, else against its worker-run R2 / shadow / R0 evidence. A
component a read could not get ("unavailable") on either side is never a change. The scan runs after every llm-config
route, switch, catalog, connection and route-control save of a path stage, after a release activation, rollback or
retirement (same transaction), after an L0 rule activation or rollback and a staff house-style / SEO save, at worker
start, hourly in the worker (a DB Migrate change without a deploy, a tenant's own brand save) and on demand. Each content write the
KPIs read records how its writer answered: T9 `writer_input.writer_answer` (answered model, fallback_used, route
position, connection, effort; admin-only), T2 `generation_meta.writer_answer`, A1 the existing
`generated_content.model_editorial` / `fallback_used` columns. Activation needs its evidence twice: the API (`services/writing_memory/releases.py`
`evidence_problem`, 409 with a code) and the DB (CHECK `writing_memory_release_evidence_check` + trigger
`writing_memory_release_guard`): an `empty` release needs R0 (0 byte diffs over ≥ 20 inputs) + the AA-729 baseline
eval; `lexicon_only` R0 only; `repair_hint_only` / `rules` an R2 pass + a shadow pass. One active release per writer
and owner (the pointer); rollback = the active release → `retired` (`rollback: …`, never re-activated) and the one it
superseded → `active`; no deploy. One regular release per writer and owner per month (the train; `train_closed_month`
on `shared.writing_memory_writer` closes it); lexicon, emergency (an open hard stop) and rollbacks never wait for it.
**Release 0** (`release_no` 0, class `empty`) renders nothing: the writers' prompts stay byte-identical (WM-20,
proved by `scripts/writing_memory/wm20_release0_replay.py` on Dev and `tests/unit/test_aa919_release0_replay.py` in
CI, and by `tests/unit/test_aa920_injection.py` through the DB resolve path).
The writers resolve their view once per write (`services/writing_memory/insertion.resolve`: the platform pointer +
the tenant's, explicit `tenant_id` filter, fail open, nothing when `memory_enabled` is off). The view carries the
release's rule items, loaded and checked once (`load_items`: every rule exists, belongs to the writer and owner, and
the items hash equals the release's `items_sha256`; otherwise the part renders nothing). **One builder** in
`insertion.py` renders every slot (AA-920): A1 HOUSE RULES after the forbidden-words line of the S1 writer v2
(platform rules only, no examples; the v1 / batch path stays frozen); T2 platform then tenant blocks at the end of
the system prompt; T9 platform rules appended to `SYSTEM_PROMPT` (cached across tenants) and tenant rules after the
brand words line, both filtered by the rule's `applies_to` channels; a `repair_hint` rule only in the repair pass of
a failed gate of its `gate_class` (A1 flag-fix, T9 `rewrite_with_feedback`), never in a writing slot. Activation by
writer (`releases.WIRED_CLASSES`): A1 and T9 take `rules` and `repair_hint_only`; T2 takes `rules` (WM-25; a T2
repair hint goes live inside a `rules` release); no writer takes `lexicon_only` (R0-only evidence; lexicon reaches T9
through L0 or inside a `rules` release) — anything else is 409 `content_not_wired`.
Each write records a stamp (ids, numbers and the release fingerprint `items_sha256`, never text): A1
`generated_content.metadata.writing_release` (S1 writer v2 only), T2 `tenant_tour_versions.generation_meta`
(`{writing_release, rule_ids, writer_prompt_version}`, CIS 297, admin-only: `_tenant_projection` hides the column and every
`generation_meta|rule_ids|release|memory` key from tenant reads) plus `shared.job.result.writing_release`, T9
`content_piece.writer_input.writing_release`. **Post-activation check** (AA-920 WM-26,
`services/writing_memory/post_activation.py`, job `writing_memory_post_activation`, $0, queued hourly by the api ops
sampler while a release with content is active; or POST `/admin/writing-memory/releases/{id}/post-activation-check`,
DRY RUN unless `apply`): the writes since the activation (the newest 200 per run) must carry the release's
fingerprint and hold no QA trace (`eval_checks.qa_artefacts`, the rule texts included) and no other tenant's text
(`isolation_probe`); `pass` after 20 carried writes. A violation rolls the pointer back at once (a tenant release
with no previous is retired alone; a platform one without a previous, or any refused rollback, turns the writer's
memory off), writes one audit row (ids, codes) and alerts (admin bell + `ops_alert_email` template
`writing_memory_rollback`). Routes `/admin/writing-memory/releases*` and `/writers*` (admin only).
**R2** (AA-919 WM-19, `services/writing_memory/r2.py`, job `writing_memory_r2`, CLI DRY RUN + `--enqueue`): a
candidate release (or today's writers) against the golden set: the judge calibrated on the frozen labelled items
(every run) and the stored controls re-judged by the current judge (stage `writing_memory_eval`, CIS 291; regenerated
only in a `reverify` run), then probe (<= 3, traps first) -> tune ->
holdout of the affected tiers, each candidate written through the real writer path (`eval_writers.py`; T9 = the first
write only) and checked for a new hard fact class (counts only when the judge's kappa vs the labels >= 0.6, and since
S271 W0 only on a candidate verdict fail against a control verdict pass), a QA
trace and another tenant's text (`eval_checks.py`); fail-only early stop; hard cap <= $10 a run from llm_call_log.
S271 W0 (AA-900): controls are paired on (`sample_id`, `input_sha256`), and release 0 / an empty release whose eval run
is the baseline of an older golden version uses this version's baseline (`details.control_run_stale`); the run is
refused at $0 when a planned control is missing or calibration cannot be measured, and by default
(`require_calibrated`) stops after calibration, before any writer call, when the judge does not count
(`--calibrate-only` always stops there); `--judge-model` / `--judge-connection` pin the judge (identity in
`judge_version` and details; a run with two judge identities is invalid, `details.error = judge_model_mixed`); the
per-item reservation comes from the ledger (median logged candidate cost x 1.3).
**Shadow** (WM-21, `services/writing_memory/shadow.py`, job `writing_memory_shadow`): the candidate on recent real
inputs (20; T2 / Facebook 50; a social release for every channel 50 Facebook + 20 others; repair-hint-only <= 10; a
tenant release + 2 inputs of another tenant; golden tours excluded), QA-trace and
isolation checks, rows in `writing_memory_shadow`, cap <= $7. Every model call of both runs is written to
`shared.llm_call_log` under the eval stage with `quality_signal.eval_run` and tenant NULL (`call_log.bind_eval_stage`);
the run row is what the Releases tab attaches as `eval_run_id` / `shadow_run_id`, accepted only when the run names that
release (`release_id`; no code-train or seeded-fault run: API + CIS 292 trigger).
**Weekly KPIs and soft stops** (AA-922 WM-30, `services/writing_memory/kpi.py`, job `writing_memory_kpi_weekly`, $0,
queued every week by the api ops sampler for the last full ISO week; CIS 314): per writer (overall and per tenant id)
writes, re-flag with its Wilson 95% interval, repeat errors, light-edit share and minutes to fix (T9 labels), tenant
edits per write, reason capture (target 80%), cost per write, the replay hit rate, the rule funnel, the clean-pairs
counter (no export path) and the month's learning-loop spend; a rate with no data is None ("not measured"). For each
active release with content, its writes since activation against the 28 days before: a soft stop (re-flag Wilson lower
bound > 8% on >= 20 writes, light-edit share -10 points, minutes to fix +20%, tenant edits +20% and +5 points, cost
per write +20%; minimum samples apply) takes it out of service through the same path as the post-activation check
(`post_activation.move_pointer`: rollback, else retire alone / memory off; audit `writing_memory.release_soft_stop`, bell,
e-mail) unless a human recorded a keep (`shared.writing_memory_keep`: codes or all, <= 30 days; POST
`/admin/writing-memory/releases/{id}/keep`). Active rules past `expires_at` are re-confirmed for 60 days by a fresh
human pass signal of their cluster, else expire; the P3 project stop rule (two releases without improvement) is
flagged, never acted on. One stored report a week (`shared.writing_memory_kpi_report`, numbers and ids only); a DRY
RUN (`POST /admin/writing-memory/metrics/run`) stores and moves nothing. The **disagreement queue** (WM-31,
`services/writing_memory/disagreements.py`, `shared.writing_memory_disagreement`): up to 10 new items a week where an
R2 control verdict differs from the golden label, or a T9 piece the gates passed was labelled heavy / reject (or a held
one as_is / light); one-click resolution judge_right / human_right / unclear, audited. Admin: the Metrics tab of
`/admin/writing-memory`; trend: `scripts/writing_memory/wm30_kpi_trend.py`. The W2 claim items of the same queue
(source `golden_claim`, AA-1048) are reviewed on the **Claim labels** tab instead (AA-1063,
`services/ground_truth/review.py`, CIS 394): one card per sentence (context, matching source lines, full source, the
three lenses, the newest **agent proposal** from `shared.eval_claim_proposal`, loaded by
`POST /admin/writing-memory/claim-labels/proposals/import`); one answer (Correct / Wrong / Unsure) resolves the item
and writes the tech-lead row of `shared.eval_claim_label` in one transaction (a split panel: the answered label,
resolved unclear); "accept every remaining proposal" after an in-page second click. The **label set browser** lists
every set version and every labelled claim (who decided, when, why); a tech lead can change a final label there:
append only, a `shared.eval_claim_relabel` row that supersedes the current one (a chain per claim), and the newest
decision is what the report and finals read (`review.effective_rows`). The **Jev pre-filter** (AA-1063,
`services/ground_truth/prefilter.py`, `POST .../claim-labels/{set}/{version}/prefilter`) settles a disputed claim as
supported when Jev `a1_claim_supported` p >= 0.90, the panel majority is ok and no non-ok lens says `fact.misbinding`
(S275 back-test 341/341 held-out): a tech-lead row with labeller `prefilter:jev090+panel`, final-label source
`prefilter`, never release evidence nor an audit adjudication; shadow (report only) unless the api's
`CLAIM_PREFILTER_MODE=enforce`, and enforced only for the measured T9 writers.
_Avoid_: fine-tune, model update, deploy (a Release is data, not code).

**Golden Set**:
A versioned, frozen set of labelled writer samples (AA-729: A1 50, T2 10, blog 15, LinkedIn 15, plus 10 injection
traps) that R2 runs a Release against. Stored since CIS 284 (AA-919, WM-17): `shared.eval_golden_set` (one row per
imported version, one active per name; platform inputs only — the `s1:*` brand pack), `shared.eval_golden_item` (one
sample: frozen input and output, label, writer / channel, scope / tenant, split `tune` / `holdout` (30%, stratified
by writer × label verdict, seeded) / `trap`, tiers incl. the derived `long_9d`, `fact_dense`, `post_edit`) and
`shared.eval_item_result` (one scored output per run, item, arm and mode). T2 / T9 items are tenant text (the tenant's
brand pack is inlined on its items): RLS ENABLE + FORCE, no tenant-pool privilege, deleted with the tenant (AA-839).
Runs are rows of `shared.prompt_eval_runs` with `pipeline = 'writing_memory'` and a `run_kind` (baseline_import,
r0_replay, r1, r2, reverify, probe, shadow); a trigger keeps controls honest (a human-edited output is never a writer
control; controls are regenerated only in a baseline or reverify run; a result belongs to a run of its item's set), and
a run with results keeps its writer, kind and set. Imported by `python -m services.golden_set.importer` (DRY RUN by
default, $0: model calls raise in that process; a new version must be newer than the active one). A CI guard
(`services/golden_set/text_guard.py`, keyed fingerprints in `.github/golden_text_fingerprints.txt`) fails when golden
text lands in this public repo; tests use invented fixtures only.
_Avoid_: test set (ambiguous), eval data, Pinned Exemplar (a release item, not an eval sample).

**Eval side (DEV / CAL / GATE; AA-1051, root ADR 0020 W5)**:
The frozen use of a golden item, orthogonal to its R2 split: `dev` (rubric and policy work), `cal` (judge calibration)
or `gate` (sealed: release and W5 proof decisions only). Stored in `shared.eval_golden_split` (CIS 400, append only,
FK to the item): placed by `sha256("aa1051-eval:<sample_id>")` under rule `aa1051-split-v1` (cal 30%, gate 30%, dev
40%), per sample so a sample keeps its side in every golden version; readers read the stored row, never recompute it.
Written by the golden importer and by `python -m services.writer_proof.splits` (DRY RUN first). An R2 run with
`--eval-split gate` plans only GATE items (traps stay in the probe) and decides `judge_counts` on the CAL items only,
reporting `details.kappa_eval_split` (CAL vs the planned side; audit target: within 0.1).
_Avoid_: holdout (that is the R2 phase split), test split.

**Writer proof (W5; AA-1051, root ADR 0020 D12)**:
A small paid run that measures one writer change on a pre-registered fixed GATE sample: the eval null run (T9 blog, no
change, must come out non_inferior), T9, S1 and T2. A proof is one or two R2 runs (arm B, optional repeat arm A') under
stage `adhoc_aa1051`, queued by `python -m services.writer_proof.runner run --live --confirm CONFIRM --cap-usd <n>`
only when its **pre-registration** (`services/writer_proof/prereg/<id>.json`: metrics, fixed sample ids, an absolute
non-inferiority margin, the pinned judge, the cap) is registered and matches its locked sha
(`prereg/registered.lock.json`; the run rows carry it too), the power check from the R1 vote variance is adequate, the
estimate (the larger of R2's and the pinned judge priced from `shared.llm_model_catalog` on that model's own past judge
tokens) fits, a null run with the same judge and golden version came out non_inferior first (T9 / S1 / T2), and the
D12 caps and minimum samples in force (re-read before the file is checked) hold (settings
`cis.spend.writer_proof_*_cap_usd`, defaults null $5, T9 $15, S1 $8, T2 $6.5 (CIS 460, S285), $33 in total, read against the ledger; minimum
samples `cis.quality.writer_proof_*_min_items`). `runner campaign` is the DRY plan of all four in order. `runner report` gives the
pre-registered verdict, the targets and the cost and requests per object from `shared.llm_call_log`.
S283: the judge may be a **pinned claim instrument** (`judge.instrument`, `services/writer_proof/instrument.py`): the
AA-1048 chain R1 fire then two-step v7, ADVISORY (v7 missed one AA-1048 bar), judging every sentence of each output and
counting the flagged claims (`fail_claims_per_item`); R2 then skips its kappa block and judges the whole fixed sample.
A file may fix its sample as a rule (`sample`: n GATE items by a seeded hash of the id, drawn from the registered
golden version). The registered null run is `w5-null-t9-blog-v7-g150` (S284: golden aa729@1.5.0, 12 t9_blog GATE
items; it supersedes `w5-null-t9-blog-v7`, whose 1.4.0 had 6). Its control A is the newest finished **reverify** run of
the writer on that golden version, of no release or an active one (a reverify of a fixed sample regenerates the control
of every sample item), paired on its regenerated controls only, never its candidate rows, so B == A by configuration;
without one, or with a control missing, the run refuses at $0.
S285: the T9 / S1 / T2 drafts (`w5-t9`, `w5-s1`, `w5-t2`) name the null run's judge and golden version, decide
non_inferior on `fail_claims_per_item` at the null run's margin, and list their non-trap GATE items; writer cost and
request targets read the writer path's ledger rows (`writer_cost_per_object_usd`: every row but the eval judge's, so S1's
in-graph judge and T2's QA gate count; `writer_requests_per_object`: the writer-role calls), since the
claim judge adds tens of calls per object. A draft's `plan` also lists what its registration would refuse
(`registration_problems`: the minimum sample in force) and blocks a sample item the claim judge cannot read (no stored
control, or over the evidence cap).
S285 **golden carry** (`services/writer_proof/null_carry.py`): when no null run of the proof's golden version passes,
the null gate accepts a registered non_inferior null run pinned to an EARLIER version of the same golden set with the
same judge (model, connection, instrument) if every GATE item of the null run's writer is byte-identical in the proof's
version (same sample ids on GATE, every stored column of `shared.eval_golden_item` but its ids, and its
`shared.eval_golden_split` row) and the null run's resolved sample is among them; the shared inputs count too unless
the writer's judge source never reads them (T9). It never carries back to an earlier version. The plan reports
`null_run.rule` (`same_golden_version` | `golden_carry`), `golden_carried_from`, `items_compared` and `sample_items`;
a run that cannot be carried is named in the blocking line with what differs. `same_instrument` stays strict.
S285 **fire share** (`services/writer_proof/fire_share.py`): the claim judge's price uses a measured share of
claims the R1 fire sends on per writer (`instrument.FIRE_SHARE`; a writer without one is planned at every claim). The
tool measures it by running the fire step alone on a proof's stored control outputs (DRY RUN first, then `--live
--confirm CONFIRM --cap-usd <n>` within the D12 caps, a per-call cap, ledger rows on `adhoc_aa1051`), printing counts
only and the `FIRE_SHARE` entry to pin in a reviewed PR.
_Avoid_: A/B test (a proof has a fixed pre-registered rule), experiment.

**Scope (writing memory)**:
`platform` (applies to every tenant's writer; never holds tenant text; needs evidence from ≥ 3
distinct tenants when the evidence is tenant content, plus Nghiệp's approval) or `tenant` (one
tenant's rules and exemplars, used only for that tenant, never for training). `tenant_id IS NULL`
iff `scope = 'platform'`, as in Facts Entry.
_Avoid_: global / local, shared (ambiguous with the `shared` schema).
