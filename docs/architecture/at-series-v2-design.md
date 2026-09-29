# A/T Series v2 — design before the single clean rerun

Status: **Draft for review** (S203, 29/09/2026). Author: Claude Code with Nghiệp.
Sources: `CONTEXT.md`, the code on `main` after PR #481, the Jev per-stage map in AA-634 (S196–S198 comments), and ADR 0001 (root) / 0005 / 0006 (App).

## 1. Decisions this design starts from (Nghiệp, 29/09/2026)

1. **Order:** A/T redesign → A/T fixes → UI/UX v2 complete → **one** clean rerun (AA-594/599/600/601). No paid rerun waves before that. A ~3-tour smoke right before the full run is still allowed.
2. **Jev is used for real** in that rerun: it may accept or reject, not only log in shadow.
3. **We calibrate Jev ourselves.** The agent proposes labels and Nghiệp spot-checks them. We do not wait for Ms. Thư.

## 2. Constraints found while reviewing (need a decision or an action)

| # | Constraint | Consequence |
|---|---|---|
| C1 | **No Jev key in Secrets Manager** (acc2 has no `*jev*` / `*typesafe*` secret), and there is no Jev code in the App. The key exists only in Nghiệp's file (S198). | Nghiệp creates the secret (proposed name `aa-cis/dev/typesafe`). The agent must not handle the key. |
| C2 | **DPA / ZDR with TypeSafe is not confirmed** (AA-660 scope). ZDR is enterprise-only (AA-634, S196). | Jev may read **platform** data (raw/master tours, atoms, keywords, PAA, catalogue facts) now. It must **not** read **tenant** data (tenant rewrites, pieces, tenant brand profiles) until the DPA is confirmed. See §4 for how this splits the work. |
| C3 | Ms. Thư's own blind check found **51% agreement**. The disagreement was one-directional: Jev cut what the marketer kept. | Enforce only where a calibrated threshold is confident. Anything in the grey zone keeps today's behaviour (§5). |
| C4 | Her repo's ADR 0023 is "flag, never block". Our decision is to enforce. | Enforcement is limited to the confident zones. If Jev is unreachable, the stage behaves as today (fail-open to the existing rule), never "reject". |

The clean rerun rebuilds the **A-series** (A0 → A1 → A2 → A3 → atomize → Segment / Score / Route / Hub). T-series content is produced per tenant on demand and is not part of the rerun. So C2 does not block the rerun: every Jev point the rerun needs reads platform data only.

## 3. Jev decision layer (shared by every stage)

Extends AA-660 from shadow-only to enforce.

- **Seam:** `shared/llm_client/decide.py` — `decide(stage, subject_key, state, questions) -> Decision`.
  - Question types: Noul / Choice / Score.
  - Plain HTTP to `POST https://api.typesafe.ai/v1/systemone`; the key comes from Secrets Manager.
  - Timeout 3 s, retry on 429/529.
  - Same place and rules as `generate()` / `embed()`: `test_aa685_no_raw_model_calls.py` is extended to forbid raw TypeSafe calls outside `shared/llm_client/`.
- **Ledger:** `shared.decision_log` — `stage`, `subject_key`, `question_key`, `probability`, `zone` (`accept` / `grey` / `reject`), `mode` (`off` / `shadow` / `enforce`), `threshold_version`, `model`, `latency_ms`, `cost_usd`, `job_id`, and `outcome` (what the pipeline then did). Every probability is stored, so thresholds can move without paying again.
- **Thresholds and mode per stage** (in `shared.llm_role_config`, or a sibling table, edited in admin Settings): `mode`, `accept_floor`, `reject_ceiling`, `threshold_version`.
  - `accept_floor` = the probability at or above which Jev's "yes" is trusted.
  - `reject_ceiling` = the probability at or below which Jev's "no" is trusted.
  - A stage cannot be set to `enforce` without a calibration record (§5).
- **Cost:** `llm_call_log` row per call (provider `typesafe`, role `validate`; the role CHECK stays as is) and a cost guard provider `jev`. At $0.042 per 1M input tokens a full A-series pass is cents.
- **Failure:** timeout or error → `zone = error`, and the stage runs its existing rule. It is logged, never silent.

## 4. Stage by stage

