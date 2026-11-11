# AA-1049 lane l28 — recompose the fixed fields of served Masters (path A-new)

S277. The S277 audit counted 708 of 844 active Masters whose fixed fields differ from what today's S1 train composes
(fact lines 417, day prose 444 as the audit counts it, seo_meta 7; no heading, day-count or title change). The tech
lead picked path A-new of the S276 master-propagation map: a code-made new version, published through the normal
path, no model call.

## Decisions

- **Code layout.** Pure part `services/content_generation/s1_recompose.py` (recompose, narrative guard, contract
  before / after), DB part `s1_recompose_store.py` (selection, decision, new version, publish), thin runner
  `scripts/quality/recompose_masters.py` run through `tools/ecs/ecsx.sh` like the audit and the elephant hold. A
  script, not a job kind: the work is $0 and fast per tour, the paid part (A3) already runs as `a3_atomize` jobs
  that the wave report sees, and no job registry / capacity line / kind list had to grow.
- **Stored text as the writer's day objects.** The stored itinerary is split with `day_parse.canonical_blocks` (the
  A3 / catalog reader) and each block becomes a writer day object for `s1_train.compose_page`. Lines that ARE the
  source day's verbatim fact lines (`s1_writer_v2.source_fact_lines`, before the DO NOT USE filter) are taken out of
  the block first: compose appends them again, so a re-run on a recomposed page is a no-op even for a source fact
  line that is not a bare "Label: value" line (otherwise it would be kept as prose and appended a second time).
- **What "narrative" means.** Name, subtitle, summary, description and highlights byte for byte; per day block the
  title (whitespace-normalised, as compose writes it) and every prose line. A "Label: value." clause at the END of a
  prose line is a fact line (the train's own `strip_writer_fact_lines` rule), so it moves out of the prose; the run
  counts these (`fact_clauses_cut`). Text before the first heading, a stored day the plan lacks, a duplicate block:
  compose would drop prose, so the Master is skipped (`narrative_changed`), never silently rewritten.
- **Day count.** It changes only when no prose is lost (e.g. a ranged heading over a day the source lacks becomes a
  single day). The audit found no day-count change, so this is the rare path.
- **Legacy-format itineraries are skipped** (`heading_parse`): A3 would re-read them whole with new random atom ids
  (map M5). The audit found none (all 844 are v2, canonical).
- **The new version** is an INSERT ... SELECT copy of the served version with only `aa_itineraries`, `seo_title`,
  `seo_meta`, `status = 'approved'`, a new `version_num` and the metadata replaced. Metadata keeps the writer's keys
  (`writer_version` stays `v2`: the narrative is the v2 writer's and `hard_block_codes` reads it; `grounding`,
  `lineage`), drops `final_contract`, `final_contract_log` and the S1 job marker `s1_job_id` (a resumed S1 job must
  not take this version for its own), sets `llm_cost_usd = 0`, `recompose_of`, `writer = "recompose-v1"` and a
  `recompose` block (fields, verdict move, report). The latest `quality_scores` row is copied with
  `failure_codes` = today's validate hard codes on the new text plus the stored non-hard codes (the audit's
  `today_codes_of`) and `validator_fn_version = 'recompose-v1'`.
- **One exit name `recompose`** for both the insert (`insert_recomposed_version`, records the contract on the stored
  row) and the publish (`process_export(contract_exit="recompose")`), listed in `final_contract.EXITS` (topology
  test). The A3 job is created by `system:recompose`.
