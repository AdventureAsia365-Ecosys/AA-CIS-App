# AA-1029 R4: the Content studio Marketplace "from" price, CIS part (S268, lane portal-from)

## Decisions

- **Where the price joins the pool**: CIS serves the Marketplace (`GET /v1/tours/pool`) from its own tables, so the
  smallest boundary-correct change is that CIS attaches AA-Booking's public price to each row. It reads only the
  published contract view `booking.v_from_prices` (price.from.v1), granted to `cis_app` by Booking bk-040, exactly
  like the ops / supply views CIS already reads (bk-017, bk-023, bk-025). Rejected: a Booking HTTP endpoint for tenant
  tokens (a new route class in Booking for one read) and a read through AA-Partner (a studio-only member may have no
  CRM agency; AA-Partner never owns prices, root ADR 0008a / 0010).
- **On MAIN (cis_app), after the tenant block**, keyed by the catalog key, like the job-status read of the same file
  (AA-859 MAIN exception): the price has no tenant dimension and the tenant pool role `aa_app_user` keeps no privilege
  on schema `booking`. One extra indexed read per page (at most 100 keys).
- **Catalog key** = `raw_tours.source_key`, written as `COALESCE(source_group_id, tour_id)::text` (258's expression)
  so the SQL works on both pools and on the CI replay. The key is dropped before the answer (`catalog_source_id`).
- **Fail soft**: the view missing (bk-039), no grant (bk-040 not applied yet), a timeout or any error = every
  `from_price` null and one `aa1029_from_price_unreadable` log line; the page never fails. The read runs in its own
  transaction so a shared connection only rolls back a savepoint.
- **Shape** = TripPlanner's (`{amount (text), currency, basis, pax, season, valid_to (ISO date)}`), so the two UIs
  read one shape; `valid_to = 9999-12-31` means open-ended (contract).

## Changed

- `tests/integration/rls_catalogue_rules.py`: allowlist key `booking.v_from_prices:cis_app` (bk-040). AA-CIS-Infra
  holds a byte copy (`db/rls_catalogue_rules.py`) changed in its consumer-grant PR: merge this CIS PR first (the
  Infra copy guard compares with CIS main).
- `docs/api/openapi.json`: the pool route's description (pinned fastapi 0.161.1 / pydantic 2.14.1).

## Tradeoffs

- `price_raw` stays in the answer (the studio no longer shows it as a price; other readers may still use the text).
- The price is the AA catalogue RRP; per-tenant tiers are AA-1030.

## Should know

- Order: none required (fail soft). For a live price on Dev: Booking bk-040 applied (Migration Apply, contract) and
  the CIS deploy, then the studio PR.
