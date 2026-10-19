> **Archived (AA-889, 19/10/2026) — not current truth.** Session hand-off of 23/05/2026, moved from the repo root. Session state now lives in the AA-Ecosys session logs (`docs/sessions/`), the Notion memory and Linear; the current system is described in [`../../CONTEXT.md`](../../CONTEXT.md).

# HANDOFF.md — AA-CIS-App
Last updated: 23/05/2026 | Session 31

## Current State
- ECS: api:185 (STOP REQUIRED — see cost checklist)
- CI: CIS main ✅ Deploy Prod | ACP main ✅ CI + Deploy
- AWS: STOP REQUIRED (ECS desired=0 + RDS stop)
- AA-103: COMPLETE ✅ — merged to main, Vercel production deployed

## Completed This Session (Session 31 — AA-103 Completion)

### AA-103 — Route conflict fix + production deploy

| Step | Status |
|------|--------|
| Fix Next.js route conflict (admin) vs (internal) | ✅ |
| CIS PR #2 merged to develop | ✅ |
| ACP PR #1 merged to develop | ✅ |
| CIS develop → main | ✅ |
| ACP develop → main | ✅ |
| CIS Deploy Prod CI | ✅ green |
| ACP Deploy + CI | ✅ green |
| Live: aa-cis.lumiguides.it.com/admin/upload | ✅ 200 |
| Live: acp.lumiguides.it.com/workspace/pipeline/s0 | ✅ 200 |

### Route fix details
- Root cause: `(admin)` and `(internal)` both had `/upload` → same URL
- Fix: Renamed `(admin)` → real `admin/` folder. All admin pages now at `/admin/*`
- Updated: AdminSidebar hrefs, middleware, login redirect, root redirect
- Commit: `5b6face` on feature/aa-103-e2e-ui

### ALB_INTERNAL_URL status
- Lambda `aa-cis-dev-acp-s4-trigger`: ALB_INTERNAL_URL = `http://aa-cis-dev-alb-<id>.us-west-1.elb.amazonaws.com` ✅ FIXED
- Code correctly reads from env var — no placeholder in source code

## CIS Pages — Production URLs
| Page | URL | Auth |
|------|-----|------|
| Upload (S0) | /admin/upload | admin/content |
| S1 Rewrite | /admin/pipeline/s1 | admin/content |
| Master Content | /admin/master-content | admin/content |
| Dashboard | /admin/dashboard | admin only |
| Tenants | /admin/tenants | admin only |
| (Internal) Upload | /upload | admin/content |
| Review Queue | /review | admin/content |
| Catalog | /catalog | admin/content |

## Next Session Priority
1. Manual UAT all pages on production (start ECS + RDS first)
2. Fix Lambda `aa-cis-dev-acp-s4-trigger` — verify S4 blog trigger works end-to-end
3. WordPress Docker UAT setup (docker/wordpress-uat + ngrok + Secrets Manager)
4. Verify aa_internal tenant UUID in DB (Gate 1 hardcodes this)

## Start commands (next session)
```
aws ecs update-service --cluster aa-cis-dev-cluster --service aa-cis-dev-api --desired-count 1 --profile pqnghiep-admin --region us-west-1
aws rds start-db-instance --db-instance-identifier aa-cis-dev-db --profile pqnghiep-admin --region us-west-1
```

## Outstanding Issues
- WordPress UAT setup not done
- api_task_def_arn hardcoded :21 in main.tf — AA-22 tech debt
- EventBridge S3→S4 source mismatch (acp.pipeline vs acp.s3) — ACP side

## Cost Checklist (DO BEFORE CLOSING)
```
⚠️ Run these manually (Claude will NOT auto-execute):
aws ecs update-service --cluster aa-cis-dev-cluster --service aa-cis-dev-api --desired-count 0 --profile pqnghiep-admin --region us-west-1
aws rds stop-db-instance --db-instance-identifier aa-cis-dev-db --profile pqnghiep-admin --region us-west-1
```
