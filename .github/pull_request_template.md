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

## Docs checklist

- [ ] `CONTEXT.md` updated (boundary, stack, ownership or a domain term changed), or N/A
- [ ] ADR added or amended, and listed in `docs/adr/README.md`, or N/A
- [ ] Runbook or how-to updated (an ops procedure changed), or N/A
- [ ] Contract, API shape or event change noted (contract version, CHANGELOG), or N/A
- [ ] Implementation note `docs/implementation-notes/AA-xxx.md` (force-add: `docs/` is gitignored)

## Verification

- [ ] CI green (Lint, Security Audit, Unit Tests, Integration Tests, Docker Build Check, Frontend Lint, Portal Lint)
- [ ] Migration: identical copy in AA-CIS-Infra `db/migrations/cis/`, applied on Dev by DB Migrate before this merges; or N/A
- [ ] LLM calls go through `LLMClient` and land in `shared.llm_call_log`; or N/A
- [ ] UI change: Vercel preview checked on desktop and mobile, light and dark, console clean; or N/A
- [ ] Backend: merge only with no queued or running `shared.job` and no wave; after deploy, api and worker on the same SHA and the endpoint verified live (status + shape)
- Evidence (commands, output, screenshots):
