"""AA-1003 R0 hotfix (root ADR 0019 d8, d11): the reports that read the B2C path after it was switched off.

On 2026-11-03 (Dev) the ecosystem moved to the B2B-only model: TripPlanner's send-to-advisor answers 410 b2b_only,
Booking's bk_inquiry_pull is off (B2C_INTAKE_ENABLED) and no staff route creates a traveller record. From then on the
inquiry hand-off TripPlanner -> Booking carries nothing, so any check of it can only show zeros or stale rows (rows
sent before the switch-off that were never pulled read as a "gap" or as an aging inbox for 30 days). The reports
therefore:

- TP-03 (inquiry handoff reconciliation) is retired: catalog status `retired`, no generator, no hourly check, no KPI
  snapshot (K-TP-HANDOFFGAP, K-BK-PARKED, K-BK-INQ2CASE), no bell or e-mail; past runs stay readable.
- the inquiry metrics other reports still show (TP-01 inquiries sent and trip to inquiry, the EX-01 demand funnel,
  the DATA-02 inbox and unmatched-key lines) carry the status HISTORICAL: shown for history, never red or amber.

Tenant leads travel on lead.submitted.v1 into the tenant's CRM (ADR 0019 d11); a report on that path is new work once
the contract has a producer, not a re-point of TP-03.
"""
from __future__ import annotations

from datetime import date

B2C_OFF_DAY = date(2026, 11, 3)
# Status of a KPI line whose metric belongs to the switched-off B2C path (never counted as flagged).
HISTORICAL = "historical"
# Job kinds stopped on purpose with the switch-off: OPS-14 does not call them dead schedulers.
RETIRED_JOB_KINDS = ("inquiry_handoff_check",)
NOTE = (f"historical: the B2C inquiry hand-off is off since {B2C_OFF_DAY.isoformat()} (B2B-only model); kept for "
        "history, never flagged")
