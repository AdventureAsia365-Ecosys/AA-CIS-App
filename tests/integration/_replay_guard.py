"""Integration replay guard (AA-1024 l22, lesson G16): a migration that fails during the test schema replay is an error.

tests/integration/conftest.py replays every api/migrations/*.sql file on an empty database. A file that fails there used
to print "Warning <file>: <error>" and the session went on, so a broken migration (203 and about 50 GRANTs, S231) never
reached the test DB and the tests that should have read it still went green. Now conftest records each failure and
calls ``replay_problems``; any problem ends the session (pytest.exit) with the list.

EXPECTED_REPLAY_FAILURES is the set that fails on CI's fresh postgres:15 (superuser cistest, no pgvector) today: the
ACPv1 lineage (schemas never created here), migration 001's tenant_tour_versions (skipped), pgvector (no extension in
the CI image) and 205 (CREATE INDEX CONCURRENTLY inside BEGIN). Each entry keeps the error CI prints. A new failure
fails the session; so does an expected file that fails with another error (the entry must be a substring of the first
line of the error) and an entry that now applies (stale: delete it), except where pgvector is installed locally,
where the vector entries and the files that follow from them may apply or fail differently.
"""

from __future__ import annotations

# file name -> the error the replay raises on CI (postgres:15, no pgvector). Shrink only.
EXPECTED_REPLAY_FAILURES: dict[str, str] = {
    "019_social_content.sql": 'schema "acp_silver_s4" does not exist',
    "020_acp_output_rules_seed.sql": 'relation "acp_shared.acp_output_rules" does not exist',
    "021_hitl_reviewer_type.sql": 'relation "acp_shared.audit_log" does not exist',
    "029_s2_router_tables.sql": 'relation "acp_silver_s2.visibility_reports" does not exist',
    "031_acp_silver_s3_v2.sql": 'schema "acp_silver_s3" does not exist',
    "032_harness_columns.sql": 'schema "acp_silver_s4" does not exist',
    "033_blog_engine_columns.sql": 'schema "acp_silver_s4" does not exist',
    "034_social_content_channels.sql": 'schema "acp_silver_s4" does not exist',
    "035_acp_run_context_s2_columns.sql": 'relation "acp_shared.acp_run_context" does not exist',
    "036_hitl_gate1_columns.sql": 'relation "acp_shared.acp_hitl_requests" does not exist',
    "037_acp_output_rules_confidence.sql": 'relation "acp_shared.acp_output_rules" does not exist',
    "038_blog_drafts_cms_publish_status.sql": 'schema "acp_silver_s4" does not exist',
    "040_blog_drafts_aa62.sql": 'schema "acp_silver_s4" does not exist',
    "041_pgvector_embeddings.sql": 'extension "vector" is not available',
    "051_acp_run_context_updated_at_index.sql": 'relation "acp_shared.acp_run_context" does not exist',
    "060_acp_run_context_s3_keys.sql": 'relation "acp_shared.acp_run_context" does not exist',
    "062_gate3_context.sql": 'schema "acp_silver_s4" does not exist',
    "063_blog_drafts_escalated_msthy.sql": 'schema "acp_silver_s4" does not exist',
    "066_social_quality_score.sql": 'schema "acp_silver_s4" does not exist',
    "067_social_angles_json.sql": 'schema "acp_silver_s4" does not exist',
    "068_s1_tour_ids_run_context.sql": 'relation "acp_shared.acp_run_context" does not exist',
    "078_acp_contract_v_trip_registry.sql": 'column pt.content_embedding does not exist',
    "080_v_trip_registry_add_inclusions_exclusions.sql": 'relation "acp_deliver.tenant_tour_pages" does not exist',
    "083_v_trip_registry_raw_tour_floor.sql": 'relation "acp_deliver.tenant_tour_pages" does not exist',
    "087_v_trip_registry_add_tenant_id.sql": 'relation "acp_deliver.tenant_tour_pages" does not exist',
    "090_v_trip_registry_filter_deleted_at.sql": 'relation "acp_deliver.tenant_tour_pages" does not exist',
    "107_aa425_tenant_qa_gate.sql": 'relation "gold_aa_internal.tenant_tour_versions" does not exist',
    "108_tenant_tour_versions_ai_generated_status.sql": 'relation "gold_aa_internal.tenant_tour_versions" does not exist',
    "109_tenant_tour_versions_qa_auto_passed.sql": 'relation "gold_aa_internal.tenant_tour_versions" does not exist',
    "121_drop_acpv1_stage_chain.sql": 'relation "acp_shared.acp_output_rules" does not exist',
    "123_raw_sources_file_hash_unique_index.sql": 'column "file_hash" does not exist',
    "124_content_piece_reuse_columns.sql": 'type "vector" does not exist',
    "140_content_piece_embedding_index.sql": 'access method "ivfflat" does not exist',
    "161_atom_embedding_matches.sql": 'type "vector" does not exist',
    "162_question_embedding_cache.sql": 'type "vector" does not exist',
    "175_tenant_version_job.sql": 'relation "gold_aa_internal.tenant_tour_versions" does not exist',
    "195_v_trip_registry_active_sources.sql": 'relation "acp_deliver.tenant_tour_pages" does not exist',
    "205_decision_log_covering_indexes.sql": 'CREATE INDEX CONCURRENTLY cannot run inside a transaction block',
    "223_tenant_usage_real_llm_cost.sql": 'relation "gold_aa_internal.tenant_tour_versions" does not exist',
    "297_tenant_tour_versions_generation_meta.sql": 'migration 297: gold_aa_internal.tenant_tour_versions is missing (CIS 001)',
    "300_tenant_tour_versions_judge_breakdown.sql": 'migration 300: gold_aa_internal.tenant_tour_versions is missing (CIS 001)',
}

