# AA-1031 (S264 lane l10) — Tenant 360 Members tab

Tech lead test (S264): Content studio access needs an active `shared.tenant_members` row for the person's (iss, sub).
Such a row is created only by `POST /admin/tenants/{id}/members/invitations` (+ the CRM relay mail + accept), but no
admin page called it: root ADR 0010a decision 4 says "Staff invite from Tenant 360" and the UI did not exist.

## Decisions

- A new Tenant 360 tab "Members" (after Markets), over the EXISTING admin routes only (no backend change):
  list, invite, send again, change role, remove (`frontend/lib/tenantMembers.ts` holds paths, checks and helpers).
- No ticket reference: the CIS member routes do not take one (they write `acp_shared.audit_log` with the staff actor).
- Send again asks for the e-mail again: CIS never stores the address (ADR 0010a), the route needs it in the body.
- Mail language selectable (en / vi), default English (pages default to English).
- Write controls follow `can(role, "tenant.write")` (admin), like the other admin pages; the BFF
  (`API_WRITE_RULES` "tenants") and the backend (`tenant.write`) re-check.
- The invitation's mail result is kept on the page in a status line (a toast disappears before anyone reads
  "created but not delivered"), and toasted.
- Remove warns when the row is the last ACTIVE owner (the CRM locks the agency until an owner recovery); a role change
  of that row away from owner warns that the studio has no active owner (the CRM ignores role_changed events).

## Changed

- `frontend/app/admin/tenants/page.tsx` (tab), `messages.ts` (tab label EN/VI), new `_components/Tenant360Members.tsx`
  and `_components/members.messages.ts` (EN/VI), new `frontend/lib/tenantMembers.ts` + `lib/tenantMembers.test.mjs`.
- `CONTEXT.md` (Tenant membership directory: the admin UI).

## Tradeoffs

- Row actions in a kit `RowMenu` instead of inline buttons: four actions per row would not fit at 390 px.
- No optimistic updates: each write reloads the list (the server decides the outcome, e.g. a removed row is final).

## Should know

- The Dev E2E CIS admin is read-only; its invite is allowed only on a synthetic tenant, to an E2E mailbox, as
  viewer / editor (the API refuses anything else with 403 `e2e_read_only`; the dialog shows the error).
- Root ADR 0010a already describes this UI ("Staff invite from Tenant 360"); no ADR change.
