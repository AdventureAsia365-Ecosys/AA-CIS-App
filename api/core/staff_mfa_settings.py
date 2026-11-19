"""AA-1111 (S285, slice S2; CIS 459) — the administrator MFA switch on Platform › Settings (area "platform").

``cis.platform.admin_mfa_required``: when on, the CIS API refuses a staff session of an administrator (``cis-admin``
group, or a staff member with the owner flag) whose sign-in did not use an authenticator code (the access token's
``amr`` claim lacks ``otp``) with 403 ``mfa_required`` (:mod:`api.core.staff_mfa`). Off by default; off, the check only
logs. The key equals the CIS 459 seed (tests/integration/test_aa1111_migration_459.py and the settings-lint reader
check, tests/unit/test_s279_settings_lint.py); the reader reads the value in force through ``shared.settings``.
"""
from __future__ import annotations

ADMIN_MFA_REQUIRED = "cis.platform.admin_mfa_required"

ALL = (ADMIN_MFA_REQUIRED,)
