"""S279 thresholds lane L2 (AA-1096) — CIS 412, the quality registry on the settings registry (and CIS 413, prompt
version t9_write v8), on real Postgres
(conftest replays every migration).

The registration, the 151 quality definitions equal to the compiled defaults (and the settings_keys index), every one
evidence-gated and maker-checker, and the CIS 407 value trigger on them: a proposal without an evidence reference is
refused, a value outside the definition (a pass line above 9.5, a detector mode that is not enforce / shadow / warn, a
negative Channel-bar entry) is refused, a fitting proposal with its evidence is accepted. Invented values only; every
test runs in a transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from scripts.settings import gen_compiled_defaults as gen  # noqa: E402
from services.quality import settings_keys as sk  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
PASS_LINE = "cis.quality.pass_line"
DETECTOR = sk.DETECTOR_MODES[("fact.number", "F1_grounding")]
BAR = sk.CHANNEL_BARS["blog"]
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', $3) RETURNING id")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _refused(c, *args):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with c.transaction():
            await c.fetchval(INS, *args)


@pytest.mark.asyncio
async def test_409_registered_and_seeded_like_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '412'") == "412"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value::text AS d, evidence_required, approval, area FROM shared.setting_definition "
        "WHERE key = ANY($1::text[])", list(sk.ALL_KEYS))}
    seed = {r["key"]: r for r in gen.seed_rows()[0]}
    assert set(rows) == set(sk.ALL_KEYS)
    for k, r in rows.items():
        assert json.loads(r["d"]) == seed[k]["default_value"], k
        assert r["evidence_required"] is True and r["approval"] == "maker_checker", k
    assert json.loads(rows[PASS_LINE]["d"]) == 7.0


@pytest.mark.asyncio
async def test_a_quality_proposal_needs_its_evidence_and_must_fit(conn):
    await _refused(conn, PASS_LINE, "7.5", None)                       # no evidence reference
    await _refused(conn, PASS_LINE, "9.8", "replay:zz-run-1")          # above the maximum 9.5
    await _refused(conn, DETECTOR, '"off"', "replay:zz-run-1")         # not enforce / shadow / warn
    await _refused(conn, BAR, '{"needs_demand": -1}', "replay:zz-run-1")
    assert await conn.fetchval(INS, PASS_LINE, "7.5", "golden:zz-run-2") is not None
    assert await conn.fetchval(INS, DETECTOR, '"shadow"', "replay:zz-run-3") is not None


@pytest.mark.asyncio
async def test_410_registers_t9_write_v8_with_the_manifest_hash(conn):
    """CIS 413: prompt version t9_write v8 (the blog length values rendered at call time) is registered with the hash
    of the manifest's newest t9_write entry."""
    manifest = json.loads(open(os.path.join(os.path.dirname(__file__), "..", "..", "shared", "llm_client",
                                            "prompt_versions.json"), encoding="utf-8").read())
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '413'") == "413"
    row = await conn.fetchrow("SELECT prompt_sha256 AS sha, source_path AS path FROM shared.prompt_version "
                              "WHERE stage = 't9_write' AND version = 'v8'")
    assert row is not None and row["path"] == "services/acp_content_writing/prompts.py"
    v8 = [e for e in manifest["prompts"] if e["stage"] == "t9_write" and e["version"] == "v8"]
    if v8:   # the manifest entry arrives with the code PR; on the migration PR alone the row is checked by shape
        assert row["sha"] == v8[0]["prompt_sha256"]
    assert len(row["sha"]) == 64
