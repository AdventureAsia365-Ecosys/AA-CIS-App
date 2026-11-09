# AA-1031 (S264 lane l10) — Create agency: picker without taken tenants, plain words for a deleted agency's id

Tech lead test (S264): the Create agency modal offered CIS tenants that already had an agency (a sure 409
`tenant_exists`), and a tenant whose test agency had been deleted got the opaque "An agency with this id already
exists" although no list showed that agency.

## Decisions

- The tenant picker gets `offer={offerForNewAgency(rows)}`: tenants whose id is in the loaded agency list are left
  out (`lib/crmConsole.ts`). The list is keyset-paged, so an agency on a page not loaded yet still answers 409
  `tenant_exists` (the CRM stays the authority); the hint says taken tenants are not listed.
- New CRM refusal code `tenant_deleted` (AA-Partner PR of this lane, crm.ops.v1 1.9) in `CRM_ERROR_TEXT` /
  `CRM_ERRORS` (EN + VI): "This tenant's test agency was deleted; its id cannot be reused. Create a new CIS tenant."
  The modal shows it inline like every CRM refusal (text + code).
- No CIS backend change: the relay passes the CRM's status and body through.

## Changed

- `frontend/app/admin/platform/agencies/AgenciesConsole.tsx`, `messages.ts` (hint EN/VI), `frontend/lib/crmConsole.ts`
  (`tenant_deleted` text, `offerForNewAgency`), `frontend/lib/crmConsole.messages.ts` (VI), tests in
  `lib/crmConsole.test.mjs`, `CONTEXT.md` (CRM console).

## Tradeoffs

- A deleted agency's tenant stays in the picker (the CRM list leaves trashed agencies out, so CIS cannot know); the
  409 text tells staff what to do instead.

## Should know

- Ship after (or with) the AA-Partner PR: before it, the CRM still answers `tenant_exists` for a trashed id (the new
  text simply never shows; nothing breaks).
