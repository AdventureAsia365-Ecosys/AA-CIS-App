"""AA-1146 (S285) — tour versions on real Postgres: a person's draft version and its diff, the publish gate (re-check,
two-person rule), the hand-edit lock, the source tour's history with old values, the price permission and the
field-by-field merge of an Excel re-import.

Needs CIS 457 (and 458 for the two-person switch): the module is skipped on a database without them, so the code PR's
own CI (before the migration PR is merged) skips it and the merge queue (main + this PR) runs it. Each test runs in
one transaction that is rolled back; process_export (its own connection) is replaced by a stand-in. Invented names and
places only."""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_pipeline as ap  # noqa: E402
from services.content_generation import human_lock  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
EDITOR = "00000000-0000-4000-8000-0000000000e1"
LEAD = "00000000-0000-4000-8000-0000000000e2"
SWITCH = "cis.content.publish_two_person"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _request(conn, *, role="content", user=EDITOR, body=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))), scope={},
                           headers={"x-admin-role": role, "x-admin-user-id": user},
                           json=AsyncMock(return_value=body or {}))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    ready = await c.fetchval("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = "
                             "'silver_aa_internal' AND table_name = 'generated_content' AND column_name = 'origin'")
    if not ready:
        await c.close()
        pytest.skip("CIS 457 is not applied on this database (the migration PR ships first)")
    tr = c.transaction()
    await tr.start()
    try:
        with patch.object(ap, "verify_admin_secret", MagicMock()):
            yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(conn):
    tour_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "price_raw, inclusions, src_itineraries) VALUES ($1, $2, $3, 'Ombre Lake Walk 3D', 'Thailand', '3 days', "
        "'From 300 USD', 'Water', 'Day 1: Arrive in Ombre. Day 2: Boat across the lake. Day 3: Home.')",
        uuid.UUID(tour_id), MASTER, BATCH_ID)
    v1 = str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_itineraries, aa_highlights, status) VALUES ($1, $2, 1, 'Ombre Lake Walk', 'Three quiet days', "
        "'Day 2: a boat across Lake Ombre.', '[\"Lake Ombre\"]'::jsonb, 'approved') RETURNING id",
        uuid.UUID(tour_id), MASTER))
    await conn.execute(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, "
        "aa_itineraries, master_status) VALUES ($1, $2, $3, 'Ombre Lake Walk', 'Three quiet days', "
        "'Day 2: a boat across Lake Ombre.', 'active')", uuid.UUID(tour_id), uuid.UUID(v1), MASTER)
    return tour_id, v1


async def _switch(conn, on: bool):
    if not await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition WHERE key = $1", SWITCH):
        pytest.skip("CIS 458 is not applied on this database")
    vid = await conn.fetchval(
        "INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) "
        "VALUES ($1, $2::jsonb, (SELECT COALESCE(MAX(version), 0) + 1 FROM shared.setting_value WHERE key = $1), "
        "'admin:zz-a', 'zz test: a reason of at least twenty characters') RETURNING id", SWITCH, json.dumps(on))
    await conn.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                       "decided_at = now() WHERE id = $1", vid)


@pytest.mark.asyncio
async def test_a_wording_edit_creates_a_draft_version_with_a_diff_and_the_live_text_stays(conn):
    tour_id, v1 = await _tour(conn)
    body = ap.drafts.DraftSave(fields={"aa_itineraries": "Day 2: a train along Lake Ombre."},
                               reason="zz the supplier replaced the boat")
    out = await ap.put_tour_draft(tour_id, _request(conn), body)
    assert out["created"] and out["draft"]["version_num"] == 2 and out["draft"]["base_version_num"] == 1
    assert out["draft"]["author"] == f"admin:{EDITOR}" and out["draft"]["checks_passed"] is None
    assert [d["field"] for d in out["diff"]] == ["aa_itineraries"]
    row = await conn.fetchrow("SELECT status::text AS s, origin, edit_reason, human_edited FROM "
                              "silver_aa_internal.generated_content WHERE id = $1::uuid", out["draft"]["id"])
    assert (row["s"], row["origin"], row["edit_reason"], row["human_edited"]) == \
        ("draft", "human", "zz the supplier replaced the boat", True)
    # the live version and the Master are untouched
    assert await conn.fetchval("SELECT generated_content_id::text AS g FROM gold_aa_internal.published_tours "
                               "WHERE tour_id = $1::uuid", tour_id) == v1
    # a second save edits the same draft (one open draft per tour) and resets the checks
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true WHERE id = $1::uuid",
                       out["draft"]["id"])
    out2 = await ap.put_tour_draft(tour_id, _request(conn, user=LEAD, role="content_lead"), ap.drafts.DraftSave(
        fields={"aa_summary": "Three calm days"}, reason="zz tone"))
    assert not out2["created"] and out2["draft"]["id"] == out["draft"]["id"]
    assert out2["draft"]["checks_passed"] is None
    assert out2["draft"]["editors"] == [f"admin:{EDITOR}", f"admin:{LEAD}"]
    audits = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = "
                                 "'tour_version.draft_saved' AND resource_id = $1", out["draft"]["id"])
    assert audits == 2


