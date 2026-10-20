"""AA-915: the AA-132 lessons loop (shared.pipeline_lessons) is frozen behind PIPELINE_LESSONS_ENABLED.

Default OFF: no reader sends SQL to the table, the S1 system prompt carries no "LESSONS FROM PREVIOUS
BATCHES" block (sync path, batch path), and the flag_fix write-back is a no-op. ON restores the legacy
block byte for byte (rollback only). A guard keeps every SQL touch of the table in two files.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.content_generation import flag_fix_node as ff
from services.content_generation import pipeline_lessons as pl

HEADER = "LESSONS FROM PREVIOUS BATCHES"
FAKE_UUID = "11111111-1111-1111-1111-111111111111"
MASTER_UUID = "00000000-0000-0000-0000-000000000001"
LESSON_ROWS = [
    {"what_to_do": "Fix field summary — failure code SUMMARY_OFF_BRAND", "field": "summary"},
    {"what_to_do": "Never open with Discover", "field": ""},
]
# The literal both readers built before AA-915 (admin_pipeline._execute_run_tour, s1_batch).
LEGACY_BLOCK = (
    "\n\n## LESSONS FROM PREVIOUS BATCHES\n"
    "Apply these rules strictly — they come from real batch failures:\n"
    "- [summary] Fix field summary — failure code SUMMARY_OFF_BRAND\n"
    "- Never open with Discover"
)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("AUTO_UPGRADE_THRESHOLD", "8.5")
    monkeypatch.delenv(pl.PIPELINE_LESSONS_ENV, raising=False)


def _flag(monkeypatch, value):
    monkeypatch.setenv(pl.PIPELINE_LESSONS_ENV, value)


# ── flag parsing ──────────────────────────────────────────────────────────────

def test_flag_default_off():
    assert pl.pipeline_lessons_enabled() is False


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "ture", "enabled"])
def test_flag_off_values(monkeypatch, value):
    _flag(monkeypatch, value)
    assert pl.pipeline_lessons_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " on ", "yes"])
def test_flag_on_values(monkeypatch, value):
    _flag(monkeypatch, value)
    assert pl.pipeline_lessons_enabled() is True


def test_flag_read_on_every_call(monkeypatch):
    assert pl.pipeline_lessons_enabled() is False
    _flag(monkeypatch, "true")
    assert pl.pipeline_lessons_enabled() is True


# ── loader ────────────────────────────────────────────────────────────────────

def test_loader_off_sends_no_sql():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=AssertionError("no SQL may reach pipeline_lessons while frozen"))
    assert asyncio.run(pl.load_s1_lessons_block(conn)) == ""
    conn.fetch.assert_not_called()


def test_loader_on_is_byte_identical_to_legacy(monkeypatch):
    _flag(monkeypatch, "true")
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=LESSON_ROWS)
    assert asyncio.run(pl.load_s1_lessons_block(conn)) == LEGACY_BLOCK
    assert conn.fetch.call_args.args[0] == pl.LESSONS_SQL


def test_format_empty_rows():
    assert pl.format_lessons_block([]) == ""


def test_loader_on_error_is_non_fatal(monkeypatch):
    _flag(monkeypatch, "true")
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    assert asyncio.run(pl.load_s1_lessons_block(conn)) == ""


# ── shared fakes for the two S1 prompt paths ──────────────────────────────────

def _raw_row():
    return {
        "src_name": "Test Tour", "src_subtitle": "", "src_summary": "", "src_description": "",
        "src_highlights": "[]", "src_itineraries": "", "country": "Vietnam", "duration": "7 days",
        "price_raw": "1000", "inclusions": "", "exclusions": "", "source_status": None, "activities": None,
    }


def _brand_row():
    return {
        "id": "22222222-2222-2222-2222-222222222222", "brand_name": "Adventure Asia", "version": 1,
        "system_prompt": "BRAND SYSTEM PROMPT", "style_guide": "", "forbidden_words": [],
        "voice_examples": [], "core_idea": "", "customer_segment": "", "customer_mindset": "",
        "good_examples": "", "brand_type": "",
    }


def _conn():
    """A fake connection whose fetch returns lesson rows only for the pipeline_lessons query, and
    records every SQL string it was sent."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _raw_row()
    conn.sent_sql = []

    async def _fetch(sql, *args, **kwargs):
        conn.sent_sql.append(sql)
        return list(LESSON_ROWS) if "pipeline_lessons" in sql else []

    conn.fetch = AsyncMock(side_effect=_fetch)
    return conn


def _lessons_sql_sent(conn) -> bool:
    return any("pipeline_lessons" in s for s in conn.sent_sql)


# ── sync path: admin_pipeline._execute_run_tour (Done-when) ───────────────────

def _run_sync_path():
    from api.routers import admin_pipeline

    conn = _conn()
    rt = AsyncMock(return_value={
        "status": "success", "quality_score": 9.0, "model_used": "haiku-4.5",
        "generated": {}, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
    })
    req = admin_pipeline.TourRunRequest(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal",
                                        model_tier="haiku")
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)))
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule",
                                  AsyncMock(return_value=_brand_row())))
        stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", rt))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                                  MagicMock(return_value="Vietnam tours")))
        asyncio.run(admin_pipeline._execute_run_tour(req))
    assert rt.call_args_list, "_rewrite_tour was not reached"
    brand_rules = rt.call_args_list[0].kwargs["brand_rules"]
    return brand_rules, conn


