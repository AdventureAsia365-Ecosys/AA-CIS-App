"""AA-779 (S245) — the real-question chain's queries run on the real schema and count only the questions
the last landing pass claimed (atom_matches is upsert-only). Everything happens inside a transaction that
is rolled back. `acp_contract.atom_matches` comes from migration 161, which needs pgvector (CI's
postgres:15 has none): when it is missing, a stand-in with the same columns is created inside the
transaction (rolled back with it); when it exists, its FK to tour_atoms is dropped inside the transaction.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402
from services.acp_shared import real_questions as rq  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

_STANDIN = """
    CREATE TABLE acp_contract.atom_matches (
        id BIGSERIAL PRIMARY KEY, query TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind = 'question'),
        atom_id TEXT NOT NULL, distance DOUBLE PRECISION NOT NULL,
        matched_by TEXT NOT NULL CHECK (matched_by IN ('vector', 'tokens')),
        matched_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (query, atom_id))
"""


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    if await c.fetchval("SELECT to_regclass('acp_contract.atom_matches')") is None:
        await c.execute(_STANDIN)
    else:
        for name in await c.fetch("""SELECT conname FROM pg_constraint
                                     WHERE conrelid = 'acp_contract.atom_matches'::regclass AND contype = 'f'"""):
            await c.execute(f'ALTER TABLE acp_contract.atom_matches DROP CONSTRAINT "{name["conname"]}"')
    yield c
    await t.rollback()
    await c.close()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_chain_reads_segment_matches_and_place_paa_from_the_real_tables(tx):
    tour_id = uuid.uuid4()
    await tx.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)  # raw_tours is under RLS
    await tx.execute("""INSERT INTO silver_aa_internal.raw_tours
                        (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                        VALUES ($1, $2, $3, 'Khovsgol trek zz779', 'Mongolia', $4, 'active')""",
                     tour_id, uuid.UUID(TENANT_ID), uuid.UUID(BATCH_ID), uuid.uuid4())
    await tx.executemany("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place,
                            activity_type) VALUES ($1, $2, 'atom text', 3, $3, 'sightseeing')""",
                         [("zz779_a1", tour_id, "Khövsgöl Lake"), ("zz779_a2", tour_id, "Tsaatan camp")])
    await tx.execute("""INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action,
                        questions_count, questions_computed_at)
                        VALUES ('zz779_seg', 'Lake Khövsgölzz', 'visit', 2, now()),
                               ('zz779_old', 'Lake Khövsgölzz', 'visit', NULL, NULL)""")
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical) "
                     "VALUES ('zz779_old', 'zz779_seg')")
    await tx.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias) "
                     "VALUES ('zz779_seg', 'zz779_a1', false), ('zz779_seg', 'zz779_a2', false)")
    rows = [  # (query, atom, distance, age) — the newest 2 are what the last landing pass claimed
        ("How deep is Lake Khövsgöl?", "zz779_a1", 0.30, "1 minute"),
        ("Who are the Tsaatan people?", "zz779_a2", 0.20, "2 minutes"),
        ("An old question a later pass dropped?", "zz779_a1", 0.10, "30 days"),
    ]
    for q, a, d, age in rows:
        await tx.execute(f"""INSERT INTO acp_contract.atom_matches (query, kind, atom_id, distance, matched_by,
                             matched_at) VALUES ($1, 'question', $2, $3, 'vector', now() - interval '{age}')""",
                         q, a, d)
    await tx.execute("""INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask,
                        retrieved_on) VALUES ('lake khovsgolzz', 'US', 1900, $1::jsonb, now()),
                                             ('lake khovsgolzz', 'DE', 300, $2::jsonb, now())""",
                     json.dumps(["Is Lake Khövsgöl worth visiting?"]), json.dumps(["Wie tief ist der Khövsgöl See?"]))

    out = await rq.collect_real_questions(_Pool(tx), atom_ids=[], segment_ids=["zz779_old"],
                                          tour_paa=["Is Mongolia safe for tourists?"])
    assert [(r.question, r.source) for r in out] == [
        ("Who are the Tsaatan people?", "segment_match"),  # closer match first
        ("How deep is Lake Khövsgöl?", "segment_match"),
        ("Is Lake Khövsgöl worth visiting?", "place_paa"),  # the German one is dropped
        ("Is Mongolia safe for tourists?", "tour_paa"),
    ]

    # questions_count NULL (invalidated): rows newer than the last computation minus a day still count
    await tx.execute("UPDATE acp_contract.atom_segment SET questions_count = NULL WHERE segment_id = 'zz779_seg'")
    scope = await rq.resolve_scope(tx, ["zz779_a1"])
    assert scope.segment_ids == ("zz779_seg",) and scope.countries == ("Mongolia",)
    assert scope.places == ("Lake Khövsgölzz", "Khövsgöl Lake")
    got = await rq.SegmentMatchSource().fetch(tx, scope)
    assert sorted(got) == ["How deep is Lake Khövsgöl?", "Who are the Tsaatan people?"]
