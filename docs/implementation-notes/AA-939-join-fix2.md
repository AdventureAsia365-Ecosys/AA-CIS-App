# AA-939 — S263 join fix, CIS side (Agencies (CRM) panel)

The partner CRM side (claim cookies, recovery by a new invitation, the two new L1 writes) is in AA-Partner
`docs/implementation-notes/AA-939-join-fix.md`; the contract is root `docs/contracts/crm.ops.v1.openapi.yaml` 1.8.

## Decisions

- **Two relay routes** (`api/routers/admin_crm_console.py`), both `tenant.write` (`api/core/staff_authz.py`):
  `POST /admin/platform/crm/agencies/{tid}/owner-invite/withdraw` and `POST /admin/platform/crm/agencies/{tid}/delete`,
  each mirroring one crm.ops.v1 1.8 route inside the relay allow-list (`api/services/crm_console.py` `ALLOWED`). The
  CRM decides who may (crm-platform-admin) and what (synthetic only, no members, no travellers).
- **Withdraw owner invitation** sits next to "Invite first owner" in the First owner section, shown to writers while
  the agency has no owner and is active or locked. The CRM summary does not say whether an owner invitation is live,
  so the button is always offered then; with none the CRM answers 404 `owner_invite_not_found`, worded in the dialog.
- **Delete test agency** is its own section at the bottom of the drawer, rendered only for synthetic agencies. The
  button is disabled with the reason (the CRM's own refusal code, `testAgencyDeleteBlock` in `lib/crmConsole.ts`):
  offboarding, travellers, active or invited members (from the members read), members not loaded yet. The dialog
  asks for the agency name typed exactly (`agencyNameMatches`) and the ticket; on success the drawer closes and the
  list reloads (the agency is gone from it).
- EN and VI strings in `app/admin/platform/agencies/messages.ts` and `lib/crmConsole.messages.ts`; kit components only
  (`Button` danger / ghost, `WriteModal`, `Input`); no issue id in UI text.

## Changed

- Nothing beyond the brief. A real agency never shows the delete section (the brief: "shown only for synthetic
  agencies, disabled with the reason otherwise"); the CRM refuses it anyway (403 `not_synthetic`).

## Tradeoffs

- Offering "Withdraw owner invitation" without knowing whether one is live costs one possible 404 in the dialog;
  knowing would need a new summary field in crm.ops.v1 for a rare action.

## Should know

- Tests: `tests/unit/test_aa941_crm_console.py` (21 routes, both new paths relayed with their bodies, viewer refused),
  the staff route policy snapshot, `frontend/lib/crmConsole.test.mjs` (delete-block reasons, exact name, error words,
  source guards on the drawer). `docs/api/openapi.json` regenerated with the pinned FastAPI 0.161.1 / Pydantic 2.14.1
  (additions only).
- Merge after the AA-Partner PR is deployed: before that the CRM answers 404 / 405 for the two paths.
