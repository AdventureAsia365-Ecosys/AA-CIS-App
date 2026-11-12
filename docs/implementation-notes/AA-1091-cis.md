# AA-1091 (CIS part) — structured reference price and the pricing team's access

Lane l12-cis-reference-price of S278. Items 5 and 6 of the issue, plus the CIS side of the customer-service guide.
The Booking side (workbench, suggested components, bulk import, provisional price) is another lane.

## Decisions

- **One row per catalog key, not per source version.** `silver_aa_internal.tour_reference_price` is keyed by
  `raw_tours.source_key` (= the `source_id` of `catalog.tour.v1`, `COALESCE(source_group_id, tour_id)`), so a re-upload
  of a tour (a new source version) keeps its reference price. The row remembers the text it was set against
  (`price_raw_seen`); when the live version's `price_raw` differs, the status is `needs_review` ("the source text
  changed") until a person confirms or corrects it. No FK (the key is not unique on `raw_tours`), no tenant column (AA
  master tours only; the writer checks the key against the AA master tenant's active rows), so no RLS (like CIS 392).
- **A table, not new columns on `raw_tours`.** Columns on `raw_tours` would be lost or copied by hand at every supersede
  (`tour_lifecycle.supersede` inserts a new row) and could not carry CHECKs in an expand file.
