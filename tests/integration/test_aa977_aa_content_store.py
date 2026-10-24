"""AA-977 (CRM-B5) — services/aa_content (store + served reader) on real Postgres (CIS 275), logged in as the CIS
runtime role cis_app. Each test runs in one transaction that is rolled back (the store's own transactions become
savepoints).

The full life of an item: create (draft v1, not served) -> publish (served, hash reproducible) -> draft v2 (v1 still
served unchanged) -> publish v2 (v1 still fetchable with the same body and hash: rule E) -> discard a draft ->
retire (hidden from the list, versions still fetchable) -> restore; the refusals (stale stamp, visa rule, no draft,
retired, duplicate locale, kind mismatch in a group); the admin list's filters and the group read.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from services.aa_content import reader, store  # noqa: E402
from services.aa_content.store import ContentConflict, ContentNotFound  # noqa: E402
from services.aa_content.validate import ContentInvalid  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "admin:0b6f1a7e-0000-4000-8000-000000000001"
GOOD_BODY = "## Visa\n\nApply only on the official portal: https://evisa.gov.vn\n\nMoney: cash in VND."


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(migration_body(275))          # other modules re-create cis_app (its grants go with it)
        await c.execute("GRANT USAGE ON SCHEMA gold_aa_internal TO cis_app")   # 244 on Dev; the replay skips it
        await c.execute("SET LOCAL ROLE cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _stamp(item):
    return datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))


def _group():
    return f"zz977-{uuid.uuid4().hex[:10]}"


async def _create(conn, group, *, locale="en", kind="prep_pack", body=GOOD_BODY, countries=("VN",)):
    return await store.create_item(conn, kind=kind, locale=locale, group_key=group, title="Before you go: Vietnam",
                                   body_markdown=body, applies_to={"country_codes": list(countries)}, actor=ACTOR)


async def _served_ids(conn, **filters):
    page = await reader.list_published(conn, after=None, kind=filters.get("kind"), locale=filters.get("locale"),
                                       limit=50)
    return {i["content_id"]: i["version"] for i in page.items}


@pytest.mark.asyncio
async def test_full_cycle_rules_r_and_e(conn):
    item = await _create(conn, _group(), body="Visa on arrival, ask your agent.")
    cid = uuid.UUID(item["content_id"])
    assert (item["state"], item["draft_version"], item["published_version"]) == ("draft", 1, None)
    assert item["created_by"] == ACTOR and item["versions"][0]["applies_to"] == {"country_codes": ["VN"]}
    # R: a draft is never served
    assert item["content_id"] not in await _served_ids(conn)
    assert await reader.get_published_version(conn, cid, 1) is None

    with pytest.raises(ContentConflict) as stale:
        await store.save_draft(conn, cid, title="x", body_markdown="y", applies_to=None,
                               expected_updated_at=_stamp(item) - timedelta(seconds=1), actor=ACTOR)
    assert stale.value.code == "stale"
    with pytest.raises(ContentInvalid) as visa:
        await store.publish(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    assert visa.value.code == "visa_needs_official_link"

    item = await store.save_draft(conn, cid, title="Before you go: Vietnam", body_markdown=GOOD_BODY,
                                  applies_to={"country_codes": ["vn"]}, expected_updated_at=_stamp(item),
                                  actor=ACTOR)
    out = await store.publish(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    v1 = await reader.get_published_version(conn, cid, 1)
    assert v1 == out["published"]
    assert v1["content_hash"] == reader.hash_of({k: v for k, v in v1.items() if k != "content_hash"})
    assert (out["item"]["state"], out["item"]["published_version"], out["item"]["draft_version"]) == \
        ("published", 1, None)
    assert (await _served_ids(conn))[str(cid)] == 1

    # draft v2: v1 stays served as it is
    item = await store.save_draft(conn, cid, title="Before you go: Vietnam (2027)", body_markdown=GOOD_BODY + "!",
                                  applies_to={"country_codes": ["VN", "KH"]},
                                  expected_updated_at=_stamp(out["item"]), actor=ACTOR)
    assert item["draft_version"] == 2 and (await _served_ids(conn))[str(cid)] == 1
    assert await reader.get_published_version(conn, cid, 2) is None
    preview = await store.get_version(conn, cid, 2)
    assert preview["status"] == "draft" and preview["applies_to"] == {"country_codes": ["VN", "KH"]}

    # publish v2: E, v1 unchanged and still fetchable
    out2 = await store.publish(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    assert out2["published"]["version"] == 2 and out2["published"]["content_hash"] != v1["content_hash"]
    assert await reader.get_published_version(conn, cid, 1) == v1
    assert (await _served_ids(conn))[str(cid)] == 2

    # discard a draft; then there is nothing to discard or publish
    item = await store.save_draft(conn, cid, title="v3", body_markdown="text", applies_to=None,
                                  expected_updated_at=_stamp(out2["item"]), actor=ACTOR)
    assert item["draft_version"] == 3
    item = await store.discard_draft(conn, cid, expected_updated_at=_stamp(item))
    assert item["draft_version"] is None and [v["version"] for v in item["versions"]] == [2, 1]
    with pytest.raises(ContentNotFound):
        await store.discard_draft(conn, cid, expected_updated_at=_stamp(item))
    with pytest.raises(ContentConflict) as no_draft:
        await store.publish(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    assert no_draft.value.code == "no_draft"

    # retire: hidden from the list, every published version still fetchable; no edits until restored
    item = await store.retire(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    assert item["state"] == "retired" and item["retired_by"] == ACTOR
    assert str(cid) not in await _served_ids(conn)
    assert await reader.get_published_version(conn, cid, 1) == v1
    for write in (store.save_draft(conn, cid, title="x", body_markdown="", applies_to=None,
                                   expected_updated_at=_stamp(item), actor=ACTOR),
                  store.publish(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)):
        with pytest.raises(ContentConflict) as retired:
            await write
        assert retired.value.code == "retired"
    with pytest.raises(ContentConflict):
        await store.retire(conn, cid, expected_updated_at=_stamp(item), actor=ACTOR)
    item = await store.restore(conn, cid, expected_updated_at=_stamp(item))
    assert item["state"] == "published" and (await _served_ids(conn))[str(cid)] == 2
    with pytest.raises(ContentConflict):
        await store.restore(conn, cid, expected_updated_at=_stamp(item))


@pytest.mark.asyncio
async def test_groups_pair_locales_of_one_kind(conn):
    group = _group()
    en = await _create(conn, group)
    vi = await _create(conn, group, locale="vi")
    with pytest.raises(ContentConflict) as dup:
        await _create(conn, group)
    assert dup.value.code == "duplicate"
    other = _group()
    await _create(conn, other, kind="campaign_block")
    with pytest.raises(ContentInvalid) as mismatch:
        await _create(conn, other, locale="vi", kind="prep_pack")
    assert mismatch.value.code == "group_kind_mismatch"
    items = await store.get_group(conn, group)
    assert [(i["locale"], i["content_id"]) for i in items] == [("en", en["content_id"]), ("vi", vi["content_id"])]
    assert await store.get_group(conn, "zz977-none") == []


@pytest.mark.asyncio
async def test_admin_list_filters_and_paging(conn):
    group = _group()
    drafted = await _create(conn, group, countries=("TH",))
    pub = await _create(conn, group, locale="vi", countries=("TH",))
    pub = (await store.publish(conn, uuid.UUID(pub["content_id"]), expected_updated_at=_stamp(pub),
                               actor=ACTOR))["item"]
    gone = await _create(conn, _group(), kind="starter_template", countries=())
    await store.retire(conn, uuid.UUID(gone["content_id"]), expected_updated_at=_stamp(gone), actor=ACTOR)

    async def ids(**kw):
        args = {"kind": None, "locale": None, "status": None, "country": None, "limit": 100, "offset": 0, **kw}
        page = await store.list_items(conn, **args)
        return page, [i["content_id"] for i in page["items"]]

    page, everything = await ids()
    assert {drafted["content_id"], pub["content_id"], gone["content_id"]} <= set(everything)
    assert page["total"] == len(everything)
    assert (await ids(status="draft"))[1].count(drafted["content_id"]) == 1
    assert pub["content_id"] not in (await ids(status="draft"))[1]
    assert pub["content_id"] in (await ids(status="published"))[1]
    assert gone["content_id"] in (await ids(status="retired"))[1]
    assert set((await ids(country="TH"))[1]) >= {drafted["content_id"], pub["content_id"]}
    assert gone["content_id"] not in (await ids(country="TH"))[1]
    assert (await ids(kind="starter_template", status="retired"))[1].count(gone["content_id"]) == 1
    page, one = await ids(limit=1, offset=1)
    assert len(one) == 1 and page["total"] == len(everything)
    row = next(i for i in (await ids())[0]["items"] if i["content_id"] == pub["content_id"])
    assert (row["state"], row["published_version"], row["country_codes"]) == ("published", 1, ["TH"])


@pytest.mark.asyncio
async def test_publish_stores_microsecond_published_at(conn):
    item = await _create(conn, _group())
    at = datetime(2026, 10, 24, 8, 0, 0, 654321, tzinfo=timezone.utc)
    out = await store.publish(conn, uuid.UUID(item["content_id"]), expected_updated_at=_stamp(item), actor=ACTOR,
                              now=at)
    served = await reader.get_published_version(conn, uuid.UUID(item["content_id"]), 1)
    assert served["published_at"] == "2026-10-24T08:00:00.654321Z" == out["published"]["published_at"]
