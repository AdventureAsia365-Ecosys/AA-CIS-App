# A/T Series v2 — design before the single clean rerun

Status: **Draft for review** (S203, 29/09/2026). Author: Claude Code with Nghiệp.
Sources: `CONTEXT.md`, the code on `main` after PR #481, the Jev per-stage map in AA-634 (S196–S198 comments), and ADR 0001 (root) / 0005 / 0006 (App).

## 1. Decisions this design starts from (Nghiệp, 29/09/2026)

1. **Order:** A/T redesign → A/T fixes → UI/UX v2 complete → **one** clean rerun of the whole platform plus the test tenants (AA-594/599/600/601). No paid rerun waves before that. A ~3-tour smoke right before the full run is still allowed.
2. **Jev is used for real** in that rerun: it may accept or reject, not only log in shadow.
3. **We calibrate Jev ourselves.** The agent proposes labels and Nghiệp spot-checks them. We do not wait for Ms. Thư.

## 2. Constraints (need a decision or an action)

| # | Constraint | Consequence |
|---|---|---|
| C1 | There is no Jev key in Secrets Manager and no Jev code in the App. The key is in `secret_manager/Jev` (gitignored). | Nghiệp creates the secret `aa-cis/dev/typesafe`. The agent is not allowed to read the key file. |
| C2 | The TypeSafe DPA/ZDR is not confirmed; ZDR is enterprise-only. | Jev may read **platform** data now (raw/master tours, atoms, keywords, PAA, platform facts). For **tenant** data, Jev runs only for tenants on an **allow-list** (the AA-owned test tenants, e.g. WanderLux, test-n1-flow) until the DPA is confirmed. For every other tenant, T-series stages behave as today. |
| C3 | Ms. Thư's blind check found **51% agreement**, one-directional (Jev cut what the marketer kept). | Enforce only in calibrated confident zones; the grey zone keeps today's behaviour (§5). |
| C4 | Her ADR 0023 is "flag, never block"; we decided to enforce. | Enforcement only in the confident zones. If Jev is unreachable, the stage falls back to its existing rule and never auto-rejects. |

## 3. Jev decision layer (shared by every stage; extends AA-660 to enforce)

- **Seam:** `shared/llm_client/decide.py` — `decide(stage, subject_key, state, questions, tenant_id=None) -> Decision`.
  - Question types: Noul / Choice / Score.
  - Plain HTTP to `POST https://api.typesafe.ai/v1/systemone`; the key comes from Secrets Manager.
  - Timeout 3 s, retry on 429/529.
  - `test_aa685_no_raw_model_calls.py` is extended to forbid raw TypeSafe calls outside `shared/llm_client/`.
- **Tenant guard:** when `tenant_id` is set and the tenant is not on the Jev allow-list (C2), `decide()` returns `zone = skipped` without calling TypeSafe.
- **Ledger:** `shared.decision_log` — `stage`, `subject_key`, `question_key`, `probability`, `zone` (`accept` / `grey` / `reject` / `error` / `skipped`), `mode` (`off` / `shadow` / `enforce`), `threshold_version`, `model`, `latency_ms`, `cost_usd`, `job_id`, `tenant_id`, `outcome`. Every probability is stored, so thresholds can move without paying again.
- **Thresholds and mode per question** (admin Settings): `mode`, `accept_floor`, `reject_ceiling`, `threshold_version`. A question cannot be set to `enforce` without a calibration record (§5).
- **Cost:** one `llm_call_log` row per call (provider `typesafe`, role `validate`) and cost guard provider `jev`. At $0.042 per 1M input tokens, a full pass is cents.
- **Admin UI:** a "Decisions" view (Operations): counts per stage/question/zone, drill-down per subject, and the calibration record per question.

## 4. Stage by stage

"Now" is verified on current `main`. Jev shapes:
- **P** — prefilter before a spend;
- **G** — decide the grey zone of an existing rule;
- **V** — verify an LLM's claim;
- **R** — replace a classification-only call;
- **S** — select among code-built candidates.

