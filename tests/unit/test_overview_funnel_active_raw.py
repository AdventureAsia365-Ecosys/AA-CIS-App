"""S244 — /admin/overview funnel: every stage after raw counts only tours whose raw row is still active,
so no column can exceed "Raw (active)" (a trashed tour kept its S1 drafts and showed 33 vs 32)."""
import re

from api.routers import admin_overview


def _cte(name: str) -> str:
    m = re.search(rf"\b{name} AS \((.*?)\n\),?", admin_overview._FUNNEL_SQL, re.S)
    assert m, name
    return m.group(1)


def test_raw_stage_defines_active():
    assert "source_status = 'active' AND deleted_at IS NULL" in _cte("raw_c")


def test_every_later_stage_filters_on_active_raw():
    for name in ("gen_c", "rev_c", "pub_c", "atom_c"):
        body = _cte(name)
        assert "JOIN silver_aa_internal.raw_tours rt" in body, name
        assert "rt.source_status = 'active' AND rt.deleted_at IS NULL" in body, name


def test_in_review_counts_the_same_queue_as_the_review_page():
    """AA-1127 (C1): the funnel's In review and the Review Queue (and its nav badge) read one queue: aa_internal's."""
    import inspect

    from api.routers.admin_pipeline import review
    body = _cte("rev_c")
    assert f"rq.tenant_id = '{admin_overview._AA_INTERNAL}'::uuid" in body
    # AA-1156: the page and the My work tile read the queue's tenant from one constant
    assert review._REVIEW_TENANT_ID == admin_overview._AA_INTERNAL
    assert "tenant_id = _REVIEW_TENANT_ID" in inspect.getsource(review.admin_review_queue)
    assert "_review_queue_where(_REVIEW_TENANT_ID" in inspect.getsource(review.review_queue_summary)
