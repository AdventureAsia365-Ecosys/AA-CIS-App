# AA-1042 / AA-1045 — agent-only admin evidence spec (lane s270-evidence-specs, S270)

Test-only. Other AA-1042 notes: `AA-1042-anthropic-catalog.md`, `AA-1042-controls.md`, `AA-1042-preview-serving.md`,
`AA-1042-probe-cap.md`, `AA-1042-trace.md`; AA-1045: `AA-1045.md`.

## Decisions

- A separate file, `tests/e2e/admin-evidence/aa1042-1045-evidence.spec.ts`, instead of merging into
  `admin-pages.spec.ts`: it signs in once on its own and runs on its own (`--project=admin-evidence
  aa1042-1045-evidence`), and the AA-1039 PR of the same session edits `admin-pages.spec.ts`.
- New step 2b, the AA-1045 Done-when filter in the UI: the spec reads the busiest (app, tenant) pair through the BFF
  (`GET /api/admin/usage-explorer/query?group=app,tenant`, 7 days, else 30 days), opens the filter drawer, clicks the
  App chip and the Tenant chip (each chip carries the raw value as its `title`; the drawer groups are
  `role="group"` named by the dimension label), closes the drawer with Done, then asserts the address has `f_app` /
  `f_tenant` and `usage-explorer-active-filters` shows both chips, and shoots `aa1045-explorer-filtered-*`.
- Picking a real pair keeps the filtered view non-empty. When no row has both an app and a tenant in 30 days, the
  step is skipped with an annotation and a `FILTER skipped` line, and the rest of the walk still runs.

## Changed

- The draft's run command used `--project admin-evidence aa1042-1045-evidence`. `--project` takes several names, so
  Playwright read the file filter as a second project and failed ("Project(s) not found"). The header now says
  `--project=admin-evidence`.
- `tests/unit/test_aa816_e2e_cis_admin.py`: a new guard says every spec of the folder skips unless its own E2E switch
  is set, and no workflow sets any of those switches.

## Tradeoffs

- The CSV and the drill stay on the unfiltered "provider x stage" view, as drafted: that view always has rows and a
  Route link. The filter step proves the drawer and the chips; the API side of the filter was proven in S268.

## Should know

- GET only (the A10b session is read-only): the explorer query, options, rollup check and the CSV (built in the
  browser from the loaded rows). The CSV is deleted after counting its lines.
- Logs: the app and the first 8 characters of the tenant id, never a tenant name; the screenshots can show a tenant
  name, so review them before copying to a public repo.
