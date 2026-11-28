"""AA-1054 (S297; CIS 495) — the pilot fence switch on Platform › Settings (area "platform").

``cis.platform.pilot_e2e_fence``: when on, the CIS API refuses every staff session of a Dev-only E2E identity (any
address of the E2E mailbox domain, root ADR 0006a A10 ... A10h) with 403 ``e2e_pilot_fence``
(:mod:`api.core.pilot_fence`, the code PR), and the AA-Booking API does the same, reading the value in force through
``catalog.v_pilot_fence_v1`` (CIS 496). Off by default. The key equals the CIS 495 seed
(tests/integration/test_aa1054_migration_495.py and the settings-lint reader check,
tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

PILOT_E2E_FENCE = "cis.platform.pilot_e2e_fence"

ALL = (PILOT_E2E_FENCE,)
