# Pipeline stages

> Detail page of [CONTEXT.md](../../CONTEXT.md): the A-series and both T-series, stage by stage. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Pipeline stages

### A-series (Admin, master content — computed once, shared by every tenant)

- **A0 — Upload**: raw tour ingestion (`raw_tours`, `pipeline_status='ingested'`).
- **A0 note**: tours with an empty itinerary are skipped at upload and flagged "No Itinerary"
  (AA-604). As of the 16/09 data reset: 793 raw tours kept, 30 empty-itinerary rows trashed,
  763 ready for S1.
- **A1 — Generic Rewrite (S1)**: the admin S1 pipeline rewrites A0's raw content into
  `generated_content` with a neutral, brand-agnostic voice (see AA-535) — the shared base every
  tenant later re-voices for their own brand. Flow: generate → validate → judge →
  (retry ≤3 or HITL) → brand_audit → **grounding** → flag_fix → revalidate. Can run per tour or,
  once AWS enables it, as a Bedrock Batch job (see Repo Context).
- **A1 grounding** (AA-691, `services/content_generation/grounding.py`): every sentence of
  subtitle / summary / highlights / itinerary bodies is checked against the **raw source**. A
  number the source never states (after reading travel-time conversions, clock formats and glued
  figures) is `UNSUPPORTED_NUMBER`; the Jev Question `a1_claim_supported` can clear it (accept
  side) and, once calibrated on its reject side, raise `UNSUPPORTED_CLAIM`. Violations are
  rewritten sentence by sentence against the source (one `s1_flag_fix` call); what is still
  unsupported after revalidate → `manual_check` (A2). Master content is the one place this check
  exists — T3 grounds tenant rewrites against master content, not the raw source.
  Ground truth for the Jev Question (AA-845): in the Review Queue drawer ("Source check") a reviewer
  marks single sentences unsupported (`decision_log.outcome` truth=false for that sentence only); a
  Review approve stamps truth=true on the rest, a Review reject stamps nothing (it judges the whole
  version). `GET /admin/decisions/claim-sample` rebuilds a stratified sentence sample (+ the source
  Jev read) for panel labelling ($0).
- **A1 day oracle + quality shadow** (AA-925, Quality System v1 Q-2): one duration / day-marker reader,
  `services/content_generation/day_parse.py` (`prompts._estimate_day_count` and `seo_meta_utils.tour_days`
  are thin wrappers; "3 hours" is one day, "7D/6N" seven). It also owns the ONE ranged-heading rule (AA-990
  S253, `day_range` / `CANONICAL_HEAD` / `canonical_blocks`): "Day 12-15" (hyphen, en dash or "to") covers days
  12..15 when 12 < 15 <= 12 + 14, else it is the single first day; source markers, written headings
  (`itinerary_utils`, `output_days`), validate's dash normaliser, the day gap check / `final_day_guard`,
  `places/validity`, `places/days`, the T2 day markers and the SQL twin `catalog._tour_day_text` /
  `v_tour.duration_days` (CIS 289) all apply it, so a ranged day is never folded into the day before.
  Two written-day readers, on purpose (AA-1049 S273): `output_days` / `canonical_blocks` read only the canonical
  "Day N — Title" heading (S1's storage contract and its SQL twin); `written_day_numbers` reads any written form (the
  dash-fixed T2 "Day N: Title" / "Day N · Title", markdown or bold headings, a writer-JSON day list) and is the ONE
  reader of the T2 final contract, the T2 orchestrator's Master slot check and the repair contract's days_kept, so
  those day-count checks can never disagree. Its per-day twin `written_day_word_counts` (same day set) is the written
  side of `graph.itinerary_day_gap` when validate_node checks a T2 tenant rewrite (T3 and the linear orchestrator
  validate the dash-fixed text); S1 / A1 and `final_day_guard` keep the canonical reader. The day oracle
  (`day_oracle.check`, shadow-only on a tenant rewrite) still reads `output_days`.
  `validate_node` runs the deterministic day oracle
  (`services/quality/day_oracle.py`: sub-day split, duplicate / out-of-order / skipped / extra day, departure
  mid-trip, count vs duration, `SOURCE_DAY_CONTRADICTION` — a source fault, never charged to the writer —
  and `DAY_CHECK_SKIPPED`) and the S2 cut signature (`seo_meta_utils.meta_cut_signature`, "…a 20-kilometre.").
  Every code ships in **shadow**: it lands in `quality_shadow` (`generated_content.metadata.quality_shadow`)
  and changes no score or verdict. A code moves to enforce (`DAY_ORACLE_MODES`) only after a precision read on
  stored tours (`scripts/aa925/day_oracle_report.py`, D5: ≥ 0.70 with the Wilson bound); an enforced code is an
  A1 hard code (`_HARD_BLOCK_CODES`, rewrite class) and stays shadow on a tenant rewrite (T2/T3).
  A day list in the source ("Day 05 & 06", "Day 11, 12 & 13") is a range (S248). A meta ending in a two-word
  capitalised place name ("…and Hoi An.") is not a weak ending (S248).
- **S1 fact table + code checks** (S248, M2a S1-first step 1, Refs AA-645; offline only, not wired into the graph —
  S1-first freezes new gates): `services/quality/s1_facts.build_fact_table(tour)` builds, by code, the per-tour fact
  table the step-2 writer will read (duration kind, source days with their places, schedule lines, places, figures
  with unit / noun, included / excluded / optional lines, conflicts, the "ask the supplier" missing list, do-not-use
  operator names / contacts); every verbatim value points at its source field (`verify_pointers`).
  `services/quality/s1_checks.run_checks(tour, page)` measures a written S1 page against it (day oracle, day-place
  drift, cut / out-of-band meta, truncated body, novel figure, figure with the wrong unit, name not in source,
  inclusion the source excludes). `scripts/s1_step01/fixture_report.py` runs both over the golden fixture or any
  page set (A/B arms) and scores them against a ground-truth file; results with tour text go to the private root.
- **S1 writer v2 + A/B arms** (S248, M2a S1-first step 2, AA-645; offline only, the live graph does not import it
  — step 4 wires the A/B winner behind a flag): `services/content_generation/s1_writer_v2.py` builds, purely, the
  three arms the A/B compares — `aplus` (today's A1 writer prompt byte for byte + a code-built DAYS block), `w` (the
  new writer: a code-built FACT SHEET from `build_fact_table` instead of the raw dump; no per-day length targets,
  no nudge, no brand-differentiation block, no PAA; place background allowed, figures only from the sheet) and `e`
  (a code-built short page: At a glance box, `Day N —` blocks with source fact lines, Included / Not included; the
  model writes only name, subtitle, hook, highlights and one sentence a day). Every arm gets its SEO title + meta
  from a separate, code-checked call with one retry and no cut — at most 3 calls a tour. The day plan is code: source
  markers (ranges expanded) win over a disagreeing duration; a sub-day / one-day tour is one block.
  `raw_tours.group_size` / `best_time_to_go` reach the writer (`with_agency_fields`). The paid harness is
  `scripts/aa645/s1_screen.py` (LLMClient, `llm_call_log` stage `adhoc_aa645`, role `writer`; runs on the deployed
  image via S3) and `scripts/aa645/score_screen.py` (panel rubric v1 + rank, code gates, $ per tour, the W pick,
  paired Wilcoxon for the A/B runs); the gate codes are defined once in `scripts/aa645/gates.py`
  (DAY_PLACE_MISMATCH, NAME_NOT_IN_SOURCE, NUMBER_NOUN_MISMATCH are report-only). The A1 flag-fix prompt and the A1
  brand-audit judge's user prompt are neutral white-label (no "Adventure Asia"); the judge's voice rubric
  (`AA_BRAND_IDENTITY_PROMPT`) is unchanged until the step-5 cleanup.