- **Status rule (one definition, used by the admin page and the Booking view):** saved + (`needs_review` or text
  changed) = `needs_review`; saved + reviewed = `confirmed`; saved, not reviewed = `parsed`; nothing saved = `suggested`
  (the parser finds a price) or `missing`. "No price" (the page's default filter) = `suggested` + `missing`.
- **Parser reads, never invents.** `services/reference_price/parse.py`: the first amount with a currency next to it (the
  same amount rule as Booking's `parse_reference`, so both sides read "$1,722/person" alike), a lone currency code
  elsewhere in the text counts, a bare number is `no_currency` (nothing found). The basis comes from words: "from /
  starting at" = `from`, "per group / vehicle / private group" = `per_group`, "per person / pp / pax" = `per_person`,
  none = `per_person` with the doubt `basis_assumed`. Doubt codes: `several_amounts`, `several_currencies`, `tiered`,
  `basis_assumed`, `basis_unclear`, `amount_small`. No currency conversion (the marketplace's FX table is not reused: a
  reference price keeps the supplier's currency).
- **Every staff entry is a confirmation.** An inline save, an accepted suggestion and an imported row are saved as
  reviewed by that person (origin `staff` / `parsed` / `import`). Only "Read source texts" saves unreviewed rows (origin
  `parsed`, `needs_review` when the parser had doubts). That run never touches a confirmed price or a staff / import
  entry; it re-parses only an unconfirmed parse whose text changed.
- **Excel import is all-or-nothing.** The first call is always the DRY RUN (`dry_run` defaults to true); an apply with
  any error line is refused (422 with the per-line plan) and nothing is saved. A row with Amount, Currency and Basis all
  empty is skipped (kept as it is). A row whose values equal the saved price is `unchanged` WHATEVER the status: the
  export prefills Amount / Currency / Basis / Note with every saved price, also an unreviewed parse, so re-importing
  the file never confirms a price nobody looked at (review fix; first version counted only a confirmed price as
  unchanged, so a round trip confirmed every parsed / needs-review row). Only a value that differs is a staff entry
  (confirmed). An optional **Confirm** column (`yes`, also y / x / ok / true / 1; empty or no = nothing) confirms a
  row's saved price, or on an emptied row the parser's suggestion, as it is (`store.confirm`, origin kept, audit
  `reference_price.confirmed`): the team can review in Excel. Columns are found by header, so a re-ordered or trimmed
  sheet works; Key / Amount / Currency / Basis are required, Note and Confirm optional.
- **No formula in the export.** openpyxl stores any str starting with "=" as a formula; every exported cell that came
  out as a formula is set back to a string (inline string in the file), so a source text such as `=HYPERLINK(...)` is
  text in Excel and comes back unchanged on import (no apostrophe to strip).
- **Binary over JSON.** The REST API Gateway in front of the API is not binary safe (AA-741), so the export answers base64
  JSON and the page builds the file; the import sends base64 JSON. 5 MB file cap, 5,000 rows.
- **Capabilities.** `reference_price.read`: admin, content_lead, content, customer_success, finance (not the read-only
  viewer: AA-878 refuses it on every listed read). `reference_price.write`: admin, content_lead, customer_success. The
  customer-service team's CIS role is `customer_success` (it holds no `content.read`, so every read route has its own
  POLICY entry).
- **Booking contract: a v2 view, not columns on v1.** `catalog.v_tour_price_reference_v1` (CIS 365) is "one row per live
  tour with a source text"; listing every live tour (the 281 without text can get a staff price) changes that meaning,
  so CIS 402 adds `catalog.v_tour_price_reference_v2` (one row per live tour: text, amount, currency, basis, status,
  origin, reviewed / updated times) and keeps v1 until Booking stops reading it.
- **Migrations.** 401 expand (the table, its CHECKs, an index, the `cis_app` grant). 402 contract (the catalog recipe
  view, the `catalog_owner` SELECT on the 401 table: an expand file may grant only to the owner's runtime roles, the
  preflight refused it in 401). One migration-only PR (batch cis 401-402); the code PR raises readiness to 401 only
  (`REQUIRED_CIS_ABOVE_GAP`), never to 402.
- **Item 6: a "Pricing team" preset, not a new Booking role.** The Staff page already writes `shared.staff_app_grant` role
  `contracting` and mirrors it to the WORKFORCE group `booking-contracting`, and Booking already maps that group to its
  `contracting` role, which holds `pricing.write`, `supply.write` and `trip_plan.write` (AA-Booking
  `backend/app/auth/staff.py`). What was missing is a one-click, explained grant: `preset="pricing_team"` gives CIS
  `customer_success` and Booking `contracting` and is **additive only**: it fills an empty slot (CIS `viewer`, no
  Booking grant), keeps a role that already has those rights (admin / content_lead / customer_success in CIS, admin /
  contracting in Booking), and refuses with 409 `preset_would_replace` (naming the roles) when it would have to replace
  any other role, because a person holds ONE role per app (review fix: the first version replaced CIS finance / content
  / support... and Booking sales / finance / content_creator, so a finance member lost invoices and payments in one
  click). The Staff drawer computes the same plan (`pricingTeamPlan`): the button is disabled with the reason when a
  role would be replaced, and the confirm dialog lists each before -> after. Never touches CRM roles, one audit row with
  `details.preset`. The role texts of the invitation e-mail now say what contracting does.

## Changed (against the issue text)

- Item 5's "bulk edit" is a server-paged table with an inline editor in every row (amount, currency, basis, note, Save,
  Accept, Clear) plus "Accept N selected" for the bulk acceptance of suggestions; there is no separate bulk-edit form.
- The page also has "Read source texts" (a DRY RUN, then save the parser's suggestions as Not reviewed), so the 892 live
  tours start with a structured value where the text allows it.
- No Booking change is needed for item 6 (Booking reads the group, the role matrix already gives contracting the pricing
  writes). Booking must still switch its reference read from v1 to v2 (listed for the orchestrator).

## Tradeoffs

- The pricing preset refuses instead of replacing a lateral role: an owner who really wants a finance member to become
  Customer Success changes the role by hand (an explicit choice), rather than losing rights silently.
- Confirm in Excel reuses the Accept path (origin kept), so a confirmed parse still says `parsed` in the history; a
  changed value says `import`.

- The list loads every in-scope tour (~1k rows) and filters / pages in Python: simple, and the counts and countries are
  those of the whole filtered set (lesson S218). At ~10k tours the filter should move into SQL.
- No FK and no cascade: a key whose tours are all gone keeps its row until the tour lifecycle purge removes it (DM-7.1
  row: kept while the key exists).
- The parser's doubt codes are deliberately broad (any second different amount is `several_amounts`): a false "needs
  review" costs one click, a wrong silent price costs a wrong quote reference.
- Import refuses the whole file on one bad line instead of saving the good lines: the team fixes the file and imports
  again, and the audit never holds half a file.

## Should know

- Before CIS 401 is applied every route answers 503 and the page says "not available yet on this server".
- `tests/integration/test_aa1091_reference_price_db.py` is skipped while 401 is not on the branch (the code PR is cut from
  `main` before the migration PR merges); once `main` has 401 the conftest replays it and the module runs. It was run
  locally with 401 copied in (2 passed).
- The `routeControl.test.mjs` "no issue ids" failure in `npm run test:unit` is on `main` already (not this change).
- Every audit row: action `reference_price.saved | confirmed | cleared | parsed | imported`, resource type
  `tour_reference_price`, resource id = the catalog key, details before / after (amount, currency, basis, origin, note,
  needs_review, reviewed_by). History: `GET /admin/reference-prices/{source_key}/history`.

## Customer-service guide — CIS part (draft for the docs page)

**Who can do this:** a staff member with the CIS role Customer Success (or Content lead / Administrator). A platform
owner gives it on Platform › Staff & access: open the person, then **Apply pricing preset** (CIS Customer Success +
Booking Contracting in one save). The preset only adds roles: when the person already holds another role (for example
Finance), the page says so and the owner sets the roles by hand. The person signs in again.

**Where:** CIS admin › AA Catalogue › **Reference prices**.

**What a reference price is:** the price the source sheet names for a tour (amount, currency, basis). It helps Booking
staff check the price their cost sheet computes. It is never a selling price and is never sent to an agency or a
traveller.

**Statuses**

| Status | Meaning | What to do |
|---|---|---|
| No price / Suggested | Nothing saved yet; "Suggested" means CIS read a price in the source text | Check the suggestion, then **Accept** or correct and **Save** |
| Not reviewed | Saved by "Read source texts", nobody checked it | Check, then **Accept** |
| Needs review | CIS had doubts (several prices, price by group size, no "per person" word...) or the source text changed | Read the source text, correct the row, **Save** |
| Confirmed | A person confirmed it | Nothing |

**Fill one tour:** type the amount (1450 or 1,450), pick the currency and the basis (Per person, Per group, From = the
lowest per-person price), add a note if useful (no personal data), press **Save** (or Enter).

**Fill many tours fast**

1. Start with **Read source texts**: CIS shows what it would save (a DRY RUN); press **Save these suggestions**.
2. Filter **Not reviewed** (or click the tile), check the rows, select them and press **Accept N selected**.
3. For the rest, **Export Excel** (the file follows the current filter), fill or correct Amount / Currency / Basis in
   Excel (the yellow columns), keep the Key column. A row you leave as it is keeps its status; to confirm a CIS-read
   price as it is, type **yes** in **Confirm** (on a row with an empty price, yes saves the Suggested values).
4. **Import Excel** → **Check the file (DRY RUN)**: CIS lists every error and every change. Fix the errors in the file
   and check again. When there is no error, **Apply import**.

**Good to know:** every change is recorded (who, when, before / after). A tour whose source text changes after a new
upload goes back to Needs review. Clearing a price keeps the source text.