- **Contract not worse.** Old and new verdicts are recomputed today the way the audit does (today's validate codes,
  the tenant's brand list); worse = a higher rank on publish < review < policy_hold / source_hold < degraded than
  today's OR the stored verdict, or any new blocking code. Elephant-riding hits on either side skip as `policy`.
- **Skips before compose:** source not live, a pending review (the publish would dismiss it), a newer version that is
  `draft` / `passed` / `approved` / `published` (work in flight; `hitl` / `rejected` do not hold it), an active
  `s1_rewrite` / `a3_atomize` / `revalidate` / `llm_batch` job of the tour, writer not v2, human-edited (opt-in
  `INCLUDE_HUMAN_EDITED=1`: its edited fact lines would become the source's).
- **Resume.** A newer `approved` version whose `metadata.recompose_of` is the served version is a recompose that was
  written but not published (run stopped between the two steps): the next run publishes it (`resume`), never writes a
  second one.
- **Rolled back is not resume (review fix).** A recompose version R that was published and then rolled back by staff
  (`promote_tour_version` republishes the older version, which is still `approved`) is still the newest approved
  version with `recompose_of` = the served version. Resume therefore requires that R was never published: no
  `metadata.recompose.published_at` (stamped by `apply_one` after a successful publish), no `final_contract_log` entry
  `{exit: recompose, actual: published}` (process_export step 5), and no `a3_atomize` job of R (process_export step
  3b, enqueued only after the publish committed). Any one of the three makes the Master `rolled_back`: skipped,
  counted, never republished. Three signals because the last two are best-effort writes inside process_export.
- **The wave is re-read before every Master in APPLY (review fix).** A staff pause in `/admin/waves`, a capacity
  pause (`paused_at`) or a close stops the run before the next write: `stopped: <why>`, the applied counts so far,
  exit code 4. The run is resumable, so stopping is safe; the next DRY RUN gives a new pin for the rest.
- **Wave and pin.** APPLY needs `WAVE_ID` of an open, not paused `shared.rerun_wave` whose expected set holds every
  tour of the run, and `PIN` (`count:md5` of the sorted `tour_id:served version id` of the Masters due) from the DRY
  RUN. A run never opens or closes a wave (the orchestrator does, lesson S275).

## Changed

- New: `services/content_generation/s1_recompose.py`, `s1_recompose_store.py`, `scripts/quality/recompose_masters.py`,
  `tests/unit/test_aa1049_recompose.py`, `tests/integration/test_aa1049_recompose_db.py`.
- `services/quality/final_contract.py` EXITS: `recompose` on `process_export`, two new S1 exits.
- `services/export/handler.py`: docstring names the `recompose` exit (no code change).
- `CONTEXT.md`: one bullet next to the S1 train bullet.

## Tradeoffs

- "Worse than the stored verdict" is read literally: a Master that is `review` today but stored `publish` (a rule
  changed since) stays as it is even when the recompose does not make it worse. The audit found no such Master
  (stored vs today: none), so the strict reading costs nothing now and never publishes a regression.
- Human-edited Masters are left out by default: overwriting a person's fact-line edit with the source line is the
  train's rule, but it should be a deliberate choice.
- Copying `satellite_*`, `model_editorial`, `prompt_version` and `fs_id` keeps the narrative's provenance on the new
  version; `llm_cost_usd = 0` and `writer = recompose-v1` say no model ran for it.

## Should know

- The audit's `day_prose` count (444) is not "prose this rewrites": the audit's block split counts an appended source
  line that is not a bare "Label: value" line as prose, and a fact clause cut from the end of a prose line as a prose
  change. This code classifies both as fact lines (the second counted as `fact_clauses_cut`), so its narrative guard
  compares real prose only. The first DRY RUN on Dev shows how the 708 split; a large `narrative_changed` count means
  stored pages carry prose compose would drop and needs a look before APPLY.
- Every recomposed Master is a new version: A3 re-reads every day (≈ 0.006 USD/tour, map §2) and `place_extract`
  rebuilds geo. A3 runs at concurrency 1, ~20-30 tours/h: keep a wave ≤ ~480 tours so its tail drains inside the
  AA-991 24 h window (`atoms_older_than_master`).
- `process_export` dismisses pending reviews of the tour; that is why a pending review skips the Master.
- Exit codes of the runner: 0 ok, 1 a Master failed on APPLY, 2 refused, 3 pin moved, 4 stopped (wave paused or
  closed during APPLY).
- Each APPLY that writes changes the pin of the next DRY RUN (recomposed Masters become `no_op`): re-run the DRY RUN
  before another APPLY.
