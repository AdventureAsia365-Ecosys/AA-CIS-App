# AA-1039 — R6 slice 3: Writing Lab per stage and the AI & spend group (lane r6-writing-lab, S267)

Plan: root ADR 0019 d13, clarification TL-13 (`regroup_lab`), root audit
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §6.2 (IA table), §6.3 (AI gateway), §6.4 (Writing
Lab). Slices 1 and 2: `AA-1039-r6-admin-ia.md`, `AA-1039-r6-gate-fanout.md`.

## Decisions

- **A Lab stage is a writing-memory writer.** Every learning table (`writing_signal`, `writing_rule`,
  `writing_memory_release`, `writing_memory_shadow`, `eval_golden_item`, `eval_item_result`, `prompt_eval_runs.writer`)
  CHECKs the writer against the same four values `a1`, `t2`, `t9_blog`, `t9_social`, so those are the four stages.
  URL slugs (`lib/writingLab.ts`): `a1-s1`, `t2-rewrite`, `t9-blog`, `t9-social`. §6.4 also lists `place_card` and
  `tp_compose`: they have no row in any learning table (the CHECKs refuse them), so they are not stages yet; adding
  them is "lift the freeze per stage" work, not a regroup.
- **One read endpoint, no new logic.** `GET /admin/writing-memory/lab/{writer}` (`services/writing_memory/lab.py`) reads
  the five sections the issue names over existing tables: samples (active golden sets), labels (golden label verdict
  / source, signal fact status / reason / action / actor), experiments (`prompt_eval_runs` of the writer, plus A1's
  legacy `s1_old` / `s1_from_atom` regression runs; release shadow runs from `writing_memory_shadow` grouped by run;
  model shadow comparisons from `llm_shadow_log` of the writer's prompt stages over 30 days), prompt versions
  (`prompt_version` rows of the writer's prompt stages) and the eval gate (memory switch, active / newest platform
  release, releases per status, tenant releases live, rules per status, newest finished r1 / r2 / reverify / shadow
  run). No write, no job, no LLM (unit test parses the module's imports).
- **Prompt stages per writer** are a fixed map in `lab.PROMPT_STAGES`, checked against
  `shared/llm_client/prompt_versions.json` by a unit test (a renamed stage fails CI, not the page).
- **Gate state is a read, not a rule.** `gate_state()` names one of `no_release`, `memory_off`, `reverify`,
  `regression`, `passing` from stored rows. The activation checks stay in `releases.py` and the CIS 287 guard.
- **No text on the Lab.** The SQL selects ids, labels, counts and hashes only (a unit test lists every `pii:free_text`
  column of these tables and fails if one is selected). The logged `ai_ops` read of tenant text is R2's lane; the
  Writing memory tools (which do show masked text, admin only) stay one click away.
- **Each section is read on its own connection**: a missing table (code before a migration) leaves that section
  `ready: false` with no error; any other failure says "<section> could not be read" (no exception text).
- **Writing memory moved under the Lab**: `/admin/writing-memory` → `/admin/lab/writing-memory` (307, query kept).
  A static segment beside the dynamic `/admin/lab/[stage]` (Next.js matches the static one first). The API path
  `/api/admin/writing-memory/*` does not move.

## Changed

- Backend: `services/writing_memory/lab.py` (new), route `GET /admin/writing-memory/lab/{writer}` in
  `api/routers/admin_writing_memory.py`, staff POLICY entry (tenant.read, admin only like every writing-memory read),
  route snapshot regenerated.
- Tests: `tests/unit/test_aa1039_writing_lab.py`.

## Tradeoffs

- One wide read per stage (13 small queries over 5 connections) instead of reusing the six existing Writing memory
  list endpoints from the browser: the existing endpoints have no writer filter on several reads (summary, metrics,
  clusters) and none on golden sets or prompt versions; one read keeps the page to one request and the rules about
  text in one place.

## Should know

- No migration in this slice.
