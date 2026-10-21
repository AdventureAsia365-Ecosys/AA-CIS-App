# Governed writing memory: release-gated rules and pinned examples, no fine-tuning (AA-915)

- **Status:** Accepted (20/10/2026, S245 — Nghiệp approved decisions D1–D16 and E1–E4; recorded
  21/10/2026, S246).
- **Scope:** the CIS writers A1/S1 (Master tours), T2 (tenant tour rewrite), T9 blog and T9 social.
- **Supersedes:** the AA-132 lessons loop (`shared.pipeline_lessons`, frozen by AA-915 behind
  `PIPELINE_LESSONS_ENABLED`, default off) and the AA-99 / N8 output rules
  (`acp_shared.acp_output_rules`, `/v1/rules`, already unused since AA-603). WM-34 retires both.
- **Implements:** ecosystem ADR 0009 (reserved: AI platform, AA-804). That number belongs to the
  ecosystem series in the AA-Ecosys repo; this file is in the app series. The two are unrelated.
- **Relates to:** ADR 0006 §5 as amended by ADR 0008 (writer and judge vendors), ADR 0007 (Jev
  verdicts may act).
- **Design:** the S245 writing-learning-loop audit in the AA-Ecosys repo
  (`docs/audits/2026-10-20-S245-writing-learning-loop/`, with its `cost-cuts/` round).

## Context

CIS has many gates and repair loops (T10 gates, sentence repair, flag_fix, Jev) but no learning loop
that works:

- **AA-132 lessons loop.** flag_fix wrote rows with `stage = 'audit'`; the S1 readers asked for
  `S1_rewrite` / `S1_editor`, so nothing written was ever read. The write ran
  `run_until_complete` in an executor thread and most likely always failed silently. The table has
  no eval gate, no `tenant_id`, and every row auto-activates. T2 runs the same S1 graph, so a
  "fixed" loop would let one tenant's failures steer the global S1 prompt and would keep text
  derived from tenant content in a global table. AA-915 froze it.
- **N8 flywheel / rule extractor** (`acp_output_rules`) was removed with N7/N8 (AA-603).
- The best human signal already stored is the admin's sentence edits (before / after, gate,
  problem). Three places lose the original or the reason: A1 field edits, tenant edits of T9
  pieces, and every approve / reject / regenerate.
- Fine-tuning is not practical now: the data is too small and the provider paths are closing or
  unavailable for the writer models.

So the platform learns **in the prompt**, under release control, and model weights stay unchanged.

## Decision

Writing memory is a store of **rules** and **pinned examples**, scoped to the platform or to one
tenant. It reaches a writer only through a versioned **release**. Dated invariants (20/10/2026):

1. **Nothing auto-activates (D6a).** Every release, tenant releases included, needs one human
   click. This is enforced twice: by a DB CHECK and by the API, which refuses to activate a release
   that lacks its eval and shadow evidence.
2. **No fine-tuning in v1 (D2).** Revisit at ≥ 1 000 clean pairs per writer. Until then, keep a
   "clean pairs per writer" count on the metrics tab, but build no export path.
3. **Rules only constrain.** They may cut, ban, rephrase or format. They never add content.
   - An automatic lint rejects any rule of the form "mention service X".
   - The replacement string of a `lexicon` "replace" rule is checked with `new_specifics` (or an
     equivalent check). A replacement that adds a number, place, time, price or service is
     rejected ("lunch" → "lunch included" fails).
   - Rule types: `lexicon`, `writer_instruction`, `repair_hint` and `preference_summary`. A tenant
     preference summary (≤ 5 sentences) is a rule and goes through the same lint, quarantine and
     release gates.
4. **Fact gates outrank every learned item.** F1, F17, F9_cta_fact and Jev decide over any rule or
   example. Precedence among the rest: tenant-authored brand rule, then learned tenant rule, then
   platform rule.
5. **Rule drafting and judging use the existing non-Anthropic judge route (D10)**, following
   ADR 0006 §5 as amended by ADR 0008: writers stay Anthropic, the judge is another vendor. The
   OpenAI-direct route is only the last fallback. Accounts are named by role only, not by number.
6. **Monthly release train (E1).** Rule and example releases ship once a month.
   - `lexicon` (L0) rules may ship any time, proved by a $0 replay of stored text and gate ledgers,
     and may ship before the AA-729 golden set exists (D11).
   - A lexicon rule applies **before** the gates (or v1 uses ban rules only, which trigger the
     normal repair pass). Text is never replaced after the gates have run.
   - Rollback is possible at any time: move the active pointer back to the previous release. No
     deploy is needed.
