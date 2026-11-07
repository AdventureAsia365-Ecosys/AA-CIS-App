"""AA-645 — the gate / report-only code sets of the S1 code checks. The definitions live in
`services/quality/s1_gates.py` (AA-1024 G17: the API's wave start reads them too); this module re-exports them for the
aa645 scripts. Pure: services/quality/s1_gates.py imports nothing.
"""
from __future__ import annotations

from services.quality.s1_gates import (  # noqa: F401
    DAY_GATE_CODES,
    GATE_CODES,
    NOT_A_PAGE_FAULT,
    REPORT_CODES,
    gate_label,
    is_gate,
    is_report,
    page_gate_codes,
)
