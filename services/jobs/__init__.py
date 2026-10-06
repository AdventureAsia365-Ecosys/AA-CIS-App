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
