"""AA-679 — migration 225 (schema `catalog`, the CIS catalog read model) against real Postgres.

Lesson S231: conftest swallows migration errors, so the first test fails loudly when 225 did not
apply. The others seed live / not-live tours in one transaction, read the views as a consumer would,
and roll back.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-000000000099"
VIEWS = ("v_tour", "v_tour_day", "v_tour_place", "v_tour_photo", "v_destination", "v_destination_photo")

CANONICAL = (
    "Day 1 — Arrive in Hanoi\nWelcome dinner in the Old Quarter.\n\n"
    "Day 2 — Ha Long Bay\nCruise among the karsts.\nOvernight on board.\n"
    "Day 3 — Depart\nTransfer to the airport."
)
REPEATED_DAY = "Day 1 — First\nA.\n\nDay 2 — Old title\nB.\n\nDay 2 — New title\nC."


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tour(c, *, name, itinerary, tenant=MASTER_TENANT, master_status="active", source_status="active",
          duration=None, country="Vietnam", group=None):
    tour_id = str(uuid.uuid4())
    c.execute("""
        INSERT INTO silver_aa_internal.raw_tours
            (tour_id, tenant_id, batch_id, src_name, country, duration, inclusions, exclusions,
             activities, source_group_id, source_status)
        VALUES (%s, %s, %s, %s, %s, %s, 'Guide', 'Flights', '["kayaking"]'::jsonb, %s, %s)""",
              (tour_id, tenant, BATCH_ID, name, country, duration, group or str(uuid.uuid4()), source_status))
    c.execute("""
        INSERT INTO gold_aa_internal.published_tours
            (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, aa_highlights, aa_itineraries,
             master_status)
        VALUES (%s, %s, %s, %s, 'Summary', '["Karsts"]'::jsonb, %s, %s)""",
              (tour_id, str(uuid.uuid4()), tenant, name, itinerary, master_status))
    return tour_id


def _atom(c, tour_id, *, day, place, activity=None, deleted=False, empty=False):
    atom_id = f"zz679-{uuid.uuid4()}"
    c.execute("""
        INSERT INTO acp_contract.tour_atoms
            (atom_id, tour_id, text, itinerary_day, place, activity_type, deleted, is_empty_marker)
        VALUES (%s, %s, 'atom text', %s, %s, %s, %s, %s)""",
              (atom_id, tour_id, day, place, activity, deleted, empty))
    return atom_id


def test_migration_225_applied(cur):
    for v in VIEWS:
        cur.execute("SELECT to_regclass(%s)", (f"catalog.{v}",))
        assert cur.fetchone()[0], f"catalog.{v} missing — migration 225 did not apply (see conftest output)"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '225'")
    assert cur.fetchone()
    for role in ("aa_app_user", "tripplanner", "booking"):
        cur.execute("SELECT has_schema_privilege(%s, 'catalog', 'USAGE')", (role,))
        assert cur.fetchone()[0], role
        for v in VIEWS:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"catalog.{v}"))
            assert cur.fetchone()[0], (role, v)
    # the consumer roles get the read model only, never the pipeline tables behind it
    cur.execute("SELECT has_table_privilege('booking', 'gold_aa_internal.published_tours', 'SELECT')")
    assert not cur.fetchone()[0]


def test_v_tour_keeps_only_live_master_content(cur):
    tag = uuid.uuid4().hex[:8]
    group = str(uuid.uuid4())
    live = _tour(cur, name=f"Hanoi and Ha Long {tag}", itinerary=CANONICAL, duration="8 Days", group=group)
    inactive = _tour(cur, name=f"Inactive {tag}", itinerary=CANONICAL, master_status="inactive")
    trashed = _tour(cur, name=f"Trashed {tag}", itinerary=CANONICAL, master_status="trashed")
    superseded = _tour(cur, name=f"Superseded {tag}", itinerary=CANONICAL, source_status="superseded")
    other = _tour(cur, name=f"Other tenant {tag}", itinerary=CANONICAL, tenant=OTHER_TENANT)
    cur.execute("UPDATE gold_aa_internal.published_tours SET deleted_at = now() WHERE tour_id = %s", (trashed,))

    cur.execute("SELECT tour_id::text FROM catalog.v_tour WHERE title LIKE %s", (f"%{tag}",))
    assert {r[0] for r in cur.fetchall()} == {live}
    assert not {inactive, trashed, superseded, other} & {live}

    cur.execute("""
        SELECT source_system, source_id, slug, title, country, duration_days, summary, highlights,
               inclusions, exclusions, activities, itinerary_text, content_hash, published_at, updated_at
          FROM catalog.v_tour WHERE tour_id = %s""", (live,))
    (system, source_id, slug, title, country, days, summary, highlights, inc, exc, activities, text,
     content_hash, published_at, updated_at) = cur.fetchone()
    assert (system, source_id) == ("cis", group)              # stable across re-uploads, not tour_id
    assert slug == f"hanoi-and-ha-long-{tag}-vietnam-{group.replace('-', '')[:8]}"
    assert (title, country, days, summary) == (f"Hanoi and Ha Long {tag}", "Vietnam", 8, "Summary")
    assert highlights == ["Karsts"] and activities == ["kayaking"]
    assert (inc, exc, text) == ("Guide", "Flights", CANONICAL)
    assert len(content_hash) == 32 and updated_at == published_at

    # content_hash moves when content moves
    cur.execute("UPDATE gold_aa_internal.published_tours SET aa_summary = 'New' WHERE tour_id = %s", (live,))
    cur.execute("SELECT content_hash FROM catalog.v_tour WHERE tour_id = %s", (live,))
    assert cur.fetchone()[0] != content_hash


def test_duration_days_falls_back_to_itinerary(cur):
    t = _tour(cur, name=f"No duration {uuid.uuid4().hex[:8]}", itinerary=CANONICAL, duration="Flexible")
    cur.execute("SELECT duration_days FROM catalog.v_tour WHERE tour_id = %s", (t,))
    assert cur.fetchone()[0] == 3


def test_v_tour_day_matches_python_parser(cur):
    from services.content_generation.itinerary_utils import parse_canonical_itinerary_days

    for text in (CANONICAL, REPEATED_DAY):
        t = _tour(cur, name=f"Days {uuid.uuid4().hex[:8]}", itinerary=text)
        cur.execute("""SELECT day_number, title, body FROM catalog.v_tour_day
                        WHERE tour_id = %s ORDER BY day_number""", (t,))
        got = {d: {"title": title, "body": body} for d, title, body in cur.fetchall()}
        assert got == parse_canonical_itinerary_days(text)

    legacy = _tour(cur, name=f"Legacy {uuid.uuid4().hex[:8]}", itinerary="Day one: arrive. Day two: leave.")
    cur.execute("SELECT count(*) FROM catalog.v_tour_day WHERE tour_id = %s", (legacy,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT itinerary_text FROM catalog.v_tour WHERE tour_id = %s", (legacy,))
    assert cur.fetchone()[0] == "Day one: arrive. Day two: leave."


def test_v_tour_place_and_day_places(cur):
    tag = uuid.uuid4().hex[:8]
    hanoi, halong = f"Hanoi {tag}", f"Ha Long Bay {tag}"
    t = _tour(cur, name=f"Places {tag}", itinerary=CANONICAL)
    a1 = _atom(cur, t, day=1, place=hanoi, activity="food")
    _atom(cur, t, day=1, place=hanoi.lower(), activity="culture")           # same place, other case
    _atom(cur, t, day=2, place=halong, activity="cruise")
    _atom(cur, t, day=2, place=f"Ghost {tag}", deleted=True)                # deleted atom: not a place
    _atom(cur, t, day=2, place=f"Marker {tag}", empty=True)                 # empty marker: not a place
    _atom(cur, t, day=3, place="  ")                                        # blank place: skipped

    card = str(uuid.uuid4())
    cur.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, locality, country)
                   VALUES (%s, %s, %s, 'Hoan Kiem', 'Vietnam')""", (card, f"hanoi-{tag}", hanoi))
    cur.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (a1, card))
    dest = str(uuid.uuid4())
    cur.execute("""INSERT INTO shared.destinations (id, name, country, lat, lng)
                   VALUES (%s, %s, 'Vietnam', 21.03, 105.85)""", (dest, hanoi))
    # same name as a place, wrong country: never pinned
    cur.execute("""INSERT INTO shared.destinations (name, country, lat, lng)
                   VALUES (%s, 'Thailand', 1, 1)""", (halong,))

    cur.execute("""
        SELECT day_number, position, place_name, place_card_id::text, locality, country,
               destination_id::text, lat, lng, activity_types, atom_count
          FROM catalog.v_tour_place WHERE tour_id = %s ORDER BY day_number, position""", (t,))
    rows = cur.fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, 1, hanoi), (2, 1, halong)]
    day1, day2 = rows
    assert day1[3:9] == (card, "Hoan Kiem", "Vietnam", dest, 21.03, 105.85)
    assert sorted(day1[9]) == ["culture", "food"] and day1[10] == 2
    assert day2[3] is None and day2[6] is None and day2[7] is None      # no card, no pin
    assert day2[9] == ["cruise"]

    cur.execute("SELECT day_number, places FROM catalog.v_tour_day WHERE tour_id = %s ORDER BY 1", (t,))
    assert cur.fetchall() == [(1, [hanoi]), (2, [halong]), (3, [])]


