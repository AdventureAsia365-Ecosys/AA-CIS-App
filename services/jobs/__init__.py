"""AA-650 — job kinds. Importing this package registers every kind with shared.jobs.registry."""
from . import segment_research_job  # noqa: F401
from . import t2_rewrite_job  # noqa: F401
from . import t9_write_job  # noqa: F401
from . import a3_atomize_job  # noqa: F401
from . import s1_seo_prefetch_job  # noqa: F401
from . import photo_sync_job  # noqa: F401
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
from . import retention_job  # noqa: F401  AA-825
from . import ops_alert_email_job  # noqa: F401  AA-828
from . import staff_reconcile_job  # noqa: F401  AA-816 (C7)
from . import staff_invite_email_job  # noqa: F401  AA-816 (S242 invitation e-mail)
