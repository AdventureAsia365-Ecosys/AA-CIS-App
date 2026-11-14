"""AA-1105 — the learning API's SQL (services/learn/store.py PgStore) on the replayed schema learn (CIS 420): the
learner from the staff directory (name from the directory, inactive staff refused), enrollment, idempotent
statements, lesson progress, a module quiz failed then passed, review cards, the final quiz, the certificate, its
public verify by code hash and a code rotation. Drives the real services (academy / assessment) with the invented
bundle of tests/unit/aa1105_learn_fake.py. Runs in a transaction that is rolled back; one test runs as cis_app."""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core.identity_verifier import Principal  # noqa: E402
from api.routers import learn as learn_router  # noqa: E402
from services.learn import academy as ac  # noqa: E402
from services.learn import assessment as qa  # noqa: E402
from services.learn import bundle as lb  # noqa: E402
from services.learn import grading as g  # noqa: E402
from services.learn.store import PgStore  # noqa: E402
from tests.unit.aa1105_learn_fake import QUIZZES, install_bundle  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.example.invalid/pool-learn"
SUB = "1105cccc-0000-4000-8000-000000000001"
ADMIN = "1105dddd-0000-4000-8000-000000000001"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    await c.execute("INSERT INTO shared.admin_users (id, username, role, is_active, display_name, cognito_iss, "
                    "cognito_sub) VALUES ($1::uuid, 'sample-learner-1105', 'content', true, 'Sample Learner', $2, $3)",
                    ADMIN, ISS, SUB)
    install_bundle()
    try:
        yield c
    finally:
        lb.clear_cache()
        await tr.rollback()
        await c.close()


def _principal(sub: str = SUB) -> Principal:
    return Principal(iss=ISS, sub=sub, client_id="academy", groups=("cis-content",), email=None,
                     email_verified=None, username=None)


async def _ctx(conn) -> ac.Ctx:
    ctx = await learn_router.learner_ctx(PgStore(conn), _principal())
    ctx.bundle = await lb.load()
    return ctx


def _right(ids, wrong=()):
    by_id = {i["id"]: i for items in QUIZZES.values() for i in items}
    return [{"item_id": i, "chosen": [5] if i in wrong else sorted(by_id[i]["answer"])} for i in ids]


@pytest.mark.asyncio
async def test_learner_comes_from_the_directory(conn):
    ctx = await _ctx(conn)
    assert ac.me(ctx)["display_name"] == "Sample Learner" and ctx.learner["kind"] == "staff"
    await conn.execute("UPDATE shared.admin_users SET display_name = 'Renamed Learner' WHERE id = $1::uuid", ADMIN)
    again = await _ctx(conn)
    assert again.learner_id == ctx.learner_id and again.learner["display_name"] == "Renamed Learner"
    row = await conn.fetchrow("SELECT admin_user_id::text AS a, tenant_id, inactive_since FROM learn.learner "
                              "WHERE learner_id = $1::uuid", ctx.learner_id)
    assert row["a"] == ADMIN and row["tenant_id"] is None and row["inactive_since"] is None
    await conn.execute("UPDATE shared.admin_users SET is_active = false WHERE id = $1::uuid", ADMIN)
    with pytest.raises(ac.Problem) as refused:
        await _ctx(conn)
    assert refused.value.error == "not_a_learner"


@pytest.mark.asyncio
async def test_enroll_progress_and_statements(conn):
    ctx = await _ctx(conn)
    assert (await ac.enroll(ctx, "sample-path"))[1] is True
    assert (await ac.enroll(ctx, "sample-path"))[1] is False
    body = {"statement_id": str(uuid.uuid4()), "verb": "opened", "lesson_id": "Z9-01", "lang": "vi"}
    assert (await ac.record_progress(ctx, body))[1] is True
    assert (await ac.record_progress(ctx, body))[1] is False
    with pytest.raises(ac.Problem) as conflict:
        await ac.record_progress(ctx, {**body, "verb": "completed"})
    assert conflict.value.status == 409
    done, _ = await ac.record_progress(ctx, {**body, "statement_id": str(uuid.uuid4()), "verb": "completed"})
    assert done["status"] == "completed"
    later, _ = await ac.record_progress(ctx, {**body, "statement_id": str(uuid.uuid4()), "verb": "opened"})
    assert later["status"] == "completed"                                     # a completed lesson stays completed
    verbs = [r["verb"] for r in await conn.fetch(
        "SELECT verb FROM learn.statement WHERE learner_id = $1::uuid ORDER BY stored_at, verb", ctx.learner_id)]
    assert sorted(verbs) == ["completed", "enrolled", "opened", "opened"]
    paths = {p["id"]: p for p in (await ac.list_paths(ctx))["paths"]}
    assert paths["sample-path"]["enrolled"] is True


