# AA-1116 (S284, lane l12) — the seat-overage line on a per-seat plan

The S282 progress report (G13) found that a per-seat plan (CRM Core / Pro: price per seat, 3 seats included and
minimum) bills "price x seats" as one line: there is no line for the seats above what the plan includes, and the
plan's extra-seat price (Plans page, `price_extra_seat_usd` / `price_extra_seat_vnd`) is ignored on such a plan.
This lane makes the quote (this repo) and the Booking monthly draft (AA-Booking, same rule) split the seats.

## Decisions

- **Split, not a new price rule** (`quote.seat_split`): billed seats stay max(seats, seats_min, 1) (decision 2). The
  plan includes max(`seats_included`, `seats_min`, 1) of them at the seat price; the rest are `extra_seats`. A plan
  with no `seats_included` has no extra seats.
- **Extra-seat price**: the plan's extra-seat price in the billing currency when the Plans page has one, else the seat
  price. Option B left the extra-seat price empty on the CRM plans, so today's amounts do not change; an administrator
  who sets one on Plans makes the extra seats cost that. No literal and no new setting: both prices are plan rows.
- **Agreed (locked) seat price covers the extra seats** (as the 435 comment says: seats added during a lock are billed
  at the locked seat price).
- **Quote line shape**: one line per subscription as before (`quantity` = every billed seat, `amount` = seats at the
  seat price + extra seats at their price), plus `extra_seats` and `extra_seat_price` (additive fields). A plan-fee
  plan's extra seats (already in its amount) are now listed the same way. Tenant 360 shows "N extra seats at X" on
  the line. Booking writes the split as a `subscription` and an `extra_seats` invoice line.
- **What is "seats"**: the subscription's seats on Tenant 360 (the seats the agency pays for, which the CRM uses as
  its member limit through `crm.seats`). The CRM members actually in use are not billed from the CRM: no contract
  carries them to CIS or Booking (cross-app reads are HTTP contracts only, root ADR 0008a), and the CRM refuses an
  invitation over the limit. A member count above the limit (the limit lowered later, or a `crm.seats` override above
  the subscription's seats) is a follow-up, see "Should know".

## Changed

- `services/tenant_account/quote.py`: `seat_split`, `Line.extra_seats` / `extra_seat_price`, the per-seat amount.
- `frontend/lib/tenantPlans.ts` (`QuoteLine`), `Tenant360Commercial.tsx` (the tag), `commercial.messages.ts` (EN/VI).
- Tests: `tests/unit/test_aa1116_pricing.py` (overage at the seat price and at the extra-seat price, VND, combo
  discount over the extra seats, agreed price, the split table, a plan-fee plan's extra seats listed).

## Tradeoffs

- One quote line with two prices instead of two quote lines: the quote line is the subscription (its discount, terms
  and trial flags are per subscription); the invoice draft is where the two lines matter.
- No contract change: `catalog.v_tenant_billing_v1` already carries `seats`, `seats_included`, `seats_min` and the
  extra-seat prices (CIS 438, contract 1.9), so no migration.

## Should know

- Follow-up (not built): billing CRM members in use above the paid seats needs the CRM to publish its seat use to the
  billing side (a CRM contract read by Booking, or a CIS meter). Today a `crm.seats` override above the subscription's
  seats gives free seats on purpose (a staff grant).
- Live check: Tenant 360 of a synthetic agency, CRM subscription seats 5 (plan includes 3) -> the quote line says
  "2 extra seats at 19.00 USD"; the Booking SaaS draft of that agency has the lines "CRM Core ..., 3 seats" and
  "Extra seats over 3, CRM Core ...".
