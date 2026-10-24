"""AA-978 (CRM-P1) — the CIS membership directory served as contract tenant.membership.v1.

- ``reader``: the snapshot of one agency and the lookup by identity (iss, sub), over ``shared.tenant_members`` and
  ``shared.tenants`` (CIS 276).
- ``feed``: the cursor feed over ``shared.tenant_membership_event`` (written only by the CIS 276 triggers).
- ``writer``: the staff writes of the Tenant 360 Members routes (Content studio role change, remove).
"""