@pytest.mark.asyncio
async def test_quiz_review_final_and_certificate(conn):
    ctx = await _ctx(conn)
    for lid in ("Z9-01", "Z9-02"):
        await ac.record_progress(ctx, {"statement_id": str(uuid.uuid4()), "verb": "completed", "lesson_id": lid})
    first = await qa.start_module_quiz(ctx, "Z9")
    ids = [i["id"] for i in first["items"]]
    failed = await qa.submit(ctx, first["attempt_id"], _right(ids, wrong=ids[:1]))
    assert failed["passed"] is False and failed["retake_at"] is not None
    with pytest.raises(ac.Problem) as wait:
        await qa.start_module_quiz(ctx, "Z9")
    assert wait.value.error == "retake_wait"
    due = await qa.due_reviews(ctx)
    assert [c["item"]["id"] for c in due["cards"]] == ids[:1]
    now = g.now_utc().replace(microsecond=0)
    card = {**due["cards"][0]["card"], "due": g.iso(now.replace(year=now.year + 1)), "last_review": g.iso(now),
            "reps": 1, "state": 2}
    reviewed = await qa.review_result(ctx, ids[0], {"chosen": _right(ids[:1])[0]["chosen"], "reviewed_at": g.iso(now),
                                                    "card": card})
    assert reviewed["correct"] is True
    stored = await conn.fetchrow("SELECT reps, due_at, last_review_at FROM learn.review_card WHERE learner_id = "
                                 "$1::uuid AND item_id = $2", ctx.learner_id, ids[0])
    assert stored["reps"] == 1 and stored["last_review_at"] == now
    # the retake delay passed: move the failed attempt back a day
    await conn.execute("UPDATE learn.quiz_attempt SET submitted_at = submitted_at - interval '25 hours', "
                       "started_at = started_at - interval '25 hours' WHERE learner_id = $1::uuid", ctx.learner_id)
    second = await qa.start_module_quiz(ctx, "Z9")
    passed = await qa.submit(ctx, second["attempt_id"], _right([i["id"] for i in second["items"]]))
    assert passed["passed"] is True and passed["module_complete"] is True
    final = await qa.start_final_quiz(ctx, "sample-path")
    graded = await qa.submit(ctx, final["attempt_id"], _right([i["id"] for i in final["items"]]))
    code = graded["certificate"]["code"]
    row = await conn.fetchrow("SELECT code_sha256, status, final_attempt_id::text AS f, tenant_id FROM "
                              "learn.certificate WHERE learner_id = $1::uuid", ctx.learner_id)
    assert row["code_sha256"] == g.code_sha256(code) and row["f"] == final["attempt_id"] and row["tenant_id"] is None
    view = await qa.verify(PgStore(conn), code, ctx.bundle)
    assert view["holder_display_name"] == "Sample Learner" and view["status"] == "valid"
    certs = (await qa.list_certificates(ctx))["certificates"]
    rotated = await qa.rotate_code(ctx, certs[0]["certificate_id"])
    with pytest.raises(ac.Problem):
        await qa.verify(PgStore(conn), code, ctx.bundle)
    assert (await qa.verify(PgStore(conn), rotated["code"], ctx.bundle))["path_id"] == "sample-path"
    assert await conn.fetchval("SELECT status AS ok FROM learn.enrollment WHERE learner_id = $1::uuid "
                               "AND path_id = 'sample-path'", ctx.learner_id) == "completed"


@pytest.mark.asyncio
async def test_runs_as_cis_app(conn):
    """The API pool's role: learn DML through the cis_app policies, the directory read on shared.admin_users."""
    # CI replays without CIS 244's grants (lessons: "cis_app integration tests grant temporarily"); rolled back.
    if not await conn.fetchval("SELECT has_schema_privilege('cis_app', 'shared', 'USAGE') AS ok"):
        await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    if not await conn.fetchval("SELECT has_table_privilege('cis_app', 'shared.admin_users', 'SELECT') AS ok"):
        await conn.execute("GRANT SELECT ON shared.admin_users TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    ctx = await _ctx(conn)
    await ac.enroll(ctx, "solo-path")
    await ac.record_progress(ctx, {"statement_id": str(uuid.uuid4()), "verb": "completed", "lesson_id": "Z8-01"})
    attempt = await qa.start_module_quiz(ctx, "Z8")
    result = await qa.submit(ctx, attempt["attempt_id"], _right([i["id"] for i in attempt["items"]]))
    assert result["certificate"]["path_id"] == "solo-path"           # no final quiz: the last module certifies
    assert (await qa.list_certificates(ctx))["certificates"][0]["status"] == "valid"