@pytest.mark.asyncio
async def test_the_live_version_is_not_edited_in_place(conn):
    tour_id, v1 = await _tour(conn)
    req = _request(conn, body={"aa_name": "Ombre Lake Trail"})
    with pytest.raises(HTTPException) as e:
        await ap.update_generated_content_fields(tour_id, v1, req, x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "edit_live_version"


@pytest.mark.asyncio
async def test_publish_needs_passing_checks_then_locks_the_master(conn):
    tour_id, _ = await _tour(conn)
    out = await ap.put_tour_draft(tour_id, _request(conn), ap.drafts.DraftSave(
        fields={"aa_name": "Ombre Lake Trail"}, reason="zz clearer name"))
    with pytest.raises(HTTPException) as e:
        await ap.publish_tour_draft(tour_id, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "revalidate_required"
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true WHERE id = $1::uuid",
                       out["draft"]["id"])

    async def _export(version_id, **kw):            # the publish, in this transaction (process_export's own connection
        assert kw["contract_exit"] == "human_publish"   # would not see the uncommitted rows)
        await conn.execute("UPDATE gold_aa_internal.published_tours SET generated_content_id = $2::uuid "
                           "WHERE tour_id = $1::uuid", tour_id, version_id)
        await human_lock.set_lock(conn, tour_id, by=kw["lock_by"], reason=kw["lock_reason"])
        return {"catalog_id": "pt-zz", "atomize_job_id": None}
    with patch("services.export.handler.process_export", _export):
        res = await ap.publish_tour_draft(tour_id, _request(conn), x_admin_secret="s")
    assert res["published"] and res["version_num"] == 2
    lock = await human_lock.lock_of(conn, tour_id)
    assert lock["by"] == f"admin:{EDITOR}" and lock["reason"] == "zz clearer name"
    assert (await ap.get_tour_draft(tour_id, _request(conn), x_admin_secret="s"))["draft"] is None
    # only a person lifts the lock, with a reason, audited
    gone = await ap.unlock_tour(tour_id, _request(conn, role="content_lead", user=LEAD),
                                ap.drafts.UnlockBody(reason="zz rerun the writer"), x_admin_secret="s")
    assert gone["unlocked"] and await human_lock.lock_of(conn, tour_id) is None


@pytest.mark.asyncio
async def test_two_person_switch_refuses_the_writer_and_the_content_role(conn):
    tour_id, _ = await _tour(conn)
    out = await ap.put_tour_draft(tour_id, _request(conn), ap.drafts.DraftSave(
        fields={"aa_name": "Ombre Lake Trail"}, reason="zz clearer name"))
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true WHERE id = $1::uuid",
                       out["draft"]["id"])
    await _switch(conn, True)
    with pytest.raises(HTTPException) as e:          # content (no content.publish)
        await ap.publish_tour_draft(tour_id, _request(conn, role="content", user=LEAD), x_admin_secret="s")
    assert e.value.status_code == 403 and e.value.detail["code"] == "two_person_required"
    # the lead edits the draft too: now an editor of it, so the lead may not publish it either
    await ap.put_tour_draft(tour_id, _request(conn, role="content_lead", user=LEAD), ap.drafts.DraftSave(
        fields={"aa_summary": "Three calm days"}, reason="zz tone"))
    await conn.execute("UPDATE silver_aa_internal.generated_content SET revalidate_passed = true WHERE id = $1::uuid",
                       out["draft"]["id"])
    with pytest.raises(HTTPException) as e:
        await ap.publish_tour_draft(tour_id, _request(conn, role="content_lead", user=LEAD), x_admin_secret="s")
    assert e.value.status_code == 403 and e.value.detail["code"] == "same_person"
    # another lead, who wrote nothing of it, publishes it
    other = "00000000-0000-4000-8000-0000000000e3"
    with patch("services.export.handler.process_export", AsyncMock(return_value={"catalog_id": "pt-zz"})) as ex:
        res = await ap.publish_tour_draft(tour_id, _request(conn, role="content_lead", user=other), x_admin_secret="s")
    assert res["published"] and ex.await_args.kwargs["lock_by"] == f"admin:{other}"