"Now" is verified on current `main`. "Jev" letters follow the AA-634 legend:
- **P** — prefilter before a spend;
- **G** — decide the grey zone of an existing rule;
- **V** — verify an LLM's claim about itself;
- **R** — replace a classification-only call;
- **S** — select among code-built candidates.

### 4.1 Platform stages (A-series) — Jev allowed now, needed for the rerun

| Stage | Now (verified) | Change | Jev | Priority |
|---|---|---|---|---|
| **A3 research — keyword belongs** (`segment_research_batch._propose` → `_buy_volumes`) | Haiku proposes up to 4 keywords per place; all are bought (DFS volume tasks) unchecked. | Before buying: a Noul "Is this keyword about *this particular place*, not the kind of thing it is?" (Ms. Thư's ticket 03). Reject → not bought. | **P**, enforce | **1** — saves DFS money on every research run |
| **A3 research — idea filter** (`_buy_suggestions`) | Every idea with volume is stored (S203 probe: hotel/resort names). Tasks with fewer than 5 seeds are skipped since #481. | Noul "Is this a traveller searching to experience the place (not lodging or booking)?" before storing. | **S**, enforce | 2 |
| **A3 PAA landing** (`atom_ranking.land_questions_for_segment`) | A question is shortlisted by sharing one word with the place. It then always lands on the nearest atom **within the same Segment**, with no distance cut-off, so every shortlisted question counts toward `questions_count` (a Score axis). | (a) Rule: never land on a transit atom (ticket 01, no Jev). (b) Noul country scope: "Is this question about a place outside `<country>`?" (ticket 04). (c) Noul landing belongs: "Would someone asking this be well served by an article about `<place — action>`?" (ticket 05). Only accepted landings count. | **G**, enforce | **1** — directly changes Score, which drives the Slate and the TripPlanner |
| **A3 demand ownership** (`atom_ranking.compute_demand`) | A Segment claims the strongest keyword that shares one non-generic word (e.g. "Kyoto" 165k credited to "Kyoto incense-making"). | Noul "Does this keyword's demand belong to this moment?" on the best claimed keyword per Segment × market. Reject → take the next candidate. Verdicts are stored, so reruns stay deterministic. | **G**, enforce | 2 |
| **A3 Segment matching** (`segment_matching`, Jaccard on place + verb) | Shared words over-merge (Kumano Hongu Taisha vs Kumano Nachi Taisha). | Noul "Same real-world place?" only on borderline pairs. The verdict is stored once, so `segment_id` stays stable (ADR 0002). First measure how many borderline pairs exist. | **G**, enforce once measured | 3 |
| **A3 atomize enums** (`t5_atomize`) | Haiku extracts atoms, including `activity_type`. | Jev Choice on `activity_type` only when it disagrees with `classify_exclusion` (transit matters for the landing rule above). | **G**, shadow first | 4 |
| **A1 validate soft codes** (`validate_node`: `SUBTITLE_GENERIC`, `HIGHLIGHTS_TOO_GENERIC`, `SUMMARY_OFF_BRAND`) | Fixed phrase lists, so paraphrases pass. | Noul "Generic — would fit any destination?" as a second signal. The codes stay soft. | **G**, enforce (soft codes only) | 3 |
| **A1 brand audit pre-checks** (`ITIN_MEAL_INVENTED` / `ITIN_CLOCK_TIME` regex) | Regex with a history of false positives (AA-608). | Jev only on a regex hit: "A logistics claim (meal/time), or narrative?" | **G**, enforce | 3 |
| **A1 flag_fix** | Introduces forbidden words and misses the SEO meta length, which forces a full rewrite. | **AA-641** (no Jev): deterministic `seo_meta` fit + a per-field forbidden-word revert. | — | **1** |
| **A1 judge / brand audit** (`s1_judge`, `s1_brand_audit`, now GPT-5.6 Luna via the gateway) | An LLM call on every attempt. | Keep as is. Luna is already cheap, and the judge's value is its written feedback. Revisit a Jev prefilter after the rerun. | — | later |
| **A0 column map** (`a0_column_map`, Haiku) | Runs when the COLUMN_MAP hit rate is below 50%. | Jev Choice per column over 19 fields + `none`. Low volume. | **R** | later |

### 4.2 Tenant stages (T-series) — Jev blocked by C2 until the DPA is confirmed

Not part of the rerun. The design is recorded here so it is ready; each row is built only after the DPA.

| Stage | Now (verified) | Change | Jev |
|---|---|---|---|
| **T3 grounding** (`_t3_grounding_check`) | A numeric regex; each hit triggers a full-tour rewrite (Sonnet). | Before rewriting: "Is this number supported by the source (unit conversion, sums, rephrasing)?" Accept → no rewrite. | **P** |
| **T8 `rank_angles`** | Only checks that the LLM copied a PAA question verbatim. | Noul per (angle, question): "Does this angle actually answer this question?" | **V** |
| **T9 facts** (`fetch_facts_for_writing`) | **All** platform facts + all of the tenant's facts go into every writer prompt. | Noul "Relevant to this trip / moment / angle?" per fact, before building the seed. Platform facts can be judged now (platform data); tenant facts only after the DPA. | **S** |
| **T10 F8 framework** (`t10_judge`, warn-only since AA-613) | A GPT call on every attempt for a warn. | One Noul per rubric item. Replaces the call. | **R** |
| **T10 F9** (`F9_cta_fact` block + `F9_brand_style` warn) | One judge call. | Style half → Jev. CTA/fact half: Jev prefilter, GPT on doubt. | **P** |
| **T10 cannibalization** (cosine ≥ 0.92 blocks) | A hard threshold; a block costs a rewrite. | Jev confirms at ≥ 0.92; also checks 0.85–0.92 for misses. | **G** |
| **T7 Debate brand-fit** | `score_candidate_fit` (S203): Luna judges the topic. | Keep. Candidate topics are platform data, so a Jev prefilter is possible later; not needed now. | — |

Non-Jev T-series items already planned: AA-668 (Writer panel), AA-667 (live admin rewrite), AA-670 (portal audit).

## 5. Calibration (per question, before `enforce`)

1. **Sample:** ~200 items per question from real Dev data, stratified by the rule's current verdict (and by Jev probability once a shadow pass exists). For example, landing belongs = 200 (question, Segment) pairs from `atom_matches`.
2. **Shadow pass:** run Jev on the sample and store the probabilities. Cost is cents.
3. **Labels:** the agent proposes a label per item with a one-line reason. Nghiệp reviews at least 20% at random plus every item where the agent and Jev disagree. If Nghiệp overturns more than 10% of the reviewed agent labels, the question is re-posed and the sample relabelled. Agent labels are **not** independent human labels; the report says so.
4. **Floors:** choose `reject_ceiling` so that rejects have ≥ 95% precision against the labels, and `accept_floor` the same for accepts. Everything in between is `grey`, which keeps today's behaviour. If no floor reaches 95%, that question stays in shadow.
5. **Record:** `docs/calibration/<question>.md` with the sample query, label file, confusion table and chosen floors, plus `threshold_version` in the DB.

Ms. Thư's lesson: pose each question so both sides name the same kind of entity. Her 51% came from a mis-posed question.

## 6. Build order

| Step | Work | Existing issue | New issue? |
|---|---|---|---|
| 1 | Jev seam + `decision_log` + per-stage mode/thresholds + cost/log | AA-660 (re-scoped to enforce) | — |
| 2 | Calibration method and the first 3 questions: keyword belongs, landing belongs, country scope | AA-661 + AA-643 (merge the eval into it) | — |
| 3 | A3 research keyword gate + idea filter | — | yes |
| 4 | A3 PAA landing: transit rule + country scope + belongs | — | yes |
| 5 | A1 flag_fix fixes | AA-641 | — |
| 6 | A3 demand ownership + A1 soft codes + brand audit grey zone | — | yes (1 issue) |
| 7 | Segment borderline measurement → decide | — | yes (measure first) |
| 8 | UI v2 | AA-662 → 664 → 666 → 667 / 668 → 669–672 | — |
| 9 | Rerun runbook + 3-tour smoke + full rerun | AA-653 → 594 / 599 / 600 / 601 | — |
| — | T-series Jev (§4.2) | — | after the DPA |

Every Jev stage also needs an admin view: `decision_log` counts per zone, and drill-down per subject (Nghiệp's rule: every backend feature observable in the UI).

## 7. Open questions for Nghiệp

1. **C1:** create the secret `aa-cis/dev/typesafe` with the Jev key.
2. **C2:** who contacts TypeSafe about the DPA? Until then, T-series Jev waits.
3. Approve creating the new issues in step 3/4/6/7 (project "LLM Model Gateway & Decision Layer", after checking its 50-issue limit).