def test_photos_and_destinations(cur):
    tag = uuid.uuid4().hex[:8]
    t = _tour(cur, name=f"Photos {tag}", itinerary=CANONICAL)
    dest = str(uuid.uuid4())
    cur.execute("""INSERT INTO shared.destinations (id, name, country, lat, lng, cover_image_url)
                   VALUES (%s, %s, 'Vietnam', 10.0, 106.0, 'https://example.test/c.jpg')""",
                (dest, f"Saigon {tag}"))
    photo = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO shared.place_photo
            (id, drive_file_id, drive_root_id, folder_path, country, file_name, tour_id, destination_id,
             match_source, status, s3_key_large)
        VALUES (%s, %s, 'root', 'Vietnam/x', 'Vietnam', 'a.jpg', %s, %s, 'manual', 'matched', 'k/large.jpg')""",
                (photo, f"drive-{tag}", t, dest))

    cur.execute("SELECT cover_photo_path, photo_count FROM catalog.v_tour WHERE tour_id = %s", (t,))
    assert cur.fetchone() == (f"/content/photos/{photo}", 1)
    cur.execute("SELECT photo_id::text, position FROM catalog.v_tour_photo WHERE tour_id = %s", (t,))
    assert cur.fetchall() == [(photo, 1)]

    cur.execute("""SELECT source_system, source_id, name, lat, lng, cover_photo_path, cover_image_url, photo_count
                     FROM catalog.v_destination WHERE destination_id = %s""", (dest,))
    assert cur.fetchone() == ("cis", dest, f"Saigon {tag}", 10.0, 106.0, f"/content/photos/{photo}",
                              "https://example.test/c.jpg", 1)
    cur.execute("SELECT photo_id::text FROM catalog.v_destination_photo WHERE destination_id = %s", (dest,))
    assert cur.fetchall() == [(photo,)]