- **S1 writer v2 on the live A1 path** (AA-645 step 0, S248; Nghiep d01 / d02): `S1_WRITER_V2` (env on the worker,
  default OFF) makes `generate_node` write arm W through `services/content_generation/s1_writer_v2_live.py` — writer
  + separate meta call + one meta retry on the `s1_generate` stage (the model is that stage's Model Gateway route:
  W x Sonnet = a route change in Settings, no model id in code), each reply one `llm_call_log` row (role `writer`,
  `quality_signal.writer_version = "v2"`). A1 admin rewrite only: never a T2 tenant rewrite (`is_tenant_rewrite`,
  stage `t2_generate`), never the Bedrock Batch seed path. The draft carries `writer_version = "v2"` (graph state,
  `generated_content.metadata.writer_version`): validate keeps ITINERARY_STILL_COMPRESSED and DFS_INTENT_UNDERUSED
  report-only for it (in `quality_shadow`, detector `s1_writer_v2`: v2 has no length target and no nudge, and uses a
  keyword only when it fits), and the A1 judge computes and logs brand fit but does not gate it (gate label
  `brand_fit_report_only`: no min() into the score, no tie-break, no feedback into the retry). The same holds on a
  reviewer edit / admin revalidate of a v2 version (`_revalidate_tour` seeds `writer_version` from the metadata).
  When the AA voice-rubric LLM brand audit runs on a v2 draft (only when the judge did not), its voice codes and a
  bare not-publish-ready verdict are report-only too (`quality_shadow`, detector `s1_writer_v2_brand_audit`); the
  pre-audit codes, product-truth codes and a `manual_check` verdict still gate. Every other gate (validate,
  grounding, flag_fix, revalidate, the day oracle) is unchanged. `raw_tours.group_size` /
  `best_time_to_go` reach the v2 fact sheet as `tour_agency` (A1 only).
