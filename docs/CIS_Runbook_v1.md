---
id: OPS-02-cis-v1
title: AA-CIS operations runbook v1 (superseded)
description: Superseded April 2026 runbook; its steps targeted the destroyed acc1 stack. Read the pages listed below instead.
type: how-to
status: superseded
superseded_by: AA-Ecosys docs/ops/ (OPS-02 rollback runbooks, OPS-04 incident response)
owner: nghiep
audience: [engineer, devops, agent]
applies_to: [aa-cis-api]
last_reviewed: 2026-10-19
---

# AA-CIS operations runbook v1 (superseded)

> **Superseded (AA-889, 19/10/2026). Do not follow the old steps.** Version 1.0 (20/04/2026) was written
> for the first CIS stack on account 1, which was destroyed in July 2026 (AA-271). Its commands used the
> wrong account and profile, SQS queues that no longer exist, a `:latest` image tag that the immutable ECR
> repository no longer accepts (AA-869) and literal resource IDs. The old text is in git history
> (`git log -p -- docs/CIS_Runbook_v1.md`).

Use instead:

| Need | Where |
|---|---|
| Deploy, rollback, "Done" definition, live verify | AA-Ecosys `skill/aa-ship/SKILL.md` (rollback runbooks move to AA-Ecosys `docs/ops/runbooks/`, AA-892) |
| Incident response | AA-Ecosys `docs/ops/incident-response.md` |
| Database access and one-off scripts | AA-Ecosys `skill/aa-cis-schema/` (S3-mediated ECS exec) |
| Clean rerun of the content pipeline | [`runbooks/clean-rerun.md`](runbooks/clean-rerun.md) |
| Stack, services, environment variables | [`../CONTEXT.md`](../CONTEXT.md) |

The AA-Ecosys root repository is private; ask Nghiệp for access.
