"""AA-900 — an R2 run started for a prompt / model / route change writes every model call under `adhoc_aa900`.

The label is the only difference: the default stays `writing_memory_eval` (Releases tab evidence runs), an unknown label
is refused before anything is spent, the per-run cap reads the run's own label and the month cap counts both.
Reuses the in-memory store, fake writer and fake judge of the AA-919 runner tests (no DB, no model).
"""
from __future__ import annotations

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import r2
from shared.llm_client import call_log
from tests.unit.test_aa919_r2_runner import FakeStore, golden, run


def test_default_label_is_unchanged():
    assert r2.R2Config(writer="a1").log_stage == cfg.EVAL_STAGE == "writing_memory_eval"


def test_change_eval_run_labels_every_call_adhoc_aa900():
    store = FakeStore(golden(labels_fail=0))
    out, writer, judge = run(store, r2.R2Config(writer="a1", log_stage="adhoc_aa900"))
    assert (out.verdict, out.stop_reason) == ("pass", "completed")
    assert set(writer.labels) == {("adhoc_aa900", out.run_id)}
    assert {lab for *_, lab in judge.calls} == {("adhoc_aa900", out.run_id)}
    assert store.runs[out.run_id]["details"]["log_stage"] == "adhoc_aa900"
    assert call_log.eval_label() is None


def test_unknown_label_is_refused_before_any_spend():
    store = FakeStore(golden())
    with pytest.raises(r2.R2Refused, match="log_stage"):
        run(store, r2.R2Config(writer="a1", log_stage="adhoc_other"))
    assert store.runs == {} and store.results == []


def test_cli_flag_and_job_payload_carry_the_label():
    conf, _ = r2._parse(["--writer", "t9_blog", "--stage", "adhoc_aa900", "--cap-usd", "2"])
    assert conf.log_stage == "adhoc_aa900" and conf.cap_usd == 2.0
    assert r2.config_from_payload(r2.payload_of(conf)) == conf
    with pytest.raises(SystemExit):
        r2._parse(["--writer", "a1", "--stage", "writing_memory_mine"])


def test_month_cap_and_run_spend_read_the_change_label():
    assert cfg.CHANGE_EVAL_STAGE in cfg.MEMORY_STAGES
    assert set(cfg.EVAL_LOG_STAGES) == {"writing_memory_eval", "adhoc_aa900"}
    assert r2.PgStore(None, stages=("adhoc_aa900",)).stages == ("adhoc_aa900",)
