# Validate stages may use any vendor (AA-748)

- **Status:** Accepted (10/10/2026, S224 — Nghiệp approved).
- **Amends:** ADR 0006 §5 (the admin vendor rule). Implements ecosystem ADR-2026-014/027 more narrowly.

## Context

ADR-2026-014/027 keep writers and judges on different vendors so a judge never scores its own
vendor's prose. ADR 0006 §5 enforced that in admin as "a stage that is not a judge must be
Anthropic", which also caught `validate` stages.

AA-748 added `s1_source_facts`: one call per tour that extracts the figures in the source
(distances, durations, altitudes, times, meals, other numbers) for the S1 writer prompt. It writes no
prose, the judge never scores its output, and every figure passes a deterministic source check
before the writer sees it. On Haiku it cost $0.0078 per tour (+25% of S1); GPT-6 Luna is about 10x
cheaper ($0.10 / $0.50 per Mtok against $1 / $5).

## Decision

1. The vendor rule applies to **writer** and **judge** stages only. Writers stay Anthropic, judges
   stay non-Anthropic.
2. A **validate** stage (extraction or a check whose output the judge never scores) may use any
   enabled catalog model.
3. `s1_source_facts` is a validate stage on `gpt-6-luna`, with `haiku` as its fallback (migration 212,
   `SAFE_DEFAULTS`).

## Consequences

- A stage must not be labelled `validate` to dodge the rule: if its output is prose the judge
  scores, it is a writer.
- The writer/judge isolation is unchanged: the S1 writer is Haiku, the S1 judge is GPT-5.6 Luna.
- Rollback is a data change (role_config row back to writer/claude/haiku).