- **A1 brand-fit rubric v2** (AA-645, S249; d02 follow-up): `services/content_generation/brand_fit_v2.py` scores the
  page's voice against the white-label house style WITHIN THE LIMITS OF ITS SOURCE — the judge reads the writer-v2
  fact sheet of the raw tour, plain wording on a thin source is not "generic", it never rewards or asks for brand
  colour / an angle / claims the source lacks, it lists the specifics the page states that the source does not
  support (quoted; code keeps only quotes found in the page and caps the score at 6, or 4 for three or more), and code
  caps it at 5 when the page names "Adventure Asia" / "Design This Journey" and the source does not. Same reply shape
  (`brand_fit_score` = voice, `cross_brand_distinct` = source specificity), same `s1_judge` route, same AA-714
  near-band median of 3 — `brand_fit.score_brand_fit(rubric="v2", source=tour)`. Selected by `A1_BRAND_FIT_RUBRIC`
  (worker env: `auto` default = v2 on a writer-v2 draft, v1 on today's writer; `v1` / `v2` force); a draft without its
  source in state falls back to v1. T2 and the T7 Debate stay on v1 (they never pass a rubric). The v2 score still does
  not gate a writer-v2 draft until `A1_V2_BRAND_FIT_GATE=enforce` (gate label `brand_fit_v2`, no Jev tie-break: the
  a1_brand_fit Question was set up on v1 scores); that flip waits for the offline re-measure on the 125 A/B pages
  (`scripts/aa645/brandfit_measure.py` + `brandfit_analyze.py`, pass = W x Sonnet retry % <= A+ retry % + 5 points AND
  Spearman(brand fit, panel serious errors) <= 0). Every v2 score carries its rubric version
  (`brand_fit_v2.RUBRIC_V2_VERSION`, pinned to the prompt text by a test) in `llm_call_log.quality_signal.rubric` and
  `generated_content.metadata.judge.rubric` (+ `unsupported`, `caps`); absent = v1. The two are different scales: the
  wave report labels each brand fit with its rubric. No brand-fit cache exists on the A1 path; the T7 Debate cache
  (`acp_shared.debate_brand_fit_cache`) holds `score_candidate_fit` scores, which this does not change.
- **Shadow run of the A1 chain** (AA-645, S249, d03): `scripts/aa645/shadow.py` runs `v1_pipeline._rewrite_tour` (the
  graph the A1 job runs, + the AA-736 Sonnet retry rule) with writer v2 on in-process only and its writer + meta calls
  on `--writer-model`, for a pilot (3 Laos tours with built places) or <= 50 tours stratified by country (golden and
  elephant-riding tours excluded). Nothing is persisted (no version, review row, publish, atomize, place work); the
  AA-729 isolation layer (`scripts/aa729/evalpatch`) logs every call under `adhoc_aa645` with the tour's job id, so real
  $ per tour comes from `llm_call_log`. `shadow_report.py` gives the per-country table and the memo section 6 stop
  conditions.
- **Per-wave quality report** (AA-645 step 0, d13): `scripts/aa645/wave_report.py` (read-only, $0) compares, per
  country, the version a rerun wave wrote with the version that was live before it: outcome (published / stayed v1 /
  not published), the A/B code checks on both pages (gate pages = the serious-error proxy, gate codes, figure
  coverage), stored validate codes, grounding still-unsupported, brand fit (report-only on v2), $ per tour, atoms after
  the wave (published-not-atomized, live atoms on a day past the new day count), and the memo section 6 stop
  conditions.
- **Degraded detectors + repair contract** (AA-925, Q-2, design §4.3/§6.3; `services/quality/`, pure modules —
  gate runners are passed in, no writer module is imported):
  - `degraded[]` (`services/quality/degraded.py`) names every fail-open path that ran on an output: Jev
    zone=error (`jev_unavailable` — hard on an enforce question or an unknown mode, counted on a shadow one;
    zone=skipped is not degraded), `grounding_not_run` (hard: the node crashed or the re-check failed; not a
    tenant rewrite, not the score-only revalidation graph), `grounding_no_source`, `f9_judge_unavailable`
    (hard, T9), `day_check_skipped`, `judge_unavailable`, `brand_audit_unavailable` (it already fails closed)
    and `repair_contract` (soft). Stored in `generated_content.metadata.degraded` (A1), the T3 outcome
    (`job.result`), `content_piece.flags` + `repair_log` (T9). One `quality_degraded` log event per output.
  - **One A1 hold**: `degraded.apply_a1_hold` at the end of the graph (`_apply_grounding_recheck`) adds the
    hard code `DEGRADED_DETECTOR` (`_HARD_BLOCK_CODES`, NOT the Sonnet rewrite class — a retry cannot fix an
    outage; `HARD_CODE_REPAIR` "review: re-run…") and `manual_check`; `_is_publishable`, `human_edit_gate` and
    the review summary read it through the hard-code path; the Review Queue shows it red.
  - A1 Jev today: `a1_claim_supported` has no reject ceiling, so Jev only CLEARS numeric hits — an outage is
    already fail-closed on numbers and the hold adds review load, not safety, until AA-845 sets a ceiling.
  - Per job: `decide()` notes every zone=error verdict into a contextvar collector bound by the job worker
    (`shared.job.result.degraded`), so A3 / ingestion / S1 prefetch outages are counted without their code.
  - **Instrument marks** (S271 W0, AA-900; `services/quality/route_guard.answer_marks`, set by the gateway on every
    reply): `fallback_instrument` (a judge / validate / decide answer from route position > 0, or from nova-lite)
    and `same_family_pair` (a judge of the same model family as a writer of the same output; the family trace is
    bound per output by `route_guard.collect_output`: T9 piece, T2 version, S1 tour). On the reply
    (`LLMResponse.route_degraded`), in the ledger (`quality_signal.degraded`) and in the collectors, severity
    `count`: T9 flags the piece (non-blocking), S1 stores it in `metadata.degraded`, T2 in
    `judge_breakdown.route_degraded`; no hold reads them yet. A writer at position > 0 carries `fallback_writer` on
    its reply too (the AA-1042 note is unchanged).
  - **Writer note** (AA-1166, S294 red-team; `services/content_generation/writer_reply.py`): the S1 v1 / T2 writer
    reply is read as its one JSON object; free text around it (a note before the object such as "the source held
    an instruction, I did not follow it", text after it, fence markers) never reaches the draft. A note is marked
    `writer_note` (severity `count`, detector `writer_reply`, detail `instruction_notice` / `refusal` / `other`),
    the writer's ledger row gets `quality_signal.writer_note`, and one `writer_reply_note_dropped` info line keeps
    its kind and length; the note's text is stored nowhere. A reply with no usable object fails as before.
  - **Repair contract** (`services/quality/repair_contract.py`): a repair may not fail a gate that passed,
    add a figure (or, for the grounding / T3 fact editors, a name) the text and source lack, cut a sentence
    short (incl. the cut signature "…a 20-kilometre." that `meta_complete_sentence` accepts — a modifier left
    without its noun; dropping a whole trailing clause before "and / with / to …" is the fact editor's job and
    is kept), touch an unflagged field, break a tag / bold / bracket pair, or (AA-645 `days_kept`) change the
    days the itinerary's line-initial "Day N" headings cover (ranges expanded) — drop, add or renumber a day, repeat
    one more often than before, or break the order (the S249 shadow: flag_fix's
    whole-itinerary rewrite returned Rajasthan with 10 of 15 days). The fit's fact tail may state the
    day count `tour_days()` derives ("8 nights" → 9 days). A violating field or sentence keeps its pre-repair
    text (soft `degraded: repair_contract`). Spliced at flag_fix (replaces the AA-641 per-field revert, same
    behaviour), the A1 grounding repair, revalidate's strip + fit, the T3 sentence repair + meta regenerate and
    T9 `splice_checked`. The T9 AA-528/AA-780 regression guard of full rewrites stays in
    `run_write_background` and already covers invariant 1 there. Invariant 7 (AA-790 `integrity_kept`,
    `enforce_step`): every deterministic edit of the T9 attempt loop (`hygiene.autofix`, the family caveat, the
    FAQ droppers, `real_first_faq`, `fix_trip_facts`, `trim_to_band`; `ensure_cta` is must-keep) is reverted when
    it leaves a new repeat, orphan, dangling opener, fragment, split or unfinished paragraph, or a sentence cut
    on 'the' / 'and' (`hygiene.integrity_problems` + `text_integrity.new_structure_findings`: a paragraph is
    unfinished only when it stops on a letter or digit, and paragraph kinds are new by count; steps before the
    final pass are compared as `text_integrity.settle` leaves them). The revert lines go to F13_autofix and the
    kept attempt's reverts count as soft `degraded: repair_contract` (detector `t9_step`); no gate or registry
    entry is added (gate freeze).
    The tenant-voice rubric (`acp_content_writing/voice_rubric.py`) is an offline scorer only
    (`scripts/aa790/voice_rubric_replay.py`): no pipeline reads it until the freeze ends and κ ≥ 0.6.
  - **Writer-v2 hard code** (AA-645, S249): on an A1 writer-v2 draft `ITINERARY_DAY_COUNT_MISMATCH` (a source day
    missing from the output) blocks Master — `graph.V2_HARD_BLOCK_CODES`, read through `graph.hard_block_codes(state)`
    by `_is_publishable` and the human-edit gate; v1 drafts and tenant rewrites keep the soft structure deduction. A
    meal fact line that copies the same source day's meal list ("Meals: dinner." from "(D)" / "Meals included:
    Dinner") is source-backed in the A1 brand pre-audit and is not sent to Jev. A page meal line where a negation sits
    next to a claimed meal ("dinner and no lunch"), or that names no meal and only says "included" / "all meals", is
    not source-backed and goes to Jev.
  - **AA-740 fitter**: `fit_seo_meta_final` no longer cuts a too-long meta mid-clause; it salvages at a
    sentence boundary, appends a facts-only clause to a short one, closes an uncut in-band clause, and
    otherwise leaves the length code to the LLM (A1 flag_fix seo_meta repair + `_rerepair_meta`; T3 a
    meta-only regenerate on `t2_flag_fix` before any whole-tour rewrite).
- **A2 — Admin QA Gate**: A1 rows that failed auto-validation (`status='hitl'`), reviewed via the
  Review Queue before they can reach A3. A row can also be **dismissed** (hidden without
  approving; a future real failure is enqueued again — AA-626, `review_status_enum` value
  `dismissed`).
  - **S271 W0 safety interim** (root ADR 0020, AA-900; notes `docs/implementation-notes/AA-900-l16-w0-t2s1.md`):
    the **hitl exit** (retries used up below the line) no longer ends the graph: `hitl_exit` → brand_audit →
    grounding → revalidate (flag_fix skipped), so the day guard, route guard and degraded hold run on it too
    (`metadata.exit_path = "hitl"`). The **approve** of an unedited version (`v1_pipeline.approve_review`, both the
    admin alias and `/v1/pipeline`) must pass the auto-publish checks (`services/quality/approve_gate.py`: score
    floor, hard codes, grounding ran with no unsupported sentence, brand audit not flagged-unfixed / manual_check):
    else **409 `approve_blocked`** with the reasons, unless the body carries `override_reason` (recorded on
    `review_queue.reviewer_notes`). A tour held only by the **elephant-riding policy** (D8; detector
    `services/quality/elephant_policy.py` over every page field and the source, fail-safe negation, a bare
    "elephant safari" in Sri Lanka is a jeep safari) is approved as `policy_keep`, and a human edit of one passes the
    re-validation gate (`human_edit_gate_node`); flag_fix never edits such a tour. "Held only by the policy" is
    `elephant_policy.is_policy_only_hold`: `FACT_CHECK_MANUAL_CHECK` as the only brand code AND a detector hit on
    the version or its source; the code alone is not enough (the LLM audit also emits it for other fact checks,
    and those stay blocked). The **S1 judge is off the live path** for a writer-v2 draft
    (D9, `S1_JUDGE_LIVE` off by default; the brand audit then uses its deterministic pre-audit codes), and
    `POST /admin/s1-batch/submit` answers 409 while writer v2 is on (the batch seeds a v1 draft).
  - **Elephant-riding policy hold** (AA-1049, tech lead decision S277, root ADR 0020 D8; CIS 398; notes
    `docs/implementation-notes/AA-1049-l13-elephant-hold.md`): a Master whose product offers elephant riding (detector
    hit on its published text or its raw source) is **not on sale until a person reviews it**. The tour lifecycle
    service's `policy_hold` (`services/tour_lifecycle`, rules in `services/tour_lifecycle/policy.py`) switches it off
    (`master_status` inactive, `master_status_reason` `policy_hold`, the pause cascade: served state `paused`, queued
    T2 / T8 / T9 jobs cancelled, TripPlanner purge, no tenant mail) and files one pending **policy review** row: an
    ordinary `review_queue` row marked by `escalate_detail` items with `check_id` `policy:elephant_riding` (evidence =
    where + field codes only, never the matched words) and a `POLICY_REVIEW` summary prefix
    (`services/quality/policy_review.py`). Only the Review Queue **approve** lifts it: the approve claims the row
    `approved`, `process_export` publishes, and the service's `publish` sets the Master active (reason NULL); the approve
    of the held Master's own version skips the new-version approve gate (it puts back what was on sale). Reject keeps it
    held. **Guard**: `publish` refuses (409 `policy_review_required`) a version with a detector hit and no `approved`
    review row of that version: `process_export` moves it to `hitl`, files the policy review and raises
    `PolicyReviewRequiredError` (the S1 auto-publish logs it, promote answers 409); Set Active refuses a held Master or
    one that hits with no approval (and files the review); restore / set-active hold a Master they would switch back
    on. `_enqueue_review` files a version held only by the policy (`is_policy_only_hold`) **pending** even when the tour
    has a live or held Master (AA-653 keeps every other hold of a live tour dismissed); a dismissed row never
    supersedes a pending policy review, and the publish dismissal (step 3c) spares one of a newer version. Review
    Queue: filter `policy=elephant_riding`, row field `policy_review` {kind, held, approvable, evidence}, facet
    `policy_reviews`; the page shows an "Elephant riding policy" badge and an "Off sale until approved" badge. The
    one-time catch-up of the Masters published before the detector: `scripts/quality/hold_elephant_masters.py` (DRY
    RUN prints the count, 8-char ids, field codes and the pin `count:md5`; `APPLY=1 PIN=...` holds them one by one).
  - **AA-1049 W3 S1 train (opt-in, default off)** (`services/content_generation/s1_train.py`, worker switch
    `S1_CONTROL=contract`; unset / any other value = the A1 path above, byte-equal to it; A1 writer-v2 drafts only):
    code composes the contract fields after the writer (day headings numbered and ordered by the day plan, the
    source day's verbatim fact lines in place of the writer's, the SEO title fit, a code meta template when the meta
    is still out of band; the writer prompt is unchanged); the `s1` rows of
    `services/acp_content_writing/route_table.yaml` (one per taxonomy `a1_codes` code: cut /
    regenerate_section / rewrite / hold) drive a class router (a `rewrite` code redrafts once, 2 drafts in total) and
    a deterministic local fix that replaces flag_fix and its meta re-repair (no model call; repair contract, then the
    detectors re-run; what is left is `manual_check`, no second grounding repair round); the grounding node's sentence
    repair is a code cut too (`grounding.cut_and_recheck`, S277), so the train's path makes no `s1_flag_fix` call; the judge stays off even with
    `S1_JUDGE_LIVE=on`. Report in `generated_content.metadata.s1_train`. The publish gate is unchanged (its enforce
    switch is AA-1048). Free replay `scripts/quality/replay_aa1049_s1.py` over stored v2 Masters (export
    `export_aa1049_s1.py`, runner `replay_aa1049_s1_local.sh`).
  - **Master recompose (AA-1049 path A-new, no model call)** (`services/content_generation/s1_recompose.py` pure,
    `s1_recompose_store.py` DB, runner `scripts/quality/recompose_masters.py`): a served v2 Master whose fixed fields
    differ from what the S1 train composes today gets them recomposed by the same `s1_train.compose_page` (day
    headings, the source day's verbatim fact lines, the title fit, the code meta template) while the narrative stays
    byte-identical (name, subtitle, summary, description, highlights, day titles, prose lines) and no fact is lost (a
    writer fact clause such as "Overnight: X." is cut only when the same day gets the source's verbatim fact line with
    the same label and value; else it stays, and a recompose that would drop a stored fact is skipped as
    `facts_would_be_lost`), and the result is a NEW
    approved `generated_content` version (a copy of the served one, `metadata.recompose_of` = its id,
    `metadata.writer` = `recompose-v2`, `writer_version` kept, the score row copied with today's failure codes)
    published through `process_export` (exit `recompose`): A3, source fan-out, `updated_at` and the other apps follow.
    Skipped and counted: source not live, a pending review, a newer version in flight, an active pipeline job, not v2,
    human-edited (opt-in), the elephant-riding policy, a legacy-format itinerary, a narrative change, a no-op, a
    contract verdict worse than the stored or today's one, a published recompose a person rolled back (never
    republished; only a never-published one is resumed). DRY RUN prints counts (incl. lines_removed, lines_added,
    facts_lost), 8-char ids, the pin and a capped before / after sample (SAMPLE=n); APPLY needs an open rerun wave
    holding the set and the pin, refuses when a Master due has facts_lost > 0, and stops before the next Master once
    the wave is paused or closed.
- **A3 — Master Content Pool**: `gold_aa_internal.published_tours` — a tour is "in Master
  Content" once it lands here. Atomize (see Atom above) now fires automatically right after a
  tour is published here (AA-526, `services/export/handler.py::process_export()`), as a
  fire-and-forget background task, `owner_scope='platform'`.
  - **Publish dates** (AA-645 d06, CIS 273): `published_tours.published_at` is the FIRST publish — a new version of a
    published tour (re-export, a rerun wave, a promote) keeps it, so the tenant Browse pool and TripPlanner's browse
    order do not move; `published_tours.updated_at` records the last change, and `catalog.v_tour.updated_at` follows
    it (`catalog._live_tour.master_updated_at`). Anything that means "changed recently / in this wave" reads
    `updated_at`: the rerun-wave report (`services/waves/report.py`, `published_in_wave`), the admin library
    `published_last_30d` / `stale_count` / `last_published` and the overview funnel `published_7d`.
  - **Promote = per-tour rollback** (AA-645 step 0, C2): `POST /admin/tours/{id}/versions/{n}/promote` publishes an
    `approved` version through `process_export` (every text / SEO field, the score pointer, the live-source rule) and
    re-atomizes it with `dedupe=False` + `force=True` (the version's job key and day fingerprints are already spent);
    atom ids are content hashes, so the old text brings the old atoms back. A version that is not `approved` → 409
    (approve it in the Review Queue); a dead source → 409 `source_not_live`. The Master write is the tour lifecycle
    service's `publish` (AA-986 L2, see Served state): it locks the source FOR SHARE and re-checks it, so a retire that
    lands between promote's check and the publish is refused too (process_export's L1 refusal, 409). The publish-date
    rule above is the ON CONFLICT branch of the repository insert that only that service runs.
  - **Tour versions, one publish gate, the hand-edit lock** (AA-1146, S285; CIS 457-458). A person's change of a
    tour's wording is a **draft version**: a new `generated_content` row (`status` draft, `origin` human,
    `parent_version_num`, `edit_reason`, `author_id`; editors in `edit_diff.editors`), one open draft per tour, copied
    from the live version; the live version is never edited in place any more (`PATCH .../generated/{id}` on it → 409
    `edit_live_version`; on a person's open draft → 409 `edit_draft_version`: drafts are saved only through PUT /draft,
    which keeps their editors). Routes (`api/routers/admin_pipeline/drafts.py`): `GET|PUT|DELETE /admin/tours/{id}/draft`
    (state + per-field word diff against the live version; save needs a reason; discard = status rejected),
    `POST .../draft/publish`, `POST /admin/tours/{id}/unlock` (master.admin, reason), `GET .../raw-revisions`.
    **One gate** for every publish route (Review Queue approve, promote, draft publish): a hand-edited version needs a
    passing re-validation (409 `revalidate_required`; promote too since AA-1146), and while the setting
    `cis.content.publish_two_person` is on (Dev default off; admin only, maker_checker; while on, changing it needs a
    second person) the publisher must hold `content.publish` (admin, content_lead; else 403 `two_person_required`) and
    must not be one of the version's editors (403 `same_person`) — `api/routers/admin_pipeline/_publish.py`,
    `api/services/content_publish_gate.py` (reads the switch in the route's transaction, fails closed). The route
    capability is `content.publish | review.act`, so `content` publishes only while the switch is off. **Lock**: a
    person's publish of a hand-edited version (`process_export` exits approve / promote / human_publish) sets
    `published_tours.human_lock_*`; a writer's version published by a person lifts it. Automatic exits (auto_publish,
    recompose, lambda) over a locked Master are refused (`HumanLockedError`, the version → hitl), the `s1_rewrite` job
    skips a locked tour before any paid call (result `{skipped, reason: human_locked}`), `run-tour` answers 409
    `human_locked` and the S1 batch submit leaves locked tours out (`services/content_generation/human_lock.py`).
    **Source tour history**: every raw edit writes `silver_aa_internal.raw_tour_revision` (append-only, before / after,
    actor, optional reason) and an audit row (`raw_tour.fields_edited`); `price_raw` needs `reference_price.write`
    (admin, customer_success; AA-1146 D5 — content_lead lost the reference price write). **Excel re-import (T1)**: the
    Duplicate Review offers "Merge field by field" (`GET /admin/upload-staging/{id}/merge-preview`, decide `merge`
    with `resolutions` + reason): fields nobody edited by hand take the sheet's value, hand-edited fields that differ
    are conflicts the person resolves (keep / take), an empty cell never erases. **The result is a draft of the facts**
    (`silver_aa_internal.raw_tour_fact_draft`, one open per tour; the tour is not touched): `GET|DELETE
    /admin/tours/{id}/fact-draft`, `POST .../fact-draft/publish` (`api/routers/admin_pipeline/fact_drafts.py`) runs the
    same two-person rule (the merge's author is the editor), the price rule, a live source and a stale check (409
    `fact_draft_stale` when a field changed since the merge), then writes the facts with one `raw_tour_revision` row
    (`excel_merge`, resolutions) and runs the source fan-out. Without `reference_price.write` a merge never changes
    `price_raw`: taking the sheet's price is 403 `price_read_only`, an untouched price change is held back (`withheld`
    in the result). **Re-check verdicts belong to the text they read**: the re-validation writes `revalidate_passed`
    only while the version's `edited_at` is the one it read (a save during the run rolls the verdict back, result
    `stale`). **Waves**: a tour skipped for its lock (or left out of the batch) is the terminal wave state `locked`
    (who, when, why on the report row), so a wave holding one can complete.
  - **Content workspace: hand-made tours tied to an AA supplier** (AA-1148, S290; CIS 478, Booking bk-073).
    `/admin/content-workspace` (AA Catalogue): a supplier that sends one tour by e-mail or PDF is entered as a form
    instead of an Excel row. **Create** (`POST /admin/content-workspace/tours`, content.write) starts a new catalog key
    through the tour lifecycle service (`supersede(create_new)`, the only raw_tours INSERT), marks the row
    `entry_origin = 'manual'`, keeps the structured facts in `raw_tours.manual_facts` (day cards: title, places, meals,
    overnight, description; inclusions; exclusions; nights; group size; tour type; guide languages; best time) and
    renders the text columns every reader uses from them (`src_itineraries` as canonical "Day N — Title" blocks,
    `duration` "3 days 2 nights", `inclusions` / `exclusions` one item per line, `group_size`, `best_time_to_go`), and
    writes the first wording draft (version 1, status draft, origin human) from the facts (REPORT-mode final contract
    exit `human_edit`). From there it is a normal tour: the wording is edited, checked and published through the
    AA-1146 draft routes and gate; `POST /admin/tours/{id}/draft/publish` also requires a hand-made tour's **required
    list** complete (409 `required_missing`: name, country, an active supplier, 1..max days, nights, a title / place /
    description for each day, the included / not included minimums, no item on both lists, the wording with one
    "Day N —" block per day; nights ≠ days − 1 and a missing overnight are hints). Rewrite (S1) can write it too, but
    **the required list holds on every publish exit**: `process_export` checks the version it publishes
    (`content_workspace.store.export_guard`) and refuses an incomplete hand-made tour with `RequiredMissingError` (the
    version goes back to `hitl`); the S1 auto publish files it in the Review Queue, a Review Queue approve answers 409
    `required_missing` and puts the review back to pending (`admin_pipeline._publish.approve_export`), promote answers
    409. **Two people**: the people who saved a hand-made tour's facts since its
    last publish (its `raw_tour_revision` `staff_edit` rows, and its creator while it was never published) count as
    writers in the publish gate of the draft publish, the Review Queue approve and the promote; while
    `cis.content.publish_two_person` is on, a published hand-made tour's facts are read-only in the workspace (409
    `facts_two_person`: the save would go live with no second person; a facts draft is a follow-up), and an
    **automatic exit** (S1 auto publish, recompose, lambda) refuses a hand-made tour whose facts a person wrote since
    its last publish, or that was never published (`FactsReviewRequiredError`, version -> `hitl`; S1 files it in the
    Review Queue, where the approve counts the facts writers). **The drawer** (`PATCH /admin/tours/{id}/raw`) refuses a
    hand-made tour's facts (409 `manual_tour_use_workspace`; only `price_raw` stays there) and its Original tab shows
    them read-only with a link to the workspace (`raw.entry_origin` in the detail). **Facts saves** (`PUT …/tours/{id}/facts`, content.write; hand-made tours only, 409 `not_hand_made`): before the
    first publish the page autosaves; once published a save needs a reason (422 `reason_required`; a reason has 3..500
    characters, 422 `reason_length`), keeps the required list complete (409 `required_missing`) and runs the source
    fan-out; every save that changes something writes one `raw_tour_revision` row (`staff_edit`) and one audit row; a
    save carries the `expected_saved_at` it loaded (422 `expected_saved_at_required` without it once the tour has one,
    409 `facts_changed` when older). **The AA
    supplier** is Booking's (`booking.v_supply_suppliers`, contract `supply.supplier.v1`, bk-073: id, code, company
    name, categories incl. `dmc`, country, status; SELECT to cis_app), stored as the soft key `raw_tours.supplier_id`;
    `provider` keeps the sheet text (a hand-made tour's provider is the supplier's name). Every read of the view fails
    soft (the picker says the list is unavailable; nothing is linked). **Supplier links** (`GET /admin/content-workspace/
    supplier-links`): the live tours with no `supplier_id`, with the one supplier whose normalised name (accents, case,
    punctuation and company-form words removed) equals the sheet text in a compatible country; `POST …/supplier-links`
    links chosen tours (content.write), `POST …/supplier-links/apply-suggestions` links every suggestion (content.publish,
    DRY RUN first); each link writes a history and an audit row. **Limits** are settings (CIS 478, Platform › Settings,
    `services/content_workspace/policy.py`): `cis.content.manual_tour.max_days` (21),
    `cis.content.manual_tour.min_inclusions` (3), `cis.content.manual_tour.min_exclusions` (1),
    `cis.content.supplier_link.bulk_max` (100); so are the **business option lists** of the form, which an administrator
    edits: `cis.content.manual_tour.tour_types` (json `[{code, label, label_vi}]`, seeded private / join-in; a save
    accepts a listed code or the one the tour already holds, and the tour keeps the label it was saved with in
    `manual_facts.tour_type_label`, rendered as "Tour type: <label>"), `cis.content.manual_tour.quick_inclusions` and
    `…quick_exclusions` (the one-click chips), all served by `GET /admin/content-workspace/options`. The editor's price panel shows `booking.v_from_prices` read-only. Code:
    `api/routers/admin_content_workspace.py`, `services/content_workspace/` (facts, suppliers, store, policy).
  - **Atomize retires what the new text no longer has** (AA-645 step 0): the per-day path soft-deletes live atoms on a
    day the itinerary no longer has (and day-less legacy rows), also on an all-skipped run (C6); the legacy path (no
    `Day N —` headers, random ids) soft-deletes the tour's earlier atoms after a good read (L1). An atomize that changed
    atoms drops the T7 Debate brand-fit cache rows (`acp_shared.debate_brand_fit_cache`) of the tour's Segments and
    the Routes through them (T13); the next Slate re-judges them. AA-1081 (S278): only the Segments whose label or
    member atoms differ before / after the segment stage (`export.handler.segment_states` / `changed_segments`); a
    re-read that gives back the same atoms (a new version of the same days, a recompose) keeps every score.
  - **Accepted score pointer** `published_tours.quality_score_id` (FK to `silver_aa_internal.quality_scores`,
    ON DELETE SET NULL, CIS 268) = the published version's latest score row (`evaluated_at DESC, id DESC`).
    Export sets it with that rule; a revalidation (`_revalidate_tour`, `revalidate` job) writes the new score
    row, re-points the published tour and only then deletes the old rows, in one transaction (AA-983). The
    published `quality_score` number stays as exported. Rows left NULL before AA-983:
    `scripts/aa983_backfill_quality_score_id.py` (DRY RUN default, `--apply --expect N`).
  - The admin **Master Content** page reads `GET /admin/tenants/{aa_internal}/details`
    (`admin.py::get_tenant_details`, internal branch). AA-718 added server-side
    `page`/`page_size`/`sort`/`sort_dir`/`country`/`master_status`/`score`/`search` to it (the
    tour list was a client-side filter over a fixed `LIMIT 200`, which dropped the newest rows
    past 200). The summary now carries a filter-aware `catalog_total` + `avg_quality` (true
    `COUNT`/`AVG` over the same WHERE), a 30-day-windowed `llm_cost_window_usd` (reconciles with
    Cost Operations) alongside the all-time figure, and a `pagination` block. Each pipeline run
    gets a read-only derived `display_status` (`_derive_run_display_status`): a stored
    `ingesting` run that is actually finished shows **completed**, an ingest-only batch shows
    **ingested**, a partial one shows **running** — the stored column is never written from this
    GET. One-off backfill (S211) recomputed `pipeline_runs.tours_passed` from the real published
    count (26 rows were stale); it did not touch any run `status` (15 of 17 `ingesting` runs are
    genuinely not-yet-run, 751 tours still at `pipeline_status='ingested'`). The tenant (B2B)
    branch of the endpoint is unchanged.