"Mode at rerun" is the target, provided calibration passes. Otherwise the question stays in shadow.

### 4.0 S0 / A0 — upload and ingest (platform data)

| # | Point | Now (verified in `services/ingestion/handler.py`, `shared/country_resolver.py`) | Change | Jev | Mode at rerun |
|---|---|---|---|---|---|
| A0-1 | **Is the row a tour?** | Only an empty itinerary is dropped (AA-604). POI/attraction/hotel/transfer rows with some text get in (S184 had to trash 30 POI by hand). | Choice over `multi_day_tour` / `day_tour` / `poi_or_attraction` / `accommodation` / `transfer_or_service` / `other`. A non-tour → `ingest_details` drop with reason `not_a_tour`. Grey → lands and is flagged in the Upload preview. | **G** | enforce |
| A0-2 | **Near-duplicates** | Exact `lower(trim(name)) + provider` only. The same product under a slightly different name, or a re-export from another supplier, lands twice. | Code shortlists pairs cheaply (trigram on the name within the same country, or itinerary-embedding distance), then a Noul "Are these the same bookable product?" Accept → goes to `upload_staging` for the existing human decision; it is never auto-dropped. | **G** | enforce (routing to staging only) |
| A0-3 | **Country** | Alias table + filename. Multi-country tours get one country; unknown → NULL. | When the resolver returns NULL or the itinerary mentions places in another master country: Choice over the country master list from the itinerary text. Grey → NULL + flag (as today). | **G** | enforce |
| A0-4 | **Itinerary usable?** | Any non-empty text passes ("Day 1: TBA" passes). | Noul "Does this itinerary describe real day-by-day activities?" Reject → the row lands but is flagged `thin_itinerary` and excluded from the S1 batch until fixed. | **G** | enforce |
| A0-5 | **Column map** (`a0_column_map`, Haiku, when the header hit rate is below 50%) | An LLM returns JSON. | One Choice per column over the 19 fields + `none`. Grey → ask in the preview. | **R** | shadow (low volume; keep Haiku) |

### 4.1 A1 / A2 — S1 master rewrite and admin QA (platform data)

| # | Point | Now (verified) | Change | Jev | Mode at rerun |
|---|---|---|---|---|---|
| A1-1 | **Grounding of master content** | **Missing.** A1 has no check that the rewrite is supported by the raw source, apart from the meal/time regex. T3 only compares a tenant rewrite with master content, so a fact invented in master content reaches every tenant and passes T3. | (a) Deterministic: run `find_novel_numeric_claims` on A1 output against the raw source, as T3 already does. (b) Jev: code splits sentences; Noul per sentence "Is this supported by the source itinerary (paraphrase, unit conversion or sums allowed)?" Reject → a new hard code `UNSUPPORTED_CLAIM` with the sentence quoted, fed to flag_fix. Grey → soft note. | **G / V** | enforce |
| A1-2 | **Brand audit** (`s1_brand_audit`, GPT-4.1 + shadow Luna; S200: "keep, wait for Jev") | An LLM call with 5 binary scores + codes + `flagged_phrases`. | One Noul per binary item / failure code. All confident passes → skip the GPT call. Any reject or grey → call GPT as today (it gives the quoted `flagged_phrases` that flag_fix needs). | **P** | enforce |
| A1-3 | **Brand audit meal/time regex** (`ITIN_MEAL_INVENTED` / `ITIN_CLOCK_TIME`) | False-positive history (AA-608). | Only on a regex hit: "A logistics claim the source does not make, or narrative?" | **G** | enforce |
| A1-4 | **Judge** (`s1_judge`, GPT-5.6 Luna) | A call per attempt; `MIN_QUALITY` 7.0 decides retry/HITL. | Tie-break only when the judge score is within ±0.5 of 7.0: Nouls `brand_fit` / `mission_present`. The judge keeps producing the feedback text. Fewer needless retries/HITL near the line. | **G** | enforce |
| A1-5 | **validate soft codes** (`SUBTITLE_GENERIC`, `HIGHLIGHTS_TOO_GENERIC`, `SUMMARY_OFF_BRAND`) | Phrase lists; paraphrases pass. | Noul "Generic — would fit any destination?" as a second signal. The codes stay soft. | **G** | enforce |
| A1-6 | **flag_fix** | Adds forbidden words; misses the meta length → a full rewrite. | **AA-641** (deterministic, no Jev). | — | — |
| A2-1 | **Review Queue order** | Sorted by time/score. | Sort by Jev uncertainty (most doubtful first); show the Jev reasons per code. Advisory only. | **S** | enforce (ordering only) |

