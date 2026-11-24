"""AA-1148 (S290 live fix): the shared.pipeline_runs row a source tour that does not come from an upload needs.

Every raw_tours row names its batch, and raw_tours.batch_id is a foreign key to shared.pipeline_runs(batch_id). The
upload path writes that row first (services/ingestion/handler.process_file, status 'ingesting'); a source tour made
some other way (the content workspace's hand-made tour) starts a new batch of its own, so it writes the row here, in
the caller's transaction, before supersede(decision='create_new') inserts the tour.

The row is born 'completed': nothing is ingested, so there is no batch to wait on. sync_batch_completion
(services/export/handler.py) only advances an 'ingesting' row, and still recounts tours_passed when the tour publishes;
the pipeline metrics skip 'ingesting' rows, and the data-coverage ingest counts only read raw-inbox/ uploads
(s3_source_path stays NULL here).
"""
from __future__ import annotations

import uuid

_OPEN_BATCH_SQL = """
    INSERT INTO shared.pipeline_runs
        (batch_id, tenant_id, batch_name, s3_source_path, status, tours_total, step_name, completed_at)
    VALUES ($1::uuid, $2::uuid, $3, NULL, 'completed', $4, $5, NOW())
"""


async def open_batch(conn, *, tenant_id: str, batch_name: str, step_name: str, tours_total: int = 1) -> str:
    """Write the pipeline_runs row of a new batch that has no upload; returns its batch_id (for supersede)."""
    batch_id = str(uuid.uuid4())
    await conn.execute(_OPEN_BATCH_SQL, batch_id, tenant_id, batch_name, tours_total, step_name)
    return batch_id
