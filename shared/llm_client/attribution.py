"""AA-1058 — cost attribution rules: which object every ``shared.llm_call_log`` row of a stage must name.

The S273 system picture could not measure cost per tour or per piece: S1 / A1 / A3 rows had no ``tour_id``, T9 / T10
rows no ``content_piece_id``. A row gets its key from the gateway context (``call_log.bind_attribution`` /
``call_log.bind_job``), bound once at the stage's entry point, or from the call site itself (``tour_id=`` /
``content_piece_id=``):

- ``RULES``: stage (exact name, or a prefix ending in ``_``) -> the key its rows carry. The first match wins, so an
  exact name listed before a prefix overrides it.
- ``BINDERS``: key -> the entry points that bind it ("path::function", a method as "path::Class.method"). The guard
  ``tests/unit/test_aa1058_cost_attribution.py`` checks each one still calls its binder with that key.
- ``BOUND_MODULES``: key -> the modules whose ledger writes for a ruled stage take the key from the context (their
  call sites pass no key): every one of them only runs below a binder. A new module that writes a ruled stage without
  passing the key fails the guard until it is listed here (after checking it runs below a binder) or passes the key.

``call_log`` also logs ``llm_call_log_attribution_missing`` at run time for a tour / piece row written without its
key (never refused: the call was billed). ``job_id`` rows are checked by the guard only: the same stages run outside a
job too (the sync /admin endpoints), where NULL is right.
"""
from __future__ import annotations

from typing import Optional

TOUR = "tour_id"
PIECE = "content_piece_id"
JOB = "job_id"

# Order matters: exact names before the prefixes they would otherwise fall under.
RULES: tuple[tuple[str, str], ...] = (
    # A3 Segment recompute (services/jobs/recompute_job.py and the a3_atomize / research tails): platform work with no
    # one tour, its cost is the recompute job's (shared.job via llm_call_log.job_id).
    ("a3_demand", JOB),
    ("a3_question_scope", JOB),
    ("a3_question_landing", JOB),
    ("a3_segment_type", JOB),
    ("a3_segment_match", JOB),
    # A1 / S1 rewrite of one tour (writer, judge, flag fix, brand audit, grounding, Jev a1_*), T5 / A3 atomize.
    ("s1_", TOUR),
    ("a1_", TOUR),
    ("t5_", TOUR),
    ("a3_atomize", TOUR),
    # T9 writer and its repairs, T10 checks: one content piece.
    ("t9_", PIECE),
    ("t10_", PIECE),
)

# Entry points that bind each key. A method is "Class.method".
BINDERS: dict[str, tuple[str, ...]] = {
    TOUR: (
        "shared/jobs/worker.py::Worker._execute_job",                       # every job whose payload names a tour
        "api/routers/admin_pipeline.py::_execute_run_tour",         # S1 rewrite (job, sync endpoint, batch ingest)
        "api/routers/admin_pipeline.py::_revalidate_tour",          # S1 re-validation of an edited version
        "services/acp_contract/a3_atomize.py::run_a3_atomize",      # A3 atomize (job and inline callers)
        "services/seo_intelligence/s1_prefetch.py::jev_review",     # a1_seo Jev of one tour (a job covers many)
    ),
    PIECE: (
        "shared/jobs/worker.py::Worker._execute_job",                       # t9_write: payload piece_id
        "services/acp_content_writing/service.py::run_write_background",   # T9 write + T10 gates of one piece
    ),
    JOB: (
        "shared/jobs/worker.py::Worker._execute_job",                       # every job (call_log.bind_job)
    ),
}

# Modules whose ledger writes of a ruled stage take the key from the bound context (see the module docstring).
BOUND_MODULES: dict[str, tuple[str, ...]] = {
    TOUR: (
        "services/content_generation/graph.py",
        "services/content_generation/s1_writer_v2_live.py",
        "services/content_generation/judge_node.py",
        "services/content_generation/brand_audit_node.py",
        "services/content_generation/flag_fix_node.py",
        "services/content_generation/grounding.py",
        "services/content_generation/source_facts.py",
        "services/acp_contract/a3_atomize.py",
        "services/seo_intelligence/s1_prefetch.py",
    ),
    PIECE: (
        "services/acp_content_writing/generate.py",
        "services/acp_content_writing/quality_gates.py",
        "services/acp_content_writing/faq_filter.py",
    ),
    JOB: (
        "services/acp_contract/atom_ranking.py",
        "services/acp_contract/segment_matching.py",
    ),
}


def required_key(stage: Optional[str]) -> Optional[str]:
    """The key ``stage``'s rows must carry (``tour_id`` / ``content_piece_id`` / ``job_id``), None when no rule."""
    if not isinstance(stage, str) or not stage:
        return None
    for name, key in RULES:
        if (name.endswith("_") and stage.startswith(name)) or stage == name:
            return key
    return None
