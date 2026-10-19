# AA-878 step C1 — read-only `viewer` staff role (CIS)

Session S243 (19/10/2026), Claude Code builder lane. Branch `feat/aa878-viewer-role`, migration **249**
(the lane plan said 248; 248 is AA-884's audit migration, merged as #835).

## Decisions

- **Migration 249 = one `BEGIN … COMMIT`, `ADD VALUE` only.** PG 12+ allows `ALTER TYPE … ADD VALUE` inside a
  transaction block; the value just cannot be used before commit (55P04). So the file has a guard (type exists,
  runner owns it), the `ADD VALUE IF NOT EXISTS 'viewer'`, the `schema_versions` row, nothing else. No row, no
  cast, no default uses `'viewer'` (a unit test counts the literal). Same shape as 218 (`content`) and 248.
- **Read-only is a role property, not only a missing capability.** `ROLE_PERMS['viewer'] = {content.read}`, plus
  `READ_ONLY_ROLES = {'viewer'}`: `denial_reason` refuses a read-only role on any method but GET/HEAD/OPTIONS
  before looking at POLICY. Why: the notification bell PUTs need `content.read` (so a plain capability map would
  let the viewer clear everyone's bell), and a future write added without a POLICY entry must not be open to it.
  Kept `content.read` on the bell PUTs (no snapshot change, no new capability), so the route-policy snapshot is
  unchanged: this PR adds no route.
- **BFF: one central refusal.** `requireAdmin()` (every secret-attaching proxy calls it) answers 403 for a
  read-only role on a non-read method; `canWriteAdminPath` also returns false for it (no default allow). Covers
  `/api/tenant/*`'s staff branch, which forwards to non-POLICY backend paths.
- **Booking.** `NO_BOOKING_ROLES = {'viewer'}`; `/auth/booking-token` answers 403 (the DB role decides, checked
  after the active-row lookup and before any signing key is read). 403, not 401: the session is valid, the role
  has no Booking access.
- **Not assignable.** `staff_directory.CIS_ROLES` and the Staff page select (`ASSIGNABLE_STAFF_ROLES`) stay
  admin/reviewer/content. A viewer row still lists (counted in `active_by_role`), its select shows "Viewer
  (read-only)" as a disabled option. `change_roles` away from viewer does not try to remove a `cis-viewer` group.
- **No Cognito group.** `cis-viewer` is deferred to AA-816 E1 (plan DEFER 3); the account is a legacy password row.
- **Pages.** Viewer may open Master Content, Review Queue, Settings (read). Upload and S1 Rewrite are write-only
  pages → `CONTENT_WORKERS` (admin/reviewer/content). Landing page stays `/admin/review`.
- **UI hiding (hint only):** Review Queue actions cell → "View only", no bulk Regenerate, editor read-only without
  Save / Re-validate; Master Content hides Promote; Topbar hides "Mark all read".
- **create_staff_user.py** gains `--password-from-secret <name>` (JSON `{username,password}`, the username must
  match the CLI argument, value never printed) and `--reset` (update an existing row; refused without it).
  (The script was retired at AA-816 D1, S244; see `AA-816-c9.md`.)
- **Smoke viewer mode** is decided at run time from the role the server answered at login (`auth.setup.ts` writes
  `tests/e2e/.auth/admin-role.json`; deleted with the auth dir by the workflow). No new env var.

## Changed (vs the lane plan)

- Migration number 249 (task text), not 248.
- The plan's "do not rely on canWriteAdminPath's default allow" is done in `requireAdmin()` (all proxies), not
  only in `/api/admin/[...path]`.
- The route-policy snapshot is NOT regenerated: no route added, no capability changed.
- `admin_platform.py` is untouched (`CisRole` stays; `STAFF_ROLES` already flows into `active_by_role`).
- `api/core/booking_jwt.py` untouched: the refusal sits in `api/main.py` before minting.

## Tradeoffs

- Viewer reads every open staff GET (same set as reviewer/content: tours, review queue, LLM config, job list,
  decisions log …). None returns customer or tenant-contact PII; staff usernames appear in some (upload history,
  tour history, reviewed_by). Accepted: the alternative is a POLICY entry per GET.
- The Master Content detail panel (`TourDetailPanelV2`) still shows its inline editors to the viewer; a save
  answers "Save failed" (BFF 403). Hiding ~25 editable fields was out of scope; follow-up if the viewer is used
  by people, not only by the smoke.
- The sidebar still shows the Booking admin link to a viewer (the link opens Booking, which then gets no token).

## Should know

- **Merge order:** independent of #836 (no shared file except `api/core/staff_authz.py` POLICY region — this PR
  edits ROLE_PERMS / denial_reason only; a rebase conflict is unlikely but possible). CIS PR first, then the Infra
  byte copy (`chore/copy-cis-249`, the copy guard needs 249 on CIS main), DB Migrate apply 249, then deploy.
  Code that knows `viewer` is harmless before the enum has it (no row can hold it).
- After apply: regenerate `docs/architecture/db-schema-reference.md` (enum gains `viewer`); X1 245 must transfer
  `shared.admin_role` ownership like the other types (it already has to).
- `scripts/` is not in the api image: run the script through the S3-mediated ECS exec (upload, presign, curl to
  `/tmp`, `python3 /tmp/s.py --password-from-secret aa-cis/dev/e2e-test-viewer e2e-claude-code-viewer viewer`).
- After the GitHub `E2E_ADMIN_*` values switch to the viewer (Nghiệp, N1), every new admin-only page added to
  `ADMIN_PAGES` must carry `viewerRefused: true`, or the smoke fails on it.
