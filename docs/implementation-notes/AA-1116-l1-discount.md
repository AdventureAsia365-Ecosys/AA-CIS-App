# AA-1116 — contract tenant.entitlements.v1 1.10: the quote discount for Booking (S283 lane l1, CIS part)

Linear (S282 status comment): "Contract 1.10: the quote discount appended to `catalog.v_tenant_billing_v1`, so Booking
drafts carry real discount lines instead of the check line." The pricing parts are in `AA-1116.md`; the CRM agency
onboarding in `AA-1116-l1-provision.md`.

## Decisions

- **Stored by code, not computed in SQL.** The discount needs the line amounts (currency, agreed price, seat minimum,
  extra seats) to pick the larger of founding and combo (S282 decision 6). That rule lives in one pure function,
  `quote.build`, used by Tenant 360 and `/v1/billing`; writing it again in a view would make two rules that drift.
  So CIS 443 adds `shared.tenant_subscription.quote_discount_pct` (numeric(5,2), default 0) and `quote_discount_kind`
  (founding | combo | NULL, CHECKed: the kind exactly when the share is not 0), `services/tenant_account/
  quote_discount.py` fills them from `quote.build`, and CIS 444 serves them as `discount_pct` / `discount_kind`.
- **A share, not an amount.** Booking already prices each line with the same rules (S7 brief in `AA-1116.md`), so it
  takes `discount_pct` off its own line amount. A seat change between two refreshes then never leaves a stale amount;
  only the share can lag, by at most an hour.
- **Columns on the subscription row, not a new table.** The update trigger `tenant_subscription_changed_upd` fires only
  on plan / status / seats / trial end, so a share write adds no account event; no DM-7.1 row or ERD change is
  needed. The guard trigger bumps `updated_at` (only when the share changes: the write is `IS DISTINCT FROM`).
- **When.** Every hour in the `tenant_billing_tick` job (the founding offer ends on a date; plan prices and
  `cis.billing.*` settings change through their own pages; an add-on starts or ends) and at once after Tenant 360
  billing details / subscription terms writes (`refresh_quietly`: its own transaction, never fails the write). The
  saga and the Plans page are covered by the hourly run. Kind `internal` is never refreshed.
- **Fail-soft.** Before 443 is applied `refresh_all` answers `skipped`; a bad tenant is counted `failed` and the run
  goes on; a discount failure never fails the billing move of the same job (`discounts.outcome = failed`).
- **Two migrations, one batch** (MIG-4): 443 (the columns, contract: ALTER TABLE + CHECK, replayed by the conftest)
  and 444 (the view, catalog recipe, skipped by the replay like 438). Readiness raises to 443 only: this code writes
  the columns and never reads the view.

## Changed

- New `services/tenant_account/quote_discount.py`; `services/jobs/tenant_billing_tick_job.py` runs `refresh_all`
  (result key `discounts`); the two Tenant 360 billing routes call `refresh_quietly`.
- `api/core/readiness.py`: 443 in `REQUIRED_CIS_ABOVE_GAP`.
- `docs/contracts/tenant.entitlements.v1.openapi.yaml`: byte copy of the root 1.10 file, pin updated
  (`tests/unit/test_aa1028_tenant_account_contract.py`).

## Tradeoffs

- The share can be up to an hour old after a plan price or a setting change (no hook on those pages). Booking drafts
  once a month, so an hour is far inside its window.
- The quote's thresholds (founding / combo percentages, eligible plans, combo products) stay where they are: the
  `cis.billing.*` settings registry (436). Nothing new is a literal.

## Should know

- Order: root contract PR (1.10) merged first (this PR's contract pin is its sha256), then the migration-only PR
  (443-444) applied by Infra Migration Apply, then this PR.
- Booking: copy `BILLING_COLUMNS` of `tests/integration/test_aa1116_migrations_443_444.py` into its view stub and add
  the discount line (`round(line amount x discount_pct / 100)` in the line currency, kind as the label), replacing the
  zero check line. Not in this lane.
- Local tests of this branch need 443 in the test database: copy `api/migrations/443_*.sql` from the migration branch
  (untracked) before `scripts/pre-push.sh --integration`.
