"""AA-1028 R1 (root ADR 0019 d1, d2) — the CIS tenant control plane served as contract tenant.entitlements.v1.

- ``reader``: one tenant's account snapshot (status, region, kind, subscriptions, resolved entitlements) over
  ``shared.tenants``, ``shared.tenant_subscription`` and ``shared.v_tenant_entitlement_v1`` (CIS 353-355).
- ``feed``: the cursor feed over ``shared.tenant_account_event`` (written only by the CIS 353 / 354 triggers).
"""
