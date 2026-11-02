# AA-1031 (S264 lane l10) — Agencies (CRM): first-owner invite also invites to the Content studio

Tech lead test (S264): inviting an agency's first owner from Platform › Agencies (CRM) gave the person a CRM account,
but the CRM sidebar never showed "Open Content studio": studio access needs an active CIS `shared.tenant_members`
row, which only the CIS invitation route creates (root ADR 0010a decision 4: staff invite; no CRM event grants a CIS
role).

## Decisions

- In the agency drawer's "Invite first owner" dialog, a checkbox "Also invite to the Content studio as owner",
  ticked by default, shown only when the agency id is an ACTIVE CIS tenant (`useTenants().byId`, the cached
  `/admin/tenants?include_inactive=true` list) and the staff member has `tenant.write` (`can()`; the CRM write right
  alone is not enough, the CIS route needs tenant.write).
- Order: the CRM invitation first; only when it was sent does the page call
  `POST /api/admin/tenants/{id}/members/invitations` `{email, content_studio_role: "owner", locale: "en"}`. A
  refused CRM call stops there, so a retry never creates a second studio invitation.
- Both results stay in the section ("Last invitation": CRM line + Content studio line: sent / created but not
  delivered / not invited, with the reason and where to send it again). The toast says when the studio half needs
  attention.
- Still a staff action (the staff member's own session, through the CIS BFF), so ADR 0010a holds; no ADR change.

## Changed

- `frontend/app/admin/platform/agencies/AgencyDrawer.tsx` (FirstOwnerSection), `messages.ts` (DRAWER EN/VI),
  `frontend/lib/crmConsole.ts` (`studioInvitationsPath`, `studioInviteOffered`, `studioInviteOutcome`) + tests in
  `lib/crmConsole.test.mjs`, `CONTEXT.md` (CRM console).

## Tradeoffs

- Two e-mails reach the owner (the CRM join link and the Content studio link): the two products keep separate
  invitations by design (crm.member_invitation vs crm.studio_invitation); merging them is a contract change.
- Mail language of the studio invitation fixed to English (pages default to English); the Members tab can send it
  again in Vietnamese.

## Should know

- The studio membership is created even if its mail fails (201, `delivery.status = failed`); send it again from
  Tenants › Tenant 360 › Members (PR "Tenant 360 Members tab").
