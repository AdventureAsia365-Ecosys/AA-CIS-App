<!--
Title: "<type>: <what changed>" in English. Types: feat, fix, chore, refactor, docs, ci, test, perf.
No "[AA-123]" in the title. In the body write "Refs AA-123"; never Fixes/Closes/Resolves (Linear would close the issue).
This repo is public: no account IDs, ARNs, SG/VPC IDs, secret values or tenant data in the PR or the diff.
Security and infrastructure findings go to the private ops docs, not here.
-->

## What

-

## Why

Refs AA-

## Risk and rollback

- Risk: low / medium / high (high: auth, tenant isolation, migrations, deletes, LLM spend, IAM)
- Blast radius: admin / portal / API / worker / pipeline stage:
- Rollback: revert and redeploy the previous image, or:

## Cost

- [ ] 0 USD: no new AWS resource, no new paid API or LLM call path
- [ ] Adds spend: monthly estimate (AWS resource, LLM calls per run, third-party API):

## Quality (Q-FIX) — only when the PR changes a writer, gate, repair, judge or input selector

<!-- AA-925 / Quality System v1 §6.1. Delete this section otherwise. -->
- Step 0: the real writer input of 3 failing samples was dumped before the fix (where: implementation note / audit link):
- `class_id` (taxonomy class this fixes):
- Q0 case ids added in `tests/unit/quality/cases/` (each must fail on the base tip: CI step "Q0 red on base"), or `no-new-case: <reason>`:
- `q0-relabel` justification (only when an existing truth label changes or a case is removed; an independent review follows):
- Q2 run id (only when a judge prompt, rubric, anchor or judge route changed):

## LLM change — only when the PR changes a prompt, a model or a route

<!-- AA-900 / handbook §5.7. Runbook: docs/llm/eval-runbook.md; prompts: docs/llm/prompt-register.md. Delete this
section otherwise. A route or model change made in admin Settings records the same lines in its issue. -->
- Prompt id(s) from the register (row added or updated when a file, symbol, stage or version moved):
- Eval: R2 `<writer>` (label `adhoc_aa900`) / Q2 / Q0-R0 tier / none (manual check described below):
- Eval run ID and verdict (an R2 run happens after deploy: add it as a comment on this PR once it finished):
- Eval cost: logged USD (cap USD):

## Docs checklist

- [ ] `CONTEXT.md` updated (boundary, stack, ownership or a domain term changed), or N/A
- [ ] ADR added or amended, and listed in `docs/adr/README.md`, or N/A
- [ ] Runbook or how-to updated (an ops procedure changed), or N/A
- [ ] Contract, API shape or event change noted (contract version, CHANGELOG), or N/A
- [ ] Implementation note `docs/implementation-notes/AA-xxx.md` (force-add: `docs/` is gitignored)

## Verification

- [ ] CI green (Lint, Security Audit, Unit Tests, Integration Tests, Docker Build Check, Frontend Lint, Frontend Build)
- [ ] Migration: identical copy in AA-CIS-Infra `db/migrations/cis/`, applied on Dev by DB Migrate before this merges; or N/A
- [ ] LLM calls go through `LLMClient` and land in `shared.llm_call_log`; or N/A
- [ ] UI change: Vercel preview checked on desktop and mobile, light and dark, console clean; or N/A
- [ ] Backend: merge only with no queued or running `shared.job` and no wave; after deploy, api and worker on the same SHA and the endpoint verified live (status + shape)
- Evidence (commands, output, screenshots):