@pytest.mark.asyncio
async def test_an_automatic_rerun_skips_a_locked_tour(conn):
    tour_id, _ = await _tour(conn)
    assert await human_lock.set_lock(conn, tour_id, by="admin:zz", reason="zz hand edit")
    assert set(await human_lock.locks_of(conn, [tour_id, str(uuid.uuid4())])) == {tour_id}
    from services.jobs import s1_rewrite_job as job
    ctx = SimpleNamespace(pool=_OneConnPool(conn), job_id="job-zz", progress=AsyncMock())
    out = await job._human_lock_skip(ctx, tour_id)
    assert out["skipped"] and out["reason"] == "human_locked"


@pytest.mark.asyncio
async def test_a_raw_edit_keeps_the_old_values_and_the_price_needs_the_pricing_role(conn):
    tour_id, _ = await _tour(conn)
    req = _request(conn, body={"inclusions": "Water, lunch", "reason": "zz supplier e-mail"})
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):
        out = await ap.update_raw_tour_fields(tour_id, req, x_admin_secret="s")
        assert out["revision_num"] == 1
        rev = await conn.fetchrow("SELECT source, fields, before::text AS b, after::text AS a, actor, reason FROM "
                                  "silver_aa_internal.raw_tour_revision WHERE tour_id = $1::uuid", tour_id)
        assert (rev["source"], list(rev["fields"]), json.loads(rev["b"]), json.loads(rev["a"])) == \
            ("staff_edit", ["inclusions"], {"inclusions": "Water"}, {"inclusions": "Water, lunch"})
        assert rev["actor"] == f"admin:{EDITOR}" and rev["reason"] == "zz supplier e-mail"
        audit = await conn.fetchval("SELECT details::text AS d FROM acp_shared.audit_log WHERE action = "
                                    "'raw_tour.fields_edited' AND resource_id = $1", tour_id)
        assert json.loads(audit)["before"] == {"inclusions": "Water"}
        for role in ("content", "content_lead"):
            with pytest.raises(HTTPException) as e:
                await ap.update_raw_tour_fields(tour_id, _request(conn, role=role, body={"price_raw": "From 1 USD"}),
                                                x_admin_secret="s")
            assert e.value.status_code == 403
        await ap.update_raw_tour_fields(tour_id, _request(conn, role="admin", body={"price_raw": "From 320 USD"}),
                                        x_admin_secret="s")
    assert await conn.fetchval("SELECT price_raw FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                               tour_id) == "From 320 USD"


@pytest.mark.asyncio
async def test_an_excel_reimport_merges_field_by_field_and_the_hand_edit_wins(conn):
    tour_id, _ = await _tour(conn)
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):
        await ap.update_raw_tour_fields(tour_id, _request(conn, body={"inclusions": "Water, picnic lunch"}),
                                        x_admin_secret="s")
    staging = str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.upload_staging (batch_id, tenant_id, parsed_payload, matched_tour_id) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING id", uuid.UUID(BATCH_ID), MASTER, json.dumps(
            {"src_name": "Ombre Lake Walk 3D", "duration": "4 days", "inclusions": "Water, lunch box",
             "period": ""}), uuid.UUID(tour_id)))
    preview = await ap.merge_preview(staging, _request(conn), x_admin_secret="s")
    assert [a["field"] for a in preview["auto"]] == ["duration"]
    assert [c["field"] for c in preview["conflicts"]] == ["inclusions"] and preview["hand_edited"] == ["inclusions"]
    body = ap.DecideRequest(decision="merge", resolutions={}, reason="zz new supplier sheet")
    with pytest.raises(HTTPException) as e:
        await ap.decide_staging(staging, body, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 422 and e.value.detail["code"] == "merge_unresolved"
    body = ap.DecideRequest(decision="merge", resolutions={"inclusions": "current"}, reason="zz new supplier sheet")
    out = await ap.decide_staging(staging, body, _request(conn), x_admin_secret="s")
    assert out["pending"] == ["duration"] and out["kept"] == ["inclusions"] and out["published"] is False
    # T1: the result is a draft, the tour is untouched until a person publishes it
    row = await conn.fetchrow("SELECT duration, inclusions FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                              tour_id)
    assert (row["duration"], row["inclusions"]) == ("3 days", "Water, picnic lunch")
    assert not await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                                   staging)
    state = await ap.get_fact_draft(tour_id, _request(conn), x_admin_secret="s")
    assert state["draft"]["id"] == out["fact_draft_id"] and state["draft"]["stale"] == []
    fields = [(f["field"], f["before"], f["after"]) for f in state["draft"]["fields"]]
    assert fields == [("duration", "3 days", "4 days")]
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()) as fan:
        pub = await ap.publish_fact_draft(tour_id, _request(conn, role="content", user=LEAD), x_admin_secret="s")
    assert pub["updated"] == ["duration"] and fan.await_args.args[3] == ["duration"]
    row = await conn.fetchrow("SELECT duration, inclusions FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                              tour_id)
    assert (row["duration"], row["inclusions"]) == ("4 days", "Water, picnic lunch")
    rev = await conn.fetchrow("SELECT source, resolutions::text AS r, actor FROM silver_aa_internal.raw_tour_revision "
                              "WHERE tour_id = $1::uuid ORDER BY revision_num DESC LIMIT 1", tour_id)
    assert rev["source"] == "excel_merge" and json.loads(rev["r"]) == {"inclusions": "current"}
    assert rev["actor"] == f"admin:{EDITOR}"            # the person who chose the values; the audit row names LEAD
    d = await conn.fetchrow("SELECT status, decided_by, revision_num FROM silver_aa_internal.raw_tour_fact_draft "
                            "WHERE id = $1", out["fact_draft_id"])
    assert (d["status"], d["decided_by"]) == ("published", f"admin:{LEAD}") and d["revision_num"]
    assert (await ap.get_fact_draft(tour_id, _request(conn), x_admin_secret="s"))["draft"] is None


async def _staged(conn, tour_id, payload):
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.upload_staging (batch_id, tenant_id, parsed_payload, matched_tour_id) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING id", uuid.UUID(BATCH_ID), MASTER, json.dumps(payload),
        uuid.UUID(tour_id)))