SUPERUSER_HINT = (
    "the integration test DB user must be a SUPERUSER, as in CI (postgres:15 service user). Start a throwaway cluster: "
    "/usr/lib/postgresql/16/bin/initdb -D <scratch>/pg -U postgres -A trust, start it on its own port (never 5432), "
    "CREATE USER cistest SUPERUSER PASSWORD 'cistest'; CREATE DATABASE cis_integration_test OWNER cistest; then run "
    "with CIS_TEST_DB_PORT=<port> (or: scripts/pre-push.sh --integration, which does all of that)"
)


def first_line(err: BaseException) -> str:
    """The first line of an error (psycopg2 appends LINE / HINT lines), or its type when it has no text."""
    text = str(err).strip()
    return text.splitlines()[0] if text else type(err).__name__


def replay_problems(failed: dict[str, str], expected: dict[str, str] | None = None,
                    vector_available: bool = False) -> list[str]:
    """Problems of one replay: ``failed`` = {file: error} of the files that did not apply.

    A file outside ``expected`` that failed is a problem. An expected file counts as expected only when its recorded
    error is a substring of the first line of the error it raised now: the same file failing for another reason (a
    typo in a later edit, a missing grant) is a problem too. An expected file that applied is a problem (a stale entry).
    With ``vector_available`` (a local Postgres with pgvector) neither of the last two is checked: the vector files
    and some that follow apply there, or fail further on with another error."""
    expected = EXPECTED_REPLAY_FAILURES if expected is None else expected
    out = [f"{name} failed during the test replay: {failed[name]}" for name in sorted(set(failed) - set(expected))]
    if not vector_available:
        for name in sorted(set(failed) & set(expected)):
            got = (failed[name].strip().splitlines() or [""])[0]
            if expected[name] not in got:
                out.append(f"{name} failed during the test replay with another error than the expected one: got "
                           f"{got!r}, expected {expected[name]!r} (tests/integration/_replay_guard.py)")
        out += [f"{name} is in EXPECTED_REPLAY_FAILURES (tests/integration/_replay_guard.py) but now applies: delete "
                "the entry" for name in sorted(set(expected) - set(failed))]
    return out


def exit_message(problems: list[str]) -> str:
    return ("integration replay guard: the test schema replay differs from the expected set "
            "(tests/integration/_replay_guard.py):\n  " + "\n  ".join(problems))
