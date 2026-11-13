# api/routers/admin_pipeline/ — Admin-only pipeline endpoints, mounted at /admin/* (no Lambda Authorizer at API GW).
# Auth: x-admin-secret header only (no tenant JWT accepted) — EXCEPT /brand-identity GET/POST
# (see _resolve_brand_tenant_id, AA-424): those two accept a tenant JWT too, real tenant_id first.
"""The admin pipeline router, split by domain out of one 5,813-line module (AA-1097, a pure move).

Parts (each under the 800-line file ratchet of .github/code-health.json):

  _shared         the one `router` (prefix /admin, staff_policy_if_staff), `logger`, the helpers several parts share
  _engine         the S1 rewrite engine of one tour (_execute_run_tour, _run_tour_safe)
  _revalidate     the S1 re-validation of an edited version (_revalidate_tour)
  runs            run-tour, SEO prefetch, async jobs, progress, Bedrock batch, brand rules
  uploads         upload URL, ingest-s3, upload staging, raw tour lifecycle, upload history
  tours           tours-ready / trashed / all tours, CSV export, export audit
  review          Review Queue (queue, claims, approve, reject, supersede, dismiss)
  versions        promote, source, SEO context, versions export, DOCX export
  tour_detail     version detail, versions, history, detail, country / raw / generated edits, revalidate
  metrics         metrics, SEO metrics, library, spot workers, infra status
  brands          /admin/brands
  brand_identity  /admin/brand-identity (staff or tenant JWT)
  billing         /admin/billing

Three things keep the package behaving as the single module did, so callers and tests need no change:

1. One router. Every route part registers on `_shared.router` (no include_router nesting), and this file imports the
   route parts in the original source order, so the served route table, its order and docs/api/openapi.json are
   byte-identical to the single module.
2. One namespace. Every top-level name of every part is re-exported here (`from api.routers.admin_pipeline import X`
   keeps working for the services and scripts that import helpers lazily), and setting a name on the package
   (`patch("api.routers.admin_pipeline._resolve_brand_rule")`, `patch.object(admin_pipeline, "_boto3")`) also sets it in
   every part that holds that name: a patch reaches every caller, as it did when they shared one module's globals.
3. One module name. Each route endpoint reports `api.routers.admin_pipeline` as its `__module__` (the public module,
   like numpy's `set_module`): the staff route policy snapshot and the per-router sample test key routes by it.
"""
import sys as _sys
import types as _types

from api.routers.admin_pipeline import _shared, _engine, _revalidate  # noqa: F401  helpers first (no routes)
from api.routers.admin_pipeline import (  # noqa: F401  route parts, in the original route order
    runs, uploads, tours, review, versions, tour_detail, metrics, brands, brand_identity, billing,
)
from api.routers.admin_pipeline._shared import router

_PARTS = (_shared, _engine, _revalidate, runs, uploads, tours, review, versions, tour_detail, metrics, brands,
          brand_identity, billing)

for _part in _PARTS:
    for _name, _value in vars(_part).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

for _route in router.routes:
    _route.endpoint.__module__ = __name__


class _OneNamespace(_types.ModuleType):
    """The package module: a name set (or deleted) on it is set (or deleted) in every part that holds the name."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for part in self.__dict__.get("_PARTS", ()):
            if name in part.__dict__:
                setattr(part, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for part in self.__dict__.get("_PARTS", ()):
            if name in part.__dict__:
                delattr(part, name)


del _part, _name, _value, _route
_sys.modules[__name__].__class__ = _OneNamespace
