# AA-1155 — CIS role editor and people-role presets as data (S287, lane l9-cis-role-editor; spec tasks T13, T14)

Spec: root `.kiro/specs/AA-1155-custom-staff-roles/` (R5.6, R5.7). Builds on T8–T11 (`AA-1155-l2-cis-roles.md`).
Migration CIS 465 ships first in its own migration-only PR (MIG-4); this code PR needs it applied.

## Decisions
- **Same rules as the Booking editor** (AA-Booking `backend/app/auth/role_editor.py`, task T6): code
  `^custom_[a-z][a-z0-9_]{1,46}$`, label 1-80 without control characters, a built-in `based_on`, every capability
  known to the code, none in the approve class (`*.approve`, `*.approve_<x>`, `staff.write`, `staff.admin`), audit
  before and after (`staff.role_created` / `staff.role_updated`, resource `staff_role`). A new role clones the base's
  capabilities minus the approve class and the owner rights (`staff_roles.custom_allowed()`).
- **Two CIS-only guards**: a role keeps at least one capability (400 `no_capabilities`: an empty CIS custom role
  refuses its holders, fail closed), and a role an active account holds cannot be archived (409 `role_in_use`).
  Roles are never deleted (no DELETE grant on `shared.staff_role`).
- **Bases**: `ROLE_PERMS` minus admin, viewer and reviewer (root spec D5 / `staff_role_based_on_chk`).
- **Presets as rows** (`shared.staff_role_preset`, CIS 465): `booking_mode` keep / set / clear and `crm_mode` keep /
  replace / add describe what today's four presets did without a code branch each; the Pricing team rule became
  `additive_only` + `cis_keep_roles` / `booking_keep_roles`. `plan()` is pure and mirrored in
  `frontend/lib/staffPresets.ts` (the "Current" mark, the confirm text, Add staff).
- **Guards stay in code**: no field can hold an owner flag (the table has none, every body forbids extra fields), the
  directory's self-demote and last-owner checks run on the planned roles, an additive preset refuses
  (409 `preset_would_replace`) before any write. An unknown or archived preset is 400 `preset`; a missing table is
  503 `presets_unavailable` (never a 500).
- **Every role code checked on save and again on apply**: a preset may name a CIS or Booking custom role, which must
  be active when saved (400) and is re-checked by the directory when applied.
- **"Current" preset** in the account panel = the first active preset (sort order) whose plan changes nothing; the
  Pricing team preset is now one row among the presets, no longer a separate block behind "Show all roles".
- **Editors on the Staff page** (`PresetEditor.tsx`, `RoleEditor.tsx`, kit only): everyone with `staff.read` reads
  them; `can_edit` (owner on a Cognito session) shows the write buttons; the backend re-checks the owner in the write's
  transaction. Preset names and descriptions are data (shown as an owner wrote them, not translated).

## Changed
- The four preset literals left `api/services/staff_presets.py`, `frontend/lib/staffPresets.ts`, `PeoplePresets.tsx`
  and `StaffManage.tsx` (`PEOPLE_PRESETS`, `PEOPLE_PRESET_ROLES`, `pricingTeamPlan`, the per-preset message keys). The
  backend tests of the four presets read the seeded rows (`tests/unit/aa1155_presets_seed.py`).
- `ChangeRolesBody.preset` is a code pattern instead of an enum (openapi.json regenerated with the pinned
  fastapi / pydantic).
- Readiness: `REQUIRED_CIS_ABOVE_GAP` gains 465.

## Tradeoffs
- No preset delete: archive keeps the history of what an audit row's `details.preset` meant.
- Additive keep lists are explicit role codes rather than "roles that hold capability X": simpler to read and to
  check, at the cost of an owner updating the list when a new role should count as "already has the rights".
- The preset editor validates in the browser too (`presetFormProblem`) so an owner sees the rule before a 400; the
  backend stays the authority.
- Thresholds: the only bounds added are column sizes / CHECK bounds (label 80, description 300, keep list 16, sort
  0-999, request list sizes), marked `technical-constant`; no policy value was added.

## Should know
- Routes: `GET/POST /admin/platform/roles/cis`, `PATCH /admin/platform/roles/cis/{code}`, `GET/POST
  /admin/platform/presets`, `PATCH /admin/platform/presets/{code}` (router `api/routers/admin_platform_roles.py`,
  POLICY `staff.read` / `staff.write`; snapshots `aa882_staff_route_policy.json`, `aa1035_role_route_matrix.json`).
- Tests: `tests/unit/test_aa1155_role_editor.py` (T13), `tests/unit/test_aa1155_staff_presets.py` (T14),
  `tests/integration/test_aa1155_editors_pg.py` (both, as `cis_app` on real Postgres with 462 + 465),
  `frontend/lib/staffPresets.test.mjs` (plan parity, no preset literal left), `frontend/lib/staffRoleEditor.test.mjs`.
- The integration test applies `api/migrations/465_*.sql`: it runs once CIS 465 is on main (MIG-4 chain).
- Renumber: if 465 is taken at merge, `git grep '465'` in readiness, the tests and these notes.
