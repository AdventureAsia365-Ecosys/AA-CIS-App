# AA-979 — the Dev-only E2E CIS admin may send Content studio invitations (synthetic tenants only)

Session S252, agent Claude Code. Branch `feat/a10b-studio-invites-synthetic`. Design: root ADR 0006a amendment
A10b-1 (Nghiệp, S252, option (a)). Purpose: the AA-979 phase 2 re-enrolment E2E (AA-Partner `e2e/phase2/`, mode
`a10b`) makes the staff invitation itself, without a person.

## Decisions

- **Allow-list, not a new identity.** `e2e_staff.E2E_STUDIO_INVITATION_WRITES` holds the two route templates
  (`POST /admin/tenants/{tenant_id}/members/invitations`, `POST /admin/tenants/{tenant_id}/members/{membership_id}/invitation/send`)
  and is part of `E2E_ALLOWED_WRITES`; `E2E_ALLOWED_WRITE_PATHS` gains the two concrete-path regexes (UUIDs, full
  match). The bearer middleware and `staff_authz.denial_reason` therefore let exactly these through, nothing else.
- **"Synthetic" = `shared.tenants.is_canary IS TRUE`.** The only CIS marker (migration 058: "TRUE for synthetic canary
  tenant"; db-schema-reference: `is_canary boolean`, nullable since 281). NULL, FALSE or no row all count as real.
  One SQL constant, `e2e_staff.SYNTHETIC_TENANT_SQL`.
- **The check sits in the routes, not the middleware.** The middleware runs before routing and has no tenant
  lookup; the routes already open a connection and run `_require_tenant`. `_refuse_e2e_invitation` runs right after
  `_require_tenant` and before `members_session.invite` / `list_members` / the CRM relay, so a refusal writes no
  membership row, no `tenant.member_invited` audit row and sends no mail.
- **Two extra narrowings for the A10b session only** (least privilege, they cost the phase 2 run nothing): the
  address must be on the E2E mailbox domain (`e2e-mail.lumiguides.it.com`, exact, ASCII only, no sub-domain) and a
  new invitation may only be `viewer` or `editor` (never `owner`). Both answer the same 403 `e2e_read_only`.
- **Same 403 detail** (`e2e_read_only`) for every refusal, so the BFF and the Partner spec need no new string. The
  reason is in the log line `tenant_member_invitation_e2e_refused reason=role|address|tenant_not_synthetic` (no
  address logged).

## Changed

- `api/core/e2e_staff.py`: the two routes, `SYNTHETIC_TENANT_SQL`, `E2E_MAIL_DOMAIN`, `E2E_INVITE_ROLES`,
  `is_e2e_mailbox_address`, `is_synthetic_tenant`.
- `api/routers/admin_tenant360.py`: `_refuse_e2e_invitation` and its two calls.
- Docstrings: `staff_identity.py`, `staff_bearer.py`; `CONTEXT.md` A10b paragraph.
- Tests: `tests/unit/test_aa979_a10b_studio_invites.py` (new); `test_aa816_e2e_cis_admin.py` guard updated to the
  four reviewed routes, plus middleware cases for the invitation paths and nearby tenant writes.

## Tradeoffs

- Audit is unchanged. Every A10b write that passes the bearer cap already gets an AA-884 `staff.request` row
  (allowed or refused, with the status); an invitation also writes `tenant.member_invited`. The resend writes no
  domain row (as for every admin) — only the `staff.request` row and the `tenant_member_invitation_relayed` log.
- The CRM side (`crm.ops.v1 POST /tenants/{id}/studio-invitations`) accepts any `cis-admin` OPS caller and does not
  know about synthetic tenants. A stolen A10b code could already call it directly before this change; CIS is the
  gate this PR adds, the CRM gate is a possible follow-up (AA-Partner).

## Should know

- Phase 2 needs a target tenant with `is_canary = TRUE` on Dev. Check it first (Data coverage row `ten.canary`, or a
  read through ECS exec). If no tenant is flagged, flagging one is a Dev data write (owner's call), not part of this PR.
- No migration, no Infra dependency for the code itself; the studio invitee mailbox addresses come from AA-CIS-Infra
  `chore/e2e-studio-invitee-mailboxes` (`e2e-test-studio-viewer@`, `e2e-test-studio-editor@`).
- `test_aa899_openapi_export` fails locally on origin/main too (local fastapi 0.110.1 renders the schema differently);
  no route signature changed here.