- **A4 — Cross-Tenant Oversight**: admin-side supervision OVER tenant-published content — Trust
  Ramp (graduated autonomy per tenant) and the force-unpublish safety net (`admin_a4.py`). Not
  part of the A0→A3 production line; it watches T-series output instead.

### T-series, old (Tenant rewrite tour, T0-T4)

- **T0 — Brand Identity**: tenant sets up their own brand voice/rules (`/portal/brand`, was `t0-brand`),
  incl. a Default Call to Action (`tenant_brand_rules.default_cta`, migration 220). The fields
  become the **brand pack** (`services/acp_shared/brand_pack.py`, AA-771) read by T8, T9 and the
  T10 judge — compiled deterministically at read time; a hand-written `system_prompt` still heads
  the judge rubric, otherwise the rubric is compiled from the T0 fields (no Adventure Asia
  fallback for tenants).
  - **CTA review claims (AA-819 follow-up).** The tenant's CTA is published word for word (F19
    exempts it). When a tenant saves one that claims reviews or ratings (T0 default CTA, or the CTA
    typed for one T9 write), the save still happens, the response carries `cta_warning`, the portal
    shows a plain warning (live via `POST /v1/content-writing/cta-check`), and AA admins get one
    bell row per tenant + CTA text (`tenant.cta.review_claim`). Rule:
    `authenticity.cta_review_claims()`; writer: `services/acp_shared/cta_review_notice.py`.
