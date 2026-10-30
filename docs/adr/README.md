# AA-CIS-App ADRs

Decisions that affect this app only. A decision that, if reversed, would change two or more repos (shared
schemas, contracts between apps, accounts, the ecosystem Model Gateway) is an ecosystem ADR and lives in the
private AA-Ecosys repo under `docs/adr/`; an app ADR that implements one cites it. Infrastructure-only decisions
live in AA-CIS-Infra.

Each ADR states its status near the top (`- **Status:** Accepted (dd/mm/yyyy, S<nnn>)`). An amendment is a new
ADR that names the one it amends; the amended ADR keeps its file. Add every new ADR to this table in the same PR.
Older decisions recorded as `ADR-2026-NNN` predate this folder and live in the team's Notion.

| ADR | Decision | Status | Linear |
|---|---|---|---|
| [0001](0001-atoms-platform-wide-segments-per-tenant.md) | Atoms are platform-wide; Segment/Score/Route/Hub per tenant was tech debt | Accepted for the Atom half; the Segment/Score/Route/Hub half is superseded by [0003](0003-segment-score-route-hub-will-become-platform-wide.md) | AA-526, AA-542 |
| [0002](0002-search-demand-shared-across-tenants.md) | Search Demand (DataForSEO cache) is shared across tenants | Accepted | — |
| [0003](0003-segment-score-route-hub-will-become-platform-wide.md) | Segment/Score/Route/Hub become platform-wide | Accepted (06/09/2026); implemented by AA-545 | AA-542, AA-545 |
| [0004](0004-live-writing-progress-via-redis-poll.md) | Live writing progress: Redis snapshot + short poll, not SSE | Accepted (25/09/2026) | AA-637 |
| [0005](0005-model-catalog-and-converse-adapter.md) | Model catalog in the DB + Bedrock Converse adapter, no silent cross-vendor fallback | Accepted (28/09/2026, S200) | AA-658 |
| [0006](0006-stage-routes-fallback-and-shadow.md) | Stage routes: explicit fallback chains and a shadow model | Accepted (28/09/2026, S200); section 5 amended by [0008](0008-validate-stages-any-vendor.md) | AA-659 |
| [0007](0007-jev-verdicts-may-act.md) | Jev verdicts may act, only when enforced, confident and calibrated | Accepted (S203) | — |
| [0008](0008-validate-stages-any-vendor.md) | Validate stages may use any vendor | Accepted (10/10/2026, S224) | AA-748 |
| [0009](0009-f17-owns-offered-moments-promises-gate-dropped.md) | F17 owns offered moments; the promises_an_option gate is dropped (amends origin ADR 0023/0026) | Accepted (20/10/2026, S245) | AA-787 |
| [0010](0010-governed-writing-memory.md) | Governed writing memory: release-gated rules and pinned examples, no fine-tuning; supersedes the AA-132 lessons loop | Accepted (20/10/2026, S245) | AA-915 |
| [0011](0011-t9-faq-question-filter-llm-step.md) | T9 blog FAQ: LLM steps (stages `t9_faq_filter` on Haiku, `t9_faq_answer_fit` on Sonnet; validate) filter the real search questions and drop answers that do not fit, behind a flag | Accepted (22/10/2026, S247) | AA-779 |
| [0012](0012-llm-data-scopes-per-stage.md) | LLM data scopes per stage: `llm_role_config.allowed_scopes` (migration-only) decides which connections a stage's content may use; replaces the S235 / S3 code rules | Accepted (30/10/2026, S260) | AA-1002 |

Template and format: the ecosystem ADR template (AA-Ecosys `docs/templates/adr.md`) and
[`.agents/skills/domain-modeling/ADR-FORMAT.md`](../../.agents/skills/domain-modeling/ADR-FORMAT.md).
