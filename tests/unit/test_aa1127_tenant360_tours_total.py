"""AA-1127 (C1, S283 UI audit) — Tenant 360: the Tours tab total and pager, and the pipeline runs window.

The tab read "Tours (25)" (the page length) while the header said 892, and showed only the first page with no pager;
"Pipeline (20)" was the query's LIMIT, not a count. Now the tenant branch of GET /admin/tenants/{id}/details pages its
versions on the server (page / page_size, the internal branch already did), lists every version the total counts, and
the summary carries the runs window the UI labels as "latest N". No DB (the SQL shape); the behaviour on Postgres runs
in tests/integration/test_aa1035_tenant_scope_pg.py."""
import inspect
import re

from api.routers import admin
from api.services import tenant360_lists as t360


def _src() -> str:
    return re.sub(r"\s+", " ", inspect.getsource(admin.get_tenant_details))


def test_the_tenant_branch_pages_on_the_server_and_lists_what_the_total_counts():
    src = _src()
    assert "await conn.fetch(_t360.TENANT_VERSIONS_PAGE_SQL, tenant_id, page_size, (page - 1) * page_size)" in src
    branch = re.sub(r"\s+", " ", t360.TENANT_VERSIONS_PAGE_SQL)
    assert "LIMIT $2 OFFSET $3" in branch
    assert "LEFT JOIN gold_aa_internal.published_tours pt" in branch
    assert "WHERE ttv.tenant_id = $1" in branch
    assert "LIMIT 50" not in branch
    # the total the tab and the pager read counts the same rows
    assert "SELECT COUNT(*) FROM gold_aa_internal.tenant_tour_versions WHERE tenant_id = $1" in src


def test_the_runs_window_is_one_value_the_ui_reads():
    src = _src()
    assert "ORDER BY started_at DESC LIMIT $2" in src and "tenant_id, _t360.PIPELINE_RUNS_SHOWN)" in src
    assert '"pipeline_runs_shown": _t360.PIPELINE_RUNS_SHOWN' in src
    assert t360.PIPELINE_RUNS_SHOWN == 20
