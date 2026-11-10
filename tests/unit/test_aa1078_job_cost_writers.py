"""AA-1078 (S277) — guard: every write of shared.job.cost_usd in shared/jobs/queue.py keeps the cost stamp invariant
(cost_usd - progress.cost_stamp.llm_usd = the handler's own spend). A raw ``cost_usd = coalesce($n, ...)`` write of a
handler value would leave a stale stamp behind it, and the Jobs page would subtract an LLM part the column no longer
holds. Use ``_stamp_sql`` (or set_progress's keep-the-stamp form) instead."""
import re
from pathlib import Path

from shared.jobs import queue

SRC = Path(queue.__file__).read_text(encoding="utf-8")


def test_no_raw_handler_cost_write():
    raw = re.findall(r"cost_usd\s*=\s*coalesce\(\s*\$\d+\s*,", SRC)
    assert raw == [], "write cost_usd through queue._stamp_sql (AA-1078 cost stamp), not coalesce($n, cost_usd)"


def test_stamp_sql_keeps_the_row_when_the_handler_cost_is_null():
    sql = queue._stamp_sql("$1::uuid", "$6")
    assert "WHEN ($6)::numeric IS NULL THEN cost_usd" in sql and "'cost_stamp'" in sql
    assert "shared.llm_call_log l WHERE l.job_id = $1::uuid" in sql


def test_handler_cost_subtracts_the_stamp():
    job = queue.Job(id="j", kind="k", payload={}, status="running", attempt=1, max_attempts=1,
                    progress={"cost_stamp": {"llm_usd": 0.04}}, cost_usd=0.05)
    assert queue.handler_cost(job) == 0.01
    assert queue.stamped_llm_usd({"cost_stamp": "bad"}) == 0.0 and queue.stamped_llm_usd(None) == 0.0