def test_sync_path_flag_off_no_lessons_block_and_no_read():
    brand_rules, conn = _run_sync_path()
    assert HEADER not in brand_rules.get("system_prompt", "")
    assert brand_rules.get("system_prompt") == "BRAND SYSTEM PROMPT"
    assert not _lessons_sql_sent(conn)


def test_sync_path_flag_on_restores_block(monkeypatch):
    _flag(monkeypatch, "true")
    brand_rules, conn = _run_sync_path()
    assert brand_rules["system_prompt"] == "BRAND SYSTEM PROMPT" + LEGACY_BLOCK
    assert _lessons_sql_sent(conn)


# ── batch path: s1_batch._load_prompt_state + materialize_s1_prompt (Done-when) ──

def _batch_prompt():
    from services.content_generation import s1_batch
    from services.content_generation.batch_prompt import materialize_s1_prompt

    conn = _conn()
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule",
                                  AsyncMock(return_value=_brand_row())))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        state = asyncio.run(s1_batch._load_prompt_state(conn, FAKE_UUID, MASTER_UUID, seo_mode="minimal"))
    system, _user, version = materialize_s1_prompt(state)
    return state, system, version, conn


def test_batch_path_flag_off_no_lessons_block_and_same_version():
    from services.content_generation.batch_prompt import materialize_s1_prompt

    state, system, version, conn = _batch_prompt()
    assert HEADER not in system
    assert state["brand_system_prompt"] == "BRAND SYSTEM PROMPT"
    assert not _lessons_sql_sent(conn)
    # The version equals the one of the same state built with an empty lessons block.
    empty_block_state = {**state, "brand_system_prompt": "BRAND SYSTEM PROMPT" + pl.format_lessons_block([])}
    assert version == materialize_s1_prompt(empty_block_state)[2]


def test_batch_path_flag_on_changes_prompt_and_version(monkeypatch):
    _, _, version_off, _ = _batch_prompt()
    _flag(monkeypatch, "true")
    state, system, version_on, conn = _batch_prompt()
    assert HEADER in system
    assert state["brand_system_prompt"] == "BRAND SYSTEM PROMPT" + LEGACY_BLOCK
    assert version_on != version_off
    assert _lessons_sql_sent(conn)


# ── write path: flag_fix_node ─────────────────────────────────────────────────

LESSONS = [
    {"failure_code": "META_OPENER_ROBOTIC", "field": "seo_meta", "pattern": "opens with Discover",
     "example_before": "Discover Bhutan.", "severity": "high"},
    {"failure_code": "META_OPENER_ROBOTIC", "field": "seo_meta", "pattern": "opens with Discover",
     "example_before": "Discover Bhutan again.", "severity": "high"},
]


def test_write_safe_flag_off_never_calls_writer():
    writer = MagicMock(side_effect=AssertionError("write_lessons_log must not run while frozen"))
    with patch.object(ff, "write_lessons_log", writer):
        ff._write_lessons_safe(LESSONS, {"tour": {"batch_name": "b", "country": "Vietnam"}})
    writer.assert_not_called()


def test_write_log_flag_off_opens_no_connection():
    connect = AsyncMock(side_effect=AssertionError("no connection while frozen"))
    with patch.object(ff.asyncpg, "connect", connect):
        assert asyncio.run(ff.write_lessons_log(LESSONS, min_frequency=2)) == 0
    connect.assert_not_called()


def test_write_log_flag_on_still_writes(monkeypatch):
    """Rollback path stays intact: with the flag on the legacy writer inserts as before."""
    _flag(monkeypatch, "true")
    conn = AsyncMock()
    conn.fetchval.return_value = None
    with patch.object(ff.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch.object(ff, "get_database_url", return_value="postgresql://test/test"):
        assert asyncio.run(ff.write_lessons_log(LESSONS, min_frequency=2)) == 1
    assert conn.execute.call_count == 1


# ── guard: one reader module, one writer module ───────────────────────────────

_SQL_TOUCH = re.compile(r"(?i)\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+(shared\.)?pipeline_lessons\b")
_ALLOWED = {
    "services/content_generation/pipeline_lessons.py",
    "services/content_generation/flag_fix_node.py",
}


def test_only_two_files_touch_pipeline_lessons_in_sql():
    offenders = []
    for top in ("api", "services", "shared", "scripts"):
        for path in (REPO / top).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith("api/migrations/") or rel in _ALLOWED:
                continue
            if _SQL_TOUCH.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(rel)
    assert not offenders, (
        "shared.pipeline_lessons is frozen (AA-915): read it only through "
        f"services/content_generation/pipeline_lessons.py. Offenders: {offenders}"
    )


def test_guard_regex_matches_the_real_readers():
    """The guard can fail: the reader module itself matches it."""
    assert _SQL_TOUCH.search((REPO / "services/content_generation/pipeline_lessons.py").read_text())
