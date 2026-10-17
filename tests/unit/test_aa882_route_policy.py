"""AA-882 (S241 GĐ1) — route-policy snapshot: every non-GET /admin route -> the capability it needs.

REPORT-ONLY for now. It fails only when:
- a non-GET route of a GĐ1 router (GD1_MODULES) has no POLICY entry, or its POLICY entry is not
  enforced (the route does not run staff_authz.staff_policy);
- a POLICY route is included under an include_router(prefix=...): staff_policy reads the template
  from scope["route"].path, which on FastAPI >= 0.143 does not carry an include prefix.

Everything else is reported, not enforced: the snapshot of all non-GET /admin routes is kept in
tests/unit/snapshots/aa882_admin_write_policy.json, and a drift (a new write route, a policy change)
is a pytest warning. GĐ3 extends GD1_MODULES to every /admin router and turns the drift into a
failure. Regenerate the snapshot after a deliberate change with:

    python -m tests.unit.test_aa882_route_policy
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

from fastapi.routing import APIRoute

from api.core.staff_authz import POLICY, staff_policy
from tests.unit.test_s241_admin_routes_require_auth import (
    _dependency_calls,
    _inherited_calls,
    _served_routes,
    _walk,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "aa882_admin_write_policy.json"

# Routers whose writes GĐ1 puts under POLICY (design §7 GĐ1: money / model / Jev / job control /
# settings). GĐ3: every router behind /admin.
GD1_MODULES = {
    "api.routers.admin_llm_ops",
    "api.routers.admin_budgets",
    "api.routers.admin_job_runner",
    "api.routers.admin_decisions",
    "api.routers.admin_settings",
}

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _admin_write_routes():
    """(method, full path, route, inherited include dependencies) for every non-GET /admin route."""
    from api.main import app

    for path, route, inherited in _walk(_served_routes(app)):
        if not isinstance(route, APIRoute) or not (path == "/admin" or path.startswith("/admin/")):
            continue
        for method in sorted(route.methods - _READ_METHODS):
            yield method, path, route, inherited


def current_snapshot() -> dict[str, dict]:
    """"METHOD /path" -> {capability or None, module} for every non-GET /admin route."""
    return {
        f"{method} {path}": {"capability": POLICY.get((method, path)), "module": route.endpoint.__module__}
        for method, path, route, _ in sorted(_admin_write_routes(), key=lambda r: (r[1], r[0]))
    }


def _runs_staff_policy(route, inherited, path) -> bool:
    calls = set(_dependency_calls(route.dependant)) | set(_inherited_calls(inherited, path))
    return staff_policy in calls


def test_gd1_write_routes_have_an_enforced_policy():
    missing, unenforced, prefixed = [], [], []
    for method, path, route, inherited in _admin_write_routes():
        key = (method, path)
        if route.endpoint.__module__ in GD1_MODULES and key not in POLICY:
            missing.append(f"{method} {path} ({route.endpoint.__module__})")
        if key in POLICY:
            if not _runs_staff_policy(route, inherited, path):
                unenforced.append(f"{method} {path}")
            if route.path != path:
                prefixed.append(f"{method} {path} (route.path {route.path})")
    assert not missing, ("GĐ1 router write routes without a POLICY entry in api/core/staff_authz.py "
                         "(add the capability they need):\n" + "\n".join(missing))
    assert not unenforced, ("POLICY routes that do not run staff_policy (attach it router-level):\n"
                            + "\n".join(unenforced))
    assert not prefixed, ("POLICY routes behind an include_router(prefix=...): staff_policy would look "
                          "up the wrong template on FastAPI >= 0.143:\n" + "\n".join(prefixed))


def test_every_policy_route_is_a_write_route_seen_by_the_scan():
    seen = {(m, p) for m, p, _, _ in _admin_write_routes()}
    assert not [k for k in POLICY if k not in seen]


def test_report_admin_write_routes_against_the_snapshot():
    """Report-only: warn (never fail) when the non-GET /admin routes or their policy drift."""
    current = current_snapshot()
    assert len(current) > 50, "the scan found too few /admin write routes"
    stored = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    added = sorted(set(current) - set(stored))
    removed = sorted(set(stored) - set(current))
    changed = sorted(k for k in set(current) & set(stored) if current[k] != stored[k])
    unpolicied = sorted(k for k, v in current.items() if v["capability"] is None)
    if added or removed or changed:
        warnings.warn(
            "AA-882 route-policy snapshot drift (report-only; regenerate with "
            "`python -m tests.unit.test_aa882_route_policy`): "
            f"added={added} removed={removed} changed={changed}",
            stacklevel=1,
        )
    # Visible with `pytest -rA` / -s: how much of /admin is still outside POLICY (GĐ3 backlog).
    print(f"AA-882: {len(current) - len(unpolicied)}/{len(current)} non-GET /admin routes under POLICY")


def test_snapshot_file_is_well_formed():
    stored = json.loads(SNAPSHOT.read_text())
    assert stored and all(set(v) == {"capability", "module"} for v in stored.values())
    assert all(k.split(" ", 1)[0] not in _READ_METHODS for k in stored)


if __name__ == "__main__":
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT}")