- **T1 — Tour Selection / Rewrite trigger**: tenant picks an A3 (Master Content) tour and fires
  the rewrite (`/portal/tours`, was `t1-rewrite`; `PoolTab.tsx`'s "Rewrite" button).
- **T2 — Rewrite (execution)**: the LLM rewrite of the picked tour into the tenant's own brand
  voice. It has no dedicated UI route and runs as the durable `t2_rewrite` job on the worker (AA-652).
  - **Writer prompt (AA-792).** T2 has its own writer prompt: `prompts.T2_SYSTEM_PROMPT` plus a
    CLIENT BRAND block (`brand_pack.render_brand_block`), assembled by
    `batch_prompt.build_t2_system_prompt`. These builders are chosen only when `is_tenant_rewrite`.
    The user prompt uses `build_rewrite_prompt(voice="tenant")`. The tenant's core idea
    (`tenant_brand_rules.core_idea`) is the lens for the overview and each day's framing.
  - **Shared rules.** T2 keeps A1's truth and structure rules as the same prompt pieces: structure,
    source fidelity and the meta band. It drops A1's neutral catalogue voice. The A1 prompts are pinned
    byte-for-byte by `tests/unit/test_aa792_a1_prompt_golden.py`.
  - **Signature vocabulary (AA-800).** The forbidden words come in three layers
    (`services/acp_shared/signature_vocabulary.py`): platform truth (cheap, deal, discount, book now,
    instant booking) is refused for every tenant; AA style is unchanged for A1 but, for a tenant, stock
    superlatives (pristine, stunning, breathtaking, magical, paradise) are allowed once per tour (a second
    one is FORBIDDEN_WORD) and positioning words (curated, refined, tailored, bespoke, luxury) are allowed
    when the tenant declares them; the tenant's own list is always refused. A tenant declares a word in its
    T0 text (brand type, core idea, voice, style guide, on-brand example; not in a negated clause) or in
    `tenant_brand_rules.custom_validators.signature_words`. T2 prompt rule 1, validate, the strip, flag_fix,
    T3 and the admin T2 drawer all read the same layered list (`forbidden_words.state_forbidden`); A1 keeps
    `VALIDATE_FORBIDDEN`. Evidence rule: a service-level claim ("a luxury lodge", "a tailored itinerary")
    the source does not support is a T3 grounding finding (sentence repair), for every tenant. Rule 1
    nudges the writer to use a signature word where the source states that level (it names the source
    wording per word, the same roots the evidence rule accepts) and to leave it out everywhere else:
    source fidelity first.
  - **Brand loading.** The brand is loaded once, through `brand_pack.fetch_brand_pack_conn` (AA-771).
  - **Itinerary nudge.** The length nudge keeps the draft's tenant voice. T2 flag-fix repairs speak as
    the tenant's editorial fixer.
  - **Writer stage.** `t2_generate` (Sonnet).
  - **Judge.** `t2_judge`. The gate is chosen by env `T2_JUDGE_GATE` (`brand_fit_mission` default,
    `brand_fit`, `combined`; `services/content_generation/judge_node.py`). Default: judge_score =
    brand_fit, capped at 6.0 when the tenant's own mission (core idea) is absent from the draft; a
    tenant without a core idea is never mission-capped. Cross-brand distinctness is logged in
    `llm_call_log.quality_signal` and does not gate.
  - **Judge repeats (AA-801).** Env `T2_JUDGE_REPEAT`: `single` (default) = one call, plus the AA-714
    median of 3 only when the first brand_fit lands in 6–8; `median3` = three parallel calls on every
    draft, median brand_fit/distinct and majority mission (a tie counts as absent), raw calls logged in
    `quality_signal.judge_runs`. `median3` stays off until the AA-801 measurement is in. A call that
    was billed but then failed (malformed JSON) is dropped from the verdict and gets its own
    `billed_unusable` row (AA-927); `judge_runs.failed_cost_usd` is informational — that cost is already
    in its own row, never add it again. When all three fail, each billed call still gets a
    `billed_unusable` row (as does a single failed call). Both repeat paths (AA-714 and median3) write
    one row per call since AA-927 — the verdict row carries run 1's cost, the rows of one verdict sum to
    the verdict's cost. Each call samples the stage-route shadow on its own, so a
    route with a shadow sees up to 3 shadow calls per judgement in `median3`.
  - **Rubric anchors (AA-801 option b).** Env `T2_JUDGE_ANCHORS` (`on`/`1`/`true`; default off) adds
    tenant-specific score anchors (what a 4 / 6 / 7 / 8 / 10 looks like, built deterministically from the
    T0 brand pack: name, core idea, audience, voice, on-brand example, forbidden words) and turns
    mission_present into a yes/no test that only counts with a verbatim itinerary quote
    (`mission_evidence`, >= 4 words), checked in code against the itinerary digest the judge saw — a
    paraphrased or invented quote overrules the yes (`evidence_verified=false`); both are logged in
    `quality_signal` when on. The yes bar is the overview + at least half of the days reflecting the
    core idea (the brand_fit 7 anchor's threshold, so an anchored 7 is not mission-capped; the
    un-anchored test keeps "most days"); the quote only has to show the idea in one day, and is asked
    for before the verdict. `evidence_verified` only proves the quote exists in the digest (it catches
    invented quotes), not that it reflects the idea. Combines with `median3`; A1 is untouched.
    Parsing for every judge caller (A1 s1_judge, T2, Debate, flag on or off): a boolean answer given
    as a string counts as yes only for true/yes/y/1 — "false"/"no"/"partially" are no; JSON bools and
    a missing key (default yes) are unchanged.
    The judge model itself is only changed through the `t2_judge` stage route; `score_brand_fit`'s
    `model_override` is refused on any stage but `adhoc_*` (measurement scripts).
  - **Best draft wins (AA-793).** Env `T2_BEST_DRAFT` (default on): a tenant rewrite runs the graph
    variant `build_graph(t2_drafts=True)`, which keeps the highest-scoring judged draft
    (`t2_repair.track_best_node`) and saves it at the `hitl` exit instead of the last one. Env
    `T2_TARGETED_REPAIR` (default on): a retry revises that best draft with its own feedback
    ("YOUR PREVIOUS DRAFT (revise it…)") instead of a blind re-roll. Both off = the old graph. A1
    never takes this variant. Judge rows log `quality_signal.attempt` / `revision`.
  - **Brand-audit fallback.** When the judge did not run, the audit uses the tenant's own rubric
    (`compile_rubric`), never Adventure Asia's identity.
  - **Stored status.** A version scoring < 7.0 (or escalated by T3) is stored `needs_review` — an
    admin signal only; tenants see it through the tenant projection (see **Tenant projection**).
  - **Live view and billing.** The tenant watches the rewrite live (streamed text, AA-637). Each
    rewrite counts against the plan quota; over-quota rewrites are billed (AA-640).
  - **S271 W0 interim profile** (`services/content_generation/t2_interim.py`, switch `T2_INTERIM`, default on): the
    t2_judge is report-only (score = validate's), no Jev tie-break, no median-of-3, no revalidate judge, no T3
    full-graph rewrite round, status `ai_generated` only with validate's score over the line and no T3 hard code,
    no copy of the Master's `quality_score` (NULL), em-dash fix inside T3 before every check. An **elephant-riding**
    tour (Master text or raw source) is `policy_hold` v0 (D8): Master text verbatim + code-fitted SEO title / meta,
    0 LLM calls, `needs_review` (the agency still receives it), `judge_breakdown.policy_hold`, a review row.
  - **AA-1049 W3 linear orchestrator (opt-in, default off)** (`services/content_generation/t2_orchestrator.py`,
    switch `T2_ORCHESTRATOR=linear`; unset / any other value = the graph + T3 path above, byte-equal to it): one
    plain sequence (write -> code fixes -> verify -> route by the `t2` rows of
    `services/acp_content_writing/route_table.yaml` -> sentence repair / meta regenerate / one field regen ->
    select best -> one report-only judge call -> final contract -> save) reusing the graph's and T3's own steps (no
    prompt change), a per-version budget (writer <= 2; every billed call counted on the pending version's
    `generation_meta` before it is sent, its cost right after, with the best candidate) so a job retry never pays a
    call twice and resumes without a new write, and a stricter status (any enforce finding left = `needs_review`). Dry
    store seam `t2_store.py` (store, review row and checkpoints; no DB write); free replay
    `scripts/quality/replay_aa1049_t2.py` (declared / undeclared / MISS buckets against a replay recorded on main).
  - **Per-job trial of the W3 switches** (`services/quality/trial_flags.py`): a `t2_rewrite` / `t9_write` job
    payload may carry `trial_flags` (`T2_ORCHESTRATOR`; `T9_SEO_BUILDER`, `T9_REGRESSION_GUARD_MODE`, only the
    values the switch knows). Honoured for an audit tenant only (`shared.tenants.kind = 'demo'`, not the pilot);
    anything else runs main's path. Scoped to the job's task (a ContextVar), never the worker env; the job result
    records `trial_flags` (requested / applied / refused). No router sets it: paid trials enqueue it by script.
- **T3 — QA Gate**: an automatic validate→repair loop (max 2 repairs) on T2's output. There is no
  manual tenant approval step: `status='approved'` is T3's own automatic result, not a button.
  - **Blocking and repair (AA-639).** T3 blocks only on `_HARD_BLOCK_CODES` and passes the specific
    failure feedback into the repair.
  - **Deterministic fixes.** An over-long SEO title is fixed with `fit_seo_title()` instead of a tour
    rewrite. Since AA-794, seo_meta gets the same deterministic fit as the graph
    (`fit_seo_meta_final`) before a length code can trigger a rewrite: a sentence-boundary salvage or a fact
    clause (AA-925: no clause / word cut any more). The fact clause never adds an "in <country>" tail on a
    multi-country tour. A meta it cannot land gets ONE meta-only LLM regenerate (`_t3_meta_regenerate`,
    `t2_flag_fix`, under the repair contract) before a length code costs a whole-tour rewrite.
  - **Best draft and targeted repair (AA-793).** Every checked draft is a candidate: a clean one
    returns at once; when rounds run out the best candidate (clean, then score) is saved, not the
    last, and each round repairs the best so far. With `T2_TARGETED_REPAIR` a grounding-only failure
    gets one sentence repair (`grounding.repair`, stage `t2_flag_fix`, Haiku) instead of a rewrite;
    a structural failure gets a revision of the draft with the T3 findings plus the judge feedback.
    `job.result.t3` records the chosen attempt and every draft's score.
  - **Background facts (AA-793).** Env `T3_BACKGROUND_FACTS` (default off): the numeric check
    accepts place background (`acp_produce/t3_background.py`): past years, centuries and elevations
    >= 1,000 m of a named place, never in a first-person, accommodation/service or climb/gain
    sentence; durations, times, distances, prices and counts stay violations (writer source
    fidelity rule A/B). A bare 4-digit year needs a year cue before it and never a comma, plural
    noun or currency after it; a metre figure next to a walking / route word is a distance.
  - **Cost.** A typical long tour takes 1-2 writer calls, about $0.10-0.18 (previously 4 calls, about
    $0.31).
  - **Jev clearing (AA-699).** For Jev allow-listed tenants, each numeric hit is first checked with
    `a1_claim_supported` against the master content (stage `t3_grounding`). A confident "supported"
    verdict clears the hit without a rewrite.
  - **T2 judge tie-break.** Near the retry line it asks `a1_brand_fit` (stage `t2_judge_tiebreak`,
    shadow).
  - **Other tenants.** `skipped`; T3 is unchanged.
- **T4 — Pool**: the tenant's own rewritten-tours pool (`/portal/tours/mine` "My Tours", was `t4-pool`) — a T3-approved tour
  here is what a tenant can build T7+ content from.

### T-series, new (per-tenant social content, ported from Ms. Thư's `aa-social-media`, T5-T11)

- **T5 — Atomize**: **historical label, superseded.** Originally a tenant-triggered, per-tenant
  atomize step; AA-526 (05/09/2026) moved atom generation to A3 (platform-wide, admin side, see
  above) and removed the tenant-triggered endpoint entirely. Do not build anything new against
  "T5" as a tenant-facing stage.

> **⚠ Naming trap — atomize is A3 platform only (Nghiệp, S224).** A tour is atomized **once, when
> it is published to Master** (`a3_atomize` job from `process_export`), or when an admin re-runs it
> (`POST /admin/atoms/atomize`). Tenants **never** atomize: a tenant rewrites a tour (T2/T3) and then
> uses the **platform** atoms, Segments, routes and hubs of that tour to plan topics and angles and
> write social content. Adding or removing a tour only **recomputes** Segment/Score/Route/Hub — it
> does not re-atomize. Names that still say otherwise are leftovers from before AA-526:
> `services/acp_produce/tenant_pipeline.py::run_t5_atomize(tenant_id, …)` (only ever called with
> `"platform"`), the LLM stage key `t5_atomize`, and the `tenant_tour_version_id` column of
> `atomize_day_fingerprint` (holds `generated_content.id`). Verify a pipeline claim by grepping the
> real callers, not by reading names (S224 lesson). Rename tracked on AA-757.
- **T6 — Atom Curation**: **historical label, superseded.** Was a tenant-facing atom star/delete
  UI (`/portal/t6-atoms`); removed at AA-527, replaced by the admin-only `/admin/atom-curation`
  page (platform-scope atoms only — this is exactly the page AA-527 built into the wrong role
  the first time, which is why this glossary exists).
- **T7 — Slate** (`/portal/create/social` "Social Posts" + `/portal/create/article` "Articles", was `t7-planning` "Social Content"): the tenant's ranked,
  per-Channel list of Subjects to pick from, read from the platform-wide Score/Route data (see
  Slate above). The older slot-grid UI on this page was removed (AA-519). Debate's brand-fit check (rank only
  since AA-1079) asks the Jev Question `t7_topic_fits_brand` before the Luna call on a cache miss (AA-700, shadow).
- **T8 — Angle Gate**: Goal + Angle selection (`/portal/create/write`, was `t8-angle-gate`; one continuous wizard
  covering T8 and T9 together — no separate T9 route). Each PAA answer an Angle claims is checked
  with `t8_angle_answers` before the ranking (AA-700, shadow). S234 (AA-771): an Angle's
  `best_final_style` is shape + which seed details to lead with — never a voice, an opening line or an
  audience other than the brand's; `sanitize_best_final_style` fixes those in code ('Open with X' →
  'Lead with X', a literal first line or a trade-facing sentence is cut) before the Angle
  is saved (and again on stored Angles when a non-blog T9 prompt is built). A Route request writes its
  Angles from a digest of the whole walk (day order, fact counts, ANCHOR CANDIDATES / THIN MOMENTS); an
  Angle anchored on a thin moment ranks last, and the call is retried only when all 3 are thin.