@pytest.mark.asyncio
async def test_a_merge_never_changes_the_price_without_the_pricing_role(conn):
    tour_id, _ = await _tour(conn)
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):     # an admin set the price by hand
        await ap.update_raw_tour_fields(tour_id, _request(conn, role="admin", body={"price_raw": "From 320 USD"}),
                                        x_admin_secret="s")
    staging = await _staged(conn, tour_id, {"price_raw": "From 290 USD", "duration": "4 days"})
    preview = await ap.merge_preview(staging, _request(conn, role="content"), x_admin_secret="s")
    assert preview["read_only"] == ["price_raw"] and [c["field"] for c in preview["conflicts"]] == ["price_raw"]
    for role in ("content", "content_lead"):          # choosing the sheet's price is refused
        with pytest.raises(HTTPException) as e:
            await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={"price_raw": "incoming"},
                                                              reason="zz new sheet"),
                                    _request(conn, role=role), x_admin_secret="s")
        assert e.value.status_code == 403
    out = await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={"price_raw": "current"},
                                                            reason="zz new sheet"), _request(conn), x_admin_secret="s")
    assert out["pending"] == ["duration"] and out["kept"] == ["price_raw"]
    # an untouched price on another tour: held back for content, named in the result
    tour2, _ = await _tour(conn)
    staging2 = await _staged(conn, tour2, {"price_raw": "From 290 USD"})
    out2 = await ap.decide_staging(staging2, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                   _request(conn), x_admin_secret="s")
    assert out2["withheld"] == {"price_raw": "From 290 USD"} and out2["fact_draft_id"] is None
    assert await conn.fetchval("SELECT price_raw FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                               tour2) == "From 300 USD"


@pytest.mark.asyncio
async def test_a_sheet_draft_is_refused_when_stale_twice_open_or_by_its_author_under_two_person(conn):
    tour_id, _ = await _tour(conn)
    staging = await _staged(conn, tour_id, {"duration": "4 days"})
    out = await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                  _request(conn, role="content_lead", user=LEAD), x_admin_secret="s")
    again = await _staged(conn, tour_id, {"duration": "5 days"})
    with pytest.raises(HTTPException) as e:            # one open sheet draft per tour
        await ap.decide_staging(again, ap.DecideRequest(decision="merge", resolutions={}, reason="zz newer sheet"),
                                _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_open"
    await _switch(conn, True)
    with pytest.raises(HTTPException) as e:
        await ap.publish_fact_draft(tour_id, _request(conn, role="content_lead", user=LEAD), x_admin_secret="s")
    assert e.value.status_code == 403 and e.value.detail["code"] == "same_person"
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):     # someone edits the tour meanwhile
        await ap.update_raw_tour_fields(tour_id, _request(conn, body={"duration": "3 days and 2 nights"}),
                                        x_admin_secret="s")
    other = "00000000-0000-4000-8000-0000000000e3"
    with pytest.raises(HTTPException) as e:
        await ap.publish_fact_draft(tour_id, _request(conn, role="content_lead", user=other), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_stale"
    gone = await ap.discard_fact_draft(tour_id, _request(conn), x_admin_secret="s")
    assert gone["discarded"] == out["fact_draft_id"]
    assert await conn.fetchval("SELECT duration FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid",
                               tour_id) == "3 days and 2 nights"


@pytest.mark.asyncio
async def test_an_open_draft_is_edited_only_through_the_draft_editor(conn):
    tour_id, _ = await _tour(conn)
    out = await ap.put_tour_draft(tour_id, _request(conn), ap.drafts.DraftSave(
        fields={"aa_name": "Ombre Lake Trail"}, reason="zz clearer name"))
    req = _request(conn, role="content_lead", user=LEAD, body={"aa_summary": "My own words"})
    with pytest.raises(HTTPException) as e:
        await ap.update_generated_content_fields(tour_id, out["draft"]["id"], req, x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "edit_draft_version"


@pytest.mark.asyncio
async def test_a_check_that_ran_on_older_text_never_passes_the_newer_text(conn):
    tour_id, _ = await _tour(conn)
    out = await ap.put_tour_draft(tour_id, _request(conn), ap.drafts.DraftSave(
        fields={"aa_name": "Ombre Lake Trail"}, reason="zz clearer name"))
    vid = out["draft"]["id"]
    read = await conn.fetchrow("SELECT tour_id, edited_at FROM silver_aa_internal.generated_content "
                               "WHERE id = $1::uuid", vid)        # the checks start on text A
    await conn.execute("UPDATE silver_aa_internal.generated_content SET edited_at = edited_at + interval '1 second', "
                       "aa_name = 'Ombre Lake Path', revalidate_passed = NULL WHERE id = $1::uuid", vid)   # save B
    result = {"quality_score": 9.0, "sub_scores": {}, "failure_codes": []}
    with pytest.raises(ap._revalidate._StaleText):
        async with conn.transaction():
            await ap._revalidate._write_verdict(conn, vid, read, MASTER, result, True)
    assert await conn.fetchval("SELECT revalidate_passed FROM silver_aa_internal.generated_content "
                               "WHERE id = $1::uuid", vid) is None
    with pytest.raises(HTTPException) as e:
        await ap.publish_tour_draft(tour_id, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "revalidate_required"
    fresh = await conn.fetchrow("SELECT tour_id, edited_at FROM silver_aa_internal.generated_content "
                                "WHERE id = $1::uuid", vid)        # a run on the saved text passes it
    async with conn.transaction():
        await ap._revalidate._write_verdict(conn, vid, fresh, MASTER, result, True)
    assert await conn.fetchval("SELECT revalidate_passed FROM silver_aa_internal.generated_content "
                               "WHERE id = $1::uuid", vid) is True
