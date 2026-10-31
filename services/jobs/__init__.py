"""AA-650 — job kinds. Importing this package registers every kind with shared.jobs.registry."""
from . import segment_research_job  # noqa: F401
from . import t2_rewrite_job  # noqa: F401
from . import t9_write_job  # noqa: F401
from . import a3_atomize_job  # noqa: F401
from . import s1_seo_prefetch_job  # noqa: F401
from . import photo_sync_job  # noqa: F401
from . import photo_medium_backfill_job  # noqa: F401  AA-995 (960 w rendition of the stored photos)
from . import photo_destination_match_job  # noqa: F401  AA-671 S257 (photo -> destination of its tour)
from . import s1_rewrite_job  # noqa: F401  AA-723
from . import revalidate_job  # noqa: F401  AA-723
from . import s1_batch_ingest_job  # noqa: F401  AA-723
from . import recompute_job  # noqa: F401  AA-723 PR2
from . import t8_angles_job  # noqa: F401  AA-764
from . import t7_slate_job  # noqa: F401  AA-764
from . import place_cards_job  # noqa: F401  AA-772
from . import place_extract_job  # noqa: F401  AA-712
from . import place_geocode_job  # noqa: F401  AA-712
from . import place_shape_job  # noqa: F401  AA-847
from . import tour_graph_job  # noqa: F401  AA-712
from . import place_dedupe_job  # noqa: F401  AA-847 (duplicate-place merge)
from . import place_relocate_job  # noqa: F401  AA-997 (re-locate model-placed destinations)
from . import retention_job  # noqa: F401  AA-825
from . import ops_alert_email_job  # noqa: F401  AA-828
from . import staff_reconcile_job  # noqa: F401  AA-816 (C7)
from . import staff_invite_email_job  # noqa: F401  AA-816 (S242 invitation e-mail)
from . import erasure_job  # noqa: F401  AA-839 (data erasure DRY RUN / approved apply)
from . import tripplanner_purge_job  # noqa: F401  AA-987 (TripPlanner cache purge on a lifecycle event)
from . import supply_content_draft_job  # noqa: F401  AA-835 (LLM draft of Booking supply content)
from . import supply_media_purge_job  # noqa: F401  AA-835 (CIS copies of a withdrawn Booking photo)
from . import writing_memory_job  # noqa: F401  AA-917 (writing-memory backfill, fact filter, mining)
from . import report_jobs  # noqa: F401  AA-1003 (kpi_snapshot_daily, report_generate)
from . import report_ops_jobs  # noqa: F401  AA-1003 R2 (inquiry_handoff_check, report_email)
from . import llm_cost_reconcile_job  # noqa: F401  AA-1002 S10 (LLM cost ledger vs the provider, nightly)
from . import llm_probe_job  # noqa: F401  AA-1018 (daily LLM connection probe; off unless LLM_DAILY_PROBE=on)
from . import noop_sleep_job  # noqa: F401  AA-993 G1 (test-only kind, registered only when JOB_TEST_KINDS=1)