- **T9 — Write**: Piece generation, fires automatically the instant an Angle is chosen; the
  text streams live to the tenant (AA-637). The seed (`atom_seed.py`) carries the atom's
  **Place Card** (AA-772: display name, locality/region/country, naming note, checked background
  facts — `acp_shared.place_card`, drafted by `a3_place_card` + checked by `a3_place_card_check` in the
  T8 job (request atom) and the `place_cards_build` job — T9 only reads cards (S234). One draft per
  place: free rule skip (unnamed / logistics places), atoms grouped by folded place + country, reuse
  through `place_card_alias` / core name, misses cached in `place_card_miss`, and an atom left for a later paid
  draft marked `miss_reason = 'needs_draft'` (S262); a generic name such
  as "City Palace" keeps its locality in the key and is never reused across tours by name; AA-784 S253: region is
  a first-level administrative division or empty and locality the settlement the place is IN or empty —
  `place_cards.clean_identity` clears geographic / trekking areas and the nearest town of a remote point before the
  check (round 3: remote = protected areas, summits / peaks / mountains / passes, camps / huts, glaciers, lakes
  outside a first-level city; a locality equal to the region is kept; a first-level city (`FIRST_LEVEL_CITIES`) is a
  right region, also of itself; a border-spanning place keeps a field only as both sides "A / B"; a person's life
  dates in parentheses are dropped from facts, at seed time too), and the check's `locality_ok` / `region_ok` clear a field instead of rejecting the card; present-tense
  status / administrative-role and authorship facts are never kept (`unverifiable_claim`, also applied to older
  cards' facts at seed time unless verified; the identity rules likewise to older cards' `Location:` line); a
  rejected card stored under a compound country (pre-AA-990) blocks a draft naming it (a hold only an admin
  lifts); `place_cards_build` with `{"card_ids": [...]}` re-drafts named cards
  in place — admin `POST /admin/content-quality/place-cards/rebuild`),
  `Day N`, same-country sibling evidence labelled "other trips", and a SCOPE line (which days and
  places the piece covers). S234: sibling lines only when the card has <3 facts (≤2, none naming
  another place); a Route's moments are in day order, skip Segments ranking excluded, carry no
  siblings, merge same-place moments into one block, and a moment with <3 facts is tagged thin (AA-789 tier 0: the
  thin note says "name it in passing" with no count or sentence shape, and the card's naming note reaches the writer
  only as a `Naming:` line of names, `atom_seed.naming_line`, never the note's prose). Facts Entries are filtered by `facts.country`; S233 removed the
  per-fact Jev call (no Jev on the T axis). S234 round 2 (`acp_shared.seed_canon`): a Route seed
  keeps one value per fact across its cards (same measure, different number → a verified card wins,
  then the place's own card, then the place with more moments, then the first; numberless
  contradictions are kept), an overnight moment loses its card when its region matches no other
  moment of that day (the card guessed the famous region), and within a day moments run
  overnight-last, then in A3 extraction order. The SEO keyword's product words (retreat, cruise,
  trek … — `acp_content_writing.seo_grounding`) must be in the seed, else the next candidate or the
  keyword cut to its place words; a meta's product nouns must be in the seed or the body (SEO fit
  and F4).
