"""AA-986 L2: the tour lifecycle service, the one CIS writer of a tour's served state (ADR 0015 decision 2).

See service.py for the actions and the cascade, derivatives.py for "which tenants depend on this tour", effects.py
for the post-commit work. tests/unit/test_aa986_l2_lifecycle_guard.py keeps every other module from writing the
status columns or inserting into raw_tours / published_tours.
"""
from .effects import recompute_after_status_change, run_after_commit
from .service import (
    LifecycleError,
    Outcome,
    PREVIEW_ACTIONS,
    activate_master,
    deleted_key_info,
    insert_new_sources,
    key_of,
    key_was_published,
    pause_master,
    preview,
    publish,
    restore_master,
    restore_source,
    retire_source,
    served_state,
    set_active,
    supersede,
    trash_master,
)

__all__ = [
    "LifecycleError", "Outcome", "PREVIEW_ACTIONS", "activate_master", "deleted_key_info", "insert_new_sources",
    "key_of", "key_was_published", "pause_master", "preview", "publish", "recompute_after_status_change",
    "restore_master", "restore_source", "retire_source", "run_after_commit", "served_state", "set_active",
    "supersede", "trash_master",
]