### 4.2 A3 — atomize, research, Segment / Score / Route (platform data)

| # | Point | Now (verified) | Change | Jev | Mode at rerun |
|---|---|---|---|---|---|
| A3-1 | **Atom grounding** (`t5_atomize`, Haiku) | Atoms are "verbatim-derived" by prompt only. Nothing checks that `place` + `action` are actually in the day's text. | Noul per atom "Is this place-and-activity stated in this day's itinerary text?" Reject → the atom is not stored (logged). | **V** | enforce |
| A3-2 | **Atom `activity_type`** | Haiku enum; `classify_exclusion` rules separately. | Choice when the two disagree (transit matters for A3-5). | **G** | enforce |
| A3-3 | **Research keyword belongs** (`segment_research_batch._propose` → `_buy_volumes`) | All Haiku keywords are bought unchecked. | Noul "About this particular place, not the kind of thing it is?" (ticket 03). Reject → not bought. | **P** | enforce |
| A3-4 | **Research idea filter** (`_buy_suggestions`) | Every idea with volume is stored (hotel/resort names). Tasks with fewer than 5 seeds are skipped (#481). | Noul "A traveller searching to experience the place (not lodging/booking)?" before storing. | **S** | enforce |
| A3-5 | **PAA landing** (`land_questions_for_segment`) | Shortlisted by one shared word, then it always lands on the nearest atom of the same Segment (no distance cut), so `questions_count` is inflated. | Rule: never land on a transit atom (ticket 01). Nouls: country scope (ticket 04) and "Would the asker be well served by an article about `<place — action>`?" (ticket 05). Only accepted landings count. | **G** | enforce |
| A3-6 | **Demand ownership** (`compute_demand`) | One shared non-generic word claims a keyword's volume ("Kyoto" 165k → "Kyoto incense-making"). | Noul "Does this keyword's demand belong to this moment?" on the best claimed keyword per Segment × market; reject → next candidate. The verdict is stored (deterministic reruns). | **G** | enforce |
| A3-7 | **Segment matching** (Jaccard place + verb) | Over-merges similar names (Kumano Hongu vs Nachi). | Noul "Same real-world place?" on borderline pairs, stored once (ADR 0002). Measure the borderline count first. | **G** | shadow → enforce if measured |

### 4.3 T-series (tenant data → Jev only for allow-listed test tenants until the DPA, C2)

| # | Point | Now (verified) | Change | Jev | Mode at rerun (test tenants) |
|---|---|---|---|---|---|
| T0-1 | Brand brief parser | Fixed header prefixes; other headings lose sections. | Choice per paragraph over 8 sections + `none`, only when fewer than 8 sections are found. | **S** | enforce |
| T2-1 | Judge / brand audit (the same nodes as A1) | As A1. | A1-2 / A1-4 apply here too, per tenant brand. | **P / G** | enforce |
| T3-1 | Grounding (`_t3_grounding_check`) | A numeric regex; each hit → a full rewrite (Sonnet). | Before rewriting: "Is this number supported (conversion, sums, rephrasing)?" Accept → no rewrite. Non-numeric sentences: A1-1 applied to the tenant rewrite. | **P / G** | enforce |
| T7-1 | Debate brand-fit (`score_candidate_fit`, Luna) | A call per uncached candidate. | Jev prefilter; Luna only in the grey zone. | **P** | enforce |
| T8-1 | `rank_angles` | Only checks verbatim copying of PAA questions. | Noul per (angle, question): "Does this angle actually answer this question?" | **V** | enforce |
| T9-1 | Facts in the writer prompt | **All** platform facts + all tenant facts are injected into every prompt. | Noul "Relevant to this trip/moment/angle?" per fact. | **S** | enforce |
| T10-1 | F8 framework (`t10_judge`, warn-only) | A GPT call every attempt for a warn. | One Noul per rubric item; replaces the call. | **R** | enforce |
| T10-2 | F9 (`cta_fact` block + `brand_style` warn) | One judge call. | Style half → Jev. CTA/fact: Jev prefilter, GPT on doubt. | **P** | enforce |
| T10-3 | Cannibalization (cosine ≥ 0.92 blocks) | A hard threshold. | Jev confirms at ≥ 0.92; checks 0.85–0.92. | **G** | enforce |
| T10-4 | `gate_promises_an_option` (hedge phrases) | Known false-positive surface. | Noul "Does this sentence state the offered moment as certain?" | **G** | enforce |
| T10-5 | F7 FAQ dedup (heuristic) | — | Noul "Same question?" on candidate pairs. | **G** | shadow |

**No Jev role:** prose generation (A1/T2 writers, flag_fix, itinerary nudge, T8 angle text, T9 write), `score.py` rank-sum, Route/Hub detection, T1, T4, T11.

## 5. Calibration (per question, before `enforce`)

1. **Sample:** ~200 items per question from real Dev data, stratified by the current rule's verdict.
2. **Shadow pass:** run Jev on the sample and store the probabilities (cents).
3. **Labels:** the agent proposes a label + a one-line reason. Nghiệp reviews ≥ 20% at random plus every agent-vs-Jev disagreement. If Nghiệp overturns more than 10% of the reviewed labels, re-pose the question and relabel. Agent labels are **not** independent human labels, and the record says so.
4. **Floors:** `reject_ceiling` and `accept_floor` each need ≥ 95% precision against the labels. Everything in between is grey (today's behaviour). No floor at 95% → the question stays in shadow.
5. **Record:** `docs/calibration/<question>.md` (sample query, labels, confusion table, floors) and `threshold_version` in the DB.

Pose each question so that both sides name the same kind of entity: Ms. Thư's 51% came from a mis-posed question.

About 30 questions in §4 × 200 items is ~6,000 labels, too many at once. Calibrate in build order (§6); rerun-critical questions first.

## 6. Build order

| Step | Work | Issue |
|---|---|---|
| 1 | Jev seam + `decision_log` + mode/thresholds + tenant allow-list + cost/log + Decisions view | AA-660 (re-scoped) |
| 2 | Calibration method + tooling (sample → shadow → label → floors) | AA-661 (AA-643 merged in) |
| 3 | A1 flag_fix | AA-641 |
| 4 | **A0 ingest gates** (A0-1…A0-4) | new |
| 5 | **A1 grounding** (A1-1, deterministic + Jev) | new |
| 6 | A1 brand audit / judge / soft codes (A1-2…A1-5) + A2 queue order | new |
| 7 | A3 research keyword gate + idea filter (A3-3, A3-4) | new |
| 8 | A3 atom grounding + activity type + PAA landing + demand ownership (A3-1, 2, 5, 6) | new |
| 9 | A3 Segment borderline measurement → decide (A3-7) | new |
| 10 | T-series Jev for test tenants (§4.3) | new (1 epic-level issue, split later) |
| 11 | UI v2 | AA-662 → 664 → 666 → 667 / 668 → 669–672 |
| 12 | Runbook + 3-tour smoke + full rerun (platform + test tenants) | AA-653 → 594 / 599 / 600 / 601 |

## 7. Still open

- C2: who contacts TypeSafe for the DPA (needed before real tenants use T-series Jev).