- **T10 — Quality Gates**: inline within T9 (not a separate request) for the automatic Gate run.
  Word layers (AA-800, same module as T2): `F2_platform_truth` blocks (sentence repair),
  `F2_superlatives` blocks a second stock superlative (style only: one repair, never a hold),
  `F2_style_warn` warns on a single superlative or an undeclared positioning word; the writer prompt has a
  WORDS line built from the brand pack; an unsupported service-level claim is an `F17_trip_fact` finding.
  No Jev on T10: the AA-701 shadow questions were turned off in S234 (no signal) and their
  observer removed; migration 194's `t10_gates` question rows stay at `mode=off` as history.
  `/portal/content` (was `t10-review`) is now **My Content** (AA-614): the tenant's pieces with no gate/held
  details, displayed text is copy-locked (select/copy/right-click blocked, with a "Use Export"
  hint; the edit textarea stays editable). AA-669 migrated it to the UI-kit DataTable
  (columns/filters by channel·status·goal/sort/saved views/CSV) with a right-side drawer for the
  piece (content + context + flags + inline edit + exports), and bulk actions (export selected,
  publish selected eligible pieces) via react-query.
- **T11 — Publish / Export**: tenant-facing publish to WordPress (blog Channel) and a Facebook Page
  (`/portal/content/publish`, was `t11-publish`); the other Channels have no publish step built yet. The router
  `api/routers/v1_publish.py` keeps HTTP, auth, the tenant pool, Secrets Manager and the `publish_log` claim / finish
  and daily limit; the per-channel dispatch (integration type, adapter, post building, remote lookup and delete) is
  `services/acp_publish/dispatch.py` (AA-765). Blog markdown is
  converted to an HTML fragment before it is sent to WordPress (AA-613 fixed raw `##`/`**`
  appearing on the site). Export has two modes: a full HTML document and a bare HTML fragment
  for pasting into a CMS. Every export and publish writes an audit row.
  **AA-1167 (OWASP LLM05):** nothing AI-written leaves unsanitized. `services/ai_safety/html_sanitizer.py` rebuilds the
  Markdown render from an allow-list (`export.render_markdown_body`: the WordPress body, both HTML exports and the
  subject gate's link check; script, iframe, style, svg, form and `on*` handlers go, links only http(s) / mailto / tel /
  relative, no `img`) and strips markup from plain text (`sanitize_plain_text`: the Facebook message, the plain-text
  export, the WordPress title and meta description). The allow-lists are security invariants, not settings;
  `tests/unit/test_aa1167_publish_guard.py` fails on a Markdown render or a post builder that bypasses them.
  **AA-1167 (OWASP LLM01):** the writer calls of the S1 v2, T2 and T9 flows (the first-draft writers and their repair
  writers: T9 sentence repair, extend, SEO fit and FAQ; the S1 / T2 / T3 grounding repair, flag fix, meta re-repair
  and itinerary nudge) can fence every quoted untrusted value (supplier source, Facts Entries, web search data) in
  `<untrusted_data>` tags with one system line saying it is data, never instructions
  (`services/ai_safety/prompt_fence.py`), behind the setting `cis.llm.prompt_data_fence` (CIS 467, off = the prompts as
  before; prompt versions s1_generate v4 / t9_write v9, same CIS 467). The text a repair call is asked to rewrite (the
  draft sentence, section or meta) stays outside the fence. Judges, filters and extractors (brand audit, F9 / brand
  fit, FAQ filter and answer fit, seed adjudication, source facts, place cards) are not fenced yet. The trap set
  (`services/ai_safety/trap_set.py`, paid run `scripts/aa1167/trap_run.py`) measures the writers before it is turned on.
  **AA-1172 (OWASP LLM01, Facts Entries):** the fence alone did not stop an instruction planted in a fact, because a
  fact is quoted as "real, verifiable source material" and also feeds the grounding corpus. The Facts instruction guard
  (`services/ai_safety/fact_guard.py`) checks every fact for text written to the writer (English and Vietnamese,
  matched after Unicode normalisation; Vietnamese with its tone marks kept, so "người Việt" is never "người viết") at
  the one read choke point `facts.prepare_facts` (called by `fetch_facts_for_writing`, so T9 and T2 both get clean
  facts). A fact that addresses the writer (a chat role line, a note to the writer, a request to ignore the rules,
  prompt talk), hides text (Unicode tag text, an invisible character from the default-ignorable set or a combining mark
  between two letters, a word mixing Latin letters with look-alikes of another script, a run of variation selectors), copies the facts-list structure (a `[Fact id=]` head, a `Source:` line),
  whose title or source line breaks a rule (or whose source line describes headings / the FAQ), whose directive is
  split across lines or between title and body, or whose body is left empty, is left out and recorded in the FactSheet
  with reason `fact_instruction`; a sentence that only asks to put words into headings / the FAQ / the post is removed
  alone. Invisible characters that carry nothing (bidi controls, zero-width spaces, a soft hyphen inside a word) are
  removed from every kept fact; the character layer lives in `services/ai_safety/fact_text.py`. Stored writer
  snapshots replayed later (ground-truth review, writing-memory eval and replay,
  sentence repair, and `service.grounding_from_writer_input` behind the admin sentence suggest, the admin edit checks
  and the writing-memory fact filter) read the cleaned block through `fact_guard.stored_facts`. Switch:
  `cis.llm.fact_instruction_guard` (CIS 472, default **off**: a best-effort pattern filter, NOT a security boundary;
  the prompt data fence and the writer validators stay the primary controls; an administrator turns it on in
  Platform › Settings only after a trap run; a fact with no match reaches the writer unchanged apart from invisible
  characters). Rules match on a Latin confusable skeleton. Known residual classes: visible separators / markup in place
  of spaces, a directive split between the body and the provenance, forged entries in replayed snapshots, scripts
  other than Latin / Greek / Cyrillic. The patterns are security invariants.
  The trap set has six T9 fact traps (body, inline role line, polite request, title, source line, a paraphrase) built through the
  live `prepare_facts` + `format_facts_block`; `trap_run.py --guard both --stage adhoc_aa<issue>` adds the guard-off
  control. Not built yet: a post-write check that refuses names absent from the source (the planned second layer).
