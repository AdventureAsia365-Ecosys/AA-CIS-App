"""AA-1097 (l20) — api/routers/admin_pipeline is a package of parts that still behaves as the one module it was.

The split is a pure move. These tests pin the three things that keep callers and tests unchanged
(see api/routers/admin_pipeline/__init__.py): one router in the original route order, one namespace (a patch on the
package reaches every part), and one public module name on every route endpoint.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute

from api.routers import admin_pipeline as ap

PKG_DIR = Path(ap.__file__).parent


def _parts():
    return ap._PARTS


def test_every_part_file_is_in_the_package_tuple():
    files = {p.stem for p in PKG_DIR.glob("*.py")} - {"__init__"}
    assert files == {p.__name__.rsplit(".", 1)[-1] for p in _parts()}


def test_one_router_with_every_route_on_it():
    assert all(getattr(p, "router", ap.router) is ap.router for p in _parts())
    routes = [r for r in ap.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 57
    assert all(r.path.startswith("/admin/") for r in routes)


def test_literal_paths_still_come_before_their_catch_all():
    """Route order matters where a literal segment shares a slot with a path parameter."""
    keys = [(sorted(r.methods)[0], r.path) for r in ap.router.routes if isinstance(r, APIRoute)]
    assert keys.index(("GET", "/admin/tours/{tour_id}/versions/export")) < \
        keys.index(("GET", "/admin/tours/{tour_id}/versions/{version_num}"))
    assert keys.index(("GET", "/admin/tours/export")) < keys.index(("GET", "/admin/tours/{tour_id}/source"))


def test_route_endpoints_report_the_package_module():
    assert {r.endpoint.__module__ for r in ap.router.routes} == {"api.routers.admin_pipeline"}


def test_every_top_level_name_of_every_part_is_on_the_package():
    for part in _parts():
        for name, value in vars(part).items():
            if name.startswith("__"):
                continue
            assert getattr(ap, name) is value, f"{part.__name__}.{name} differs from the package's"


def test_a_name_shared_by_parts_is_one_object():
    seen: dict[str, object] = {}
    for part in _parts():
        for name, value in vars(part).items():
            if name.startswith("__"):
                continue
            if name in seen:
                assert seen[name] is value, f"{name} is two objects across the parts"
            seen[name] = value


def test_a_patch_on_the_package_reaches_every_part_and_is_undone():
    original = ap._resolve_brand_rule
    holders = [p for p in _parts() if "_resolve_brand_rule" in vars(p)]
    assert len(holders) >= 2  # defined in _shared, imported by the engine
    with patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock()) as fake:
        assert all(p._resolve_brand_rule is fake for p in holders)
    assert ap._resolve_brand_rule is original
    assert all(p._resolve_brand_rule is original for p in holders)


def test_patch_object_on_the_package_reaches_the_part_that_uses_it():
    with patch.object(ap, "_boto3") as fake:
        assert ap.metrics._boto3 is fake
    assert ap.metrics._boto3 is ap._boto3


def test_a_created_name_is_removed_again():
    with patch.object(ap, "_aa1097_probe", 1, create=True):
        assert ap._aa1097_probe == 1
    assert not hasattr(ap, "_aa1097_probe")
