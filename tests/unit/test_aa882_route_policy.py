"""AA-882 (S241 GĐ3) — route policy, ENFORCING: every staff route is under staff_authz.

Scope: every route under staff_authz.STAFF_PATH_PREFIXES (/admin, /v1/pipeline, /v1/rules,
/v1/social) — the paths that accept the admin secret the staff BFF holds. CI fails when:
- a non-GET staff route has no POLICY entry (a new write must say which capability it needs);
- a staff route (any method) does not run staff_policy or staff_policy_if_staff, so a POLICY entry
  added later for it (or for a GET) would silently not apply;
- a route under /admin/platform/ (any method; Platform section, admin-only) has no POLICY entry;
- a POLICY entry names no real route (except AWAITING_ROUTE: routes of another open PR);
- a staff route is included under an include_router(prefix=...): staff_policy reads the template from
  scope["route"].path, which on FastAPI >= 0.143 does not carry an include prefix;
- the routes or their policy drift from tests/unit/snapshots/aa882_staff_route_policy.json.

After a deliberate change (a new route, a different capability) regenerate the snapshot with:

    python -m tests.unit.test_aa882_route_policy
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from api.core.staff_authz import (
    AWAITING_ROUTE,
    POLICY,
    STAFF_PATH_PREFIXES,
    staff_policy,
    staff_policy_if_staff,
)
from tests.unit.test_s241_admin_routes_require_auth import (
    _dependency_calls,
    _inherited_calls,
    _served_routes,
    _walk,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "aa882_staff_route_policy.json"

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_POLICY_DEPENDENCIES = {staff_policy, staff_policy_if_staff}


def _is_staff_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES)


def _staff_routes():
    """(method, full path, route, inherited include dependencies) for every staff route."""
    from api.main import app

    for path, route, inherited in _walk(_served_routes(app)):
        if not isinstance(route, APIRoute) or not _is_staff_path(path):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, path, route, inherited


def current_snapshot() -> dict[str, dict]:
    """"METHOD /path" -> {capability or None, module} for every staff route (None: open read)."""
    return {
        f"{method} {path}": {"capability": POLICY.get((method, path)), "module": route.endpoint.__module__}
        for method, path, route, _ in sorted(_staff_routes(), key=lambda r: (r[1], r[0]))
    }


def _runs_policy(route, inherited, path) -> bool:
    calls = set(_dependency_calls(route.dependant)) | set(_inherited_calls(inherited, path))
    return bool(calls & _POLICY_DEPENDENCIES)


def test_scan_sees_the_staff_routes():
    routes = list(_staff_routes())
    assert len(routes) > 190, "the scan found too few staff routes"
    assert sum(1 for m, *_ in routes if m not in _READ_METHODS) > 80
    assert {p for _, p, _, _ in routes if p.startswith("/v1/")}, "no /v1 staff route found"


def test_every_staff_write_has_a_policy():
    missing = [f"{m} {p} ({r.endpoint.__module__})" for m, p, r, _ in _staff_routes()
               if m not in _READ_METHODS and (m, p) not in POLICY]
    assert not missing, ("Staff write routes without a POLICY entry in api/core/staff_authz.py — add the "
                         "capability each needs (see the mapping rule above POLICY):\n" + "\n".join(missing))


def test_every_staff_route_runs_the_policy():
    unenforced = [f"{m} {p} ({r.endpoint.__module__})" for m, p, r, inh in _staff_routes()
                  if not _runs_policy(r, inh, p)]
    assert not unenforced, ("Staff routes whose router does not run staff_policy / staff_policy_if_staff "
                            "(attach it router-level):\n" + "\n".join(unenforced))


def test_every_platform_route_has_a_policy():
    """/admin/platform/* is the admin-only Platform section (S241 §4): no open read there, so a route
    whose template differs from its POLICY entry (e.g. a renamed path parameter) fails here."""
    missing = [f"{m} {p}" for m, p, _, _ in _staff_routes()
               if p.startswith("/admin/platform/") and (m, p) not in POLICY]
    assert not missing, "Platform routes without a POLICY entry:\n" + "\n".join(missing)


def test_every_policy_entry_is_a_staff_route():
    seen = {(m, p) for m, p, _, _ in _staff_routes()}
    stale = sorted(k for k in POLICY if k not in seen and k not in AWAITING_ROUTE)
    assert not stale, f"POLICY entries that match no staff route (typo, renamed or removed route): {stale}"
    assert set(AWAITING_ROUTE) <= set(POLICY), "AWAITING_ROUTE names a route POLICY does not cover"


def test_policy_routes_are_not_behind_an_include_prefix():
    prefixed = [f"{m} {p} (route.path {r.path})" for m, p, r, _ in _staff_routes() if r.path != p]
    assert not prefixed, ("Staff routes behind an include_router(prefix=...): staff_policy would look up "
                          "the wrong template on FastAPI >= 0.143:\n" + "\n".join(prefixed))


def test_snapshot_matches_the_app():
    current = current_snapshot()
    stored = json.loads(SNAPSHOT.read_text())
    added = sorted(set(current) - set(stored))
    removed = sorted(set(stored) - set(current))
    changed = sorted(k for k in set(current) & set(stored) if current[k] != stored[k])
    assert not (added or removed or changed), (
        "Staff route policy drifted from the snapshot. If the change is deliberate, regenerate it "
        "with `python -m tests.unit.test_aa882_route_policy` and commit the JSON:\n"
        f"added={added}\nremoved={removed}\nchanged={changed}"
    )


def test_snapshot_file_is_well_formed():
    stored = json.loads(SNAPSHOT.read_text())
    assert stored and all(set(v) == {"capability", "module"} for v in stored.values())
    assert all(v["capability"] for k, v in stored.items() if k.split(" ", 1)[0] not in _READ_METHODS)


if __name__ == "__main__":
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT}")