7. **Shadow sizes (E2, as revised by the cost-cuts K4 check).**
   - 20 targeted inputs for A1, blog and LinkedIn.
   - 50 inputs for T2 and Facebook, until the golden set covers those two strata.
   - At most 10 inputs (where the gate fired before) for a release that holds only `repair_hint`
     rules.
   - Every tenant release adds 1–2 inputs from another tenant as a cross-tenant isolation probe.
8. **Where memory is inserted (E3).** S1 gets rules only, never examples. T2 and T9 get two pinned
   examples per release. Entities are masked (`[PLACE]`, `[N]`…) and the examples are labelled
   "voice only, reuse no fact". There is no nearest-neighbour retrieval in v1.
9. **Budget (E4, amending D8).** The D8 figures of $60/month and $15 per run are replaced by:
   - learning-loop LLM spend on Dev ≤ $25/month and ≤ $10 per R2 release run;
   - a full baseline or reverify run needs separate approval;
   - every call goes through `LLMClient` and is logged in `shared.llm_call_log`.
10. **Scope and isolation.**
    - `platform` rows never hold tenant text.
    - A platform rule whose evidence comes from tenant content needs ≥ 3 distinct tenants, and every
      platform rule needs Nghiệp's approval (D9). "≥ 2 reviewers" and "gate failure repeats in
      ≥ 5% of pieces over 30 days" apply to A1 signals only.
    - A tenant's examples are used only for that same tenant and are never used for training (D12).
    - Requirement for the new tenant-scoped tables (WM-03 onward): follow the `acp_shared.facts`
      pattern. That means a CHECK `tenant_id IS NULL` iff `scope = 'platform'`, a
      `tenant_isolation` policy for the tenant pool role (`aa_app_user`), and FORCE ROW LEVEL
      SECURITY. The `cis_app` policies on CIS tables are `USING true`, so every `cis_app` code path
      also filters explicitly with `tenant_id = $1`, and a CI test proves that tenant A's rules,
      examples and summaries never resolve for tenant B.
11. **Tenant text lives only in deletable tables.** These are `writing_signal`, the tenant-scoped
    `writing_rule` and `writing_exemplar` rows, and `writing_memory_shadow` (shadow outputs).
    - An offboarding delete removes them through the DRY-RUN procedure.
    - `acp_shared.audit_log` (append-only, hash-chained) keeps only ids, hashes and reason chips.
    - Admin sentence-edit text that is already in `audit_log` cannot be deleted. It is a retention
      exception (D16), to be listed in the AI Use Register (AA-804).
12. **Release 0 must be a no-op.** Before `memory_enabled` is switched on for any writer, replaying
    release 0 (empty memory) through the new insertion path must give a prompt byte-identical to the
    **post-AA-915 prompt (`PIPELINE_LESSONS_ENABLED` off)** (WM-20, R0, $0). The lessons-bearing
    prompt from before AA-915 is not the baseline.
13. **Capture is implicit only (D7).** For now the tenant portal adds no feedback question; edits
    are recorded silently. Revisit at the first real tenant.

## Consequences

- Each release carries a version stamp. A1 and T9 outputs record it now; T2 records it once
  `tenant_tour_versions` gets a metadata column (P3).
- The `shared.pipeline_lessons` table stays in place but frozen until WM-34 drops it. With the flag
  off nothing reads or writes it. Turning the flag on is only a rollback of the old prompt, never a
  way to learn.
- One person approves every release, so the review queue is sized at ≤ 30 minutes a week (D15).
- Releases are monthly, so a learned improvement can take up to a month to reach a writer. Only L0
  lexicon rules skip the train.

## Alternatives considered

- **B — golden-gated writer optimiser.** Splits every writer prompt into named slots stored in the
  DB, and runs a GEPA-style optimiser against a golden set. It has the strongest eval discipline,
  and its golden-set tables, judge calibration (κ ≥ 0.6) and two-layer activation lock are folded
  into this design. It was not chosen as the base: refactoring every A1/T2/T9 prompt into slots has
  a wide blast radius (T2 shares the S1 graph), and with ~250 T9 pieces, ~50 T2 versions and almost
  no labels the optimiser would overfit.
- **C — writer flywheel with six layers up to fine-tuning.** Its L0 lexicon fast path, the
  "nothing auto-activates" rule, the same-transaction signal helper and the tenant preference
  summary are folded in. It was not chosen as the base: it is the widest and most expensive option,
  and its dataset export to S3 adds another store of tenant text that is not needed now. It also
  depends on fine-tuning paths that are mostly unavailable.

## Review triggers

- The first real (paying) tenant: revisit D7 (portal feedback question) and the tenant release
  approval load.
- ≥ 1 000 clean pairs for one writer: revisit fine-tuning (D2).
- A change of the judge model or of the gate taxonomy (AA-787): releases go to `reverify`, and the
  calibration gate is re-checked.
