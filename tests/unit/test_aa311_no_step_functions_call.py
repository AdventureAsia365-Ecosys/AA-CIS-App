"""
Unit test for AA-311: process_file() must no longer call Step Functions StartExecution.

Regression context: process_file() inserted tours successfully, then called the now-retired
_start_pipeline() to kick off "aa-cis-dev-pipeline". The ECS task role lacks
states:StartExecution, so that call raised AccessDeniedException and process_file() re-raised
it, turning a successful INSERT into a 500 response (see AA-182 for the SM-bypass decision).
"""
import os
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.ingestion import handler


@pytest.fixture(autouse=True)
def _no_jev_ingest_gates():
    """AA-690: ingest now asks Jev; these tests predate it and exercise other rules."""
    from services.ingestion import jev_gates

    async def none(rows, *a, **k):
        return [jev_gates.RowVerdict() for _ in rows]
    with patch.object(jev_gates, "assess_rows", new=none), patch.object(jev_gates, "assess_rows_standalone", new=none):
        yield


def _make_conn(fetchrow_return=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value="staging-id-1")
    conn.execute = AsyncMock(return_value=None)
    conn.close = AsyncMock(return_value=None)
    return conn


@pytest.mark.asyncio
async def test_process_file_does_not_start_step_functions_execution(tmp_path):
    # Env configured with a real-looking ARN — previously this alone was enough to
    # trigger _start_pipeline() → StartExecution → AccessDeniedException.
    fake_sfn_arn = "arn:aws:states:us-west-1:123:stateMachine:aa-cis-dev-pipeline"
    with patch.dict(os.environ, {"STEP_FUNCTIONS_ARN": fake_sfn_arn}):
        conn_check = _make_conn(fetchrow_return=None)  # no dedup match
        conn_main = _make_conn(fetchrow_return=None)    # no existing tour match

        mock_s3 = MagicMock()
        mock_s3.download_fileobj = MagicMock(return_value=None)
        mock_s3.head_object = MagicMock(return_value={"ContentLength": 1024})

        mock_sfn = MagicMock()
        mock_sfn.start_execution = MagicMock()

        mock_parser_instance = MagicMock()
        mock_parser_instance.parse.return_value = [
            {"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam"},
        ]

        with patch.object(handler, "_s3", return_value=mock_s3), \
             patch.object(handler, "_sfn", return_value=mock_sfn), \
             patch.object(handler, "get_database_url", return_value="postgresql://fake"), \
             patch.object(handler.asyncpg, "connect", AsyncMock(side_effect=[conn_check, conn_main])), \
             patch.object(handler, "ExcelParser", return_value=mock_parser_instance), \
             patch.object(handler, "RawSourceRepository") as mock_source_repo_cls, \
             patch.object(handler, "RawTourRepository") as mock_tour_repo_cls, \
             patch.object(handler, "_start_pipeline") as mock_start_pipeline:

            mock_source_repo = MagicMock()
            mock_source_repo.insert = AsyncMock(return_value="source-id-1")
            mock_source_repo.update_status = AsyncMock(return_value=None)
            mock_source_repo_cls.return_value = mock_source_repo

            mock_tour_repo = MagicMock()
            mock_tour_repo.insert_batch = AsyncMock(return_value=(["tour-id-1"], []))
            mock_tour_repo_cls.return_value = mock_tour_repo

            result = await handler.process_file("aa-cis-bronze", "raw-inbox/Horizon/file.xlsx")

        # Success response reflects the INSERT, independent of any pipeline trigger.
        assert result["status"] == "done"
        assert result["tours_written"] == 1
        assert "sfn_triggered" not in result

        # The retired call path must never fire — neither the wrapper nor the raw SDK call.
        mock_start_pipeline.assert_not_called()
        mock_sfn.start_execution.assert_not_called()
