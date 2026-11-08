"""AA-1052 W6 — the source-change fan-out (services/quality/source_fanout.py, job kind source_fanout, CIS 392).

A change of a source (raw tour, Master, atom, place card) marks the stored writer objects written from it before the
change (S1 Master versions, T2 tenant versions, T9 pieces) in shared.source_change_mark with a typed reason: $0, no
model call, never a regeneration or a status change. An S1 / T2 object of a raw-tour change whose stored FactSheet
text equals the sheet rebuilt now is left unmarked (the edit did not touch what it read).
No database: a scripted connection. Invented tours, names and places only.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.quality import source_fanout as sf
from shared.jobs import registry

NOW = datetime(2026, 11, 8, 10, 30, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(days=2)
TOUR = "0b7f2a10-1111-4222-8333-444455556666"
RAW = {"tour_id": TOUR, "src_name": "Invented Ridge Walk", "src_subtitle": None, "src_summary": "A quiet walk.",
       "src_description": "Walk the invented ridge above Lake Imaginaria.", "src_highlights": ["Lake Imaginaria"],
       "src_itineraries": "Day 1: Ridge walk to Lake Imaginaria.", "country": "Nowhereland", "duration": "1 day",
       "price_raw": None, "inclusions": "Guide", "exclusions": None, "group_size": None, "best_time_to_go": None}


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class FakeConn:
    """Scripts fetch / fetchrow by the SQL constant they run; records every call."""

    def __init__(self, s1=(), t2=(), t9=(), card_atoms=(), raw=None):
        self.rows = {sf.S1_SQL: list(s1), sf.T2_SQL: list(t2), sf.T9_SQL: list(t9),
                     sf.CARD_ATOMS_SQL: [{"atom_id": a} for a in card_atoms]}
        self.raw = raw
        self.calls: list[tuple] = []
        self.upserts: list[tuple] = []

    def transaction(self):
        return _Txn()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows.get(sql, [])

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if sql == sf.RAW_ROW_SQL:
            return self.raw
        if sql == sf.UPSERT_SQL:
            self.upserts.append(args)
            return {"inserted": len(self.upserts) % 2 == 1, "mark_id": f"m{len(self.upserts)}"}
        raise AssertionError(f"unexpected fetchrow {sql[:60]}")


def _s1(oid="s1-a", fs_render=None, live=True):
    return {"object_kind": "s1", "object_id": oid, "tour_id": TOUR, "object_created_at": EARLIER, "version_num": 2,
            "fs_id": "fs-1" if fs_render is not None else None, "fs_render": fs_render, "live": live}


def _t2(oid="t2-a", fs_render=None):
    return {"object_kind": "t2", "object_id": oid, "tour_id": TOUR, "object_created_at": EARLIER,
            "version_number": 3, "status": "approved", "published_tour_id": "pt-old", "fs_id": None,
            "fs_render": fs_render}


def _t9(oid="t9-a"):
    return {"object_kind": "t9", "object_id": oid, "tour_id": TOUR, "object_created_at": EARLIER,
            "status": "approved", "fs_id": "fs-9", "atom_ids": ["atom-1", "atom-2"]}


def _change(kind=sf.RAW_TOUR, reason="raw_tour_edited", **kw):
    base = {"tour_id": TOUR} if kind in sf.TOUR_SCOPED else {}
    return sf.Change(source_kind=kind, reason=reason, changed_at=NOW, **{**base, **kw})


@pytest.fixture(autouse=True)
def _no_model_call(monkeypatch):
    """The fan-out never calls a model: any gateway call fails the test."""
    from shared.llm_client import client
    monkeypatch.setattr(client.LLMClient, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))


# ── the change ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_change_round_trips_through_the_job_payload():
    ch = _change(fields=("src_itineraries", "country"))
    back = sf.Change.from_payload(json.loads(json.dumps(ch.payload())))
    assert back == sf.Change(source_kind="raw_tour", reason="raw_tour_edited", changed_at=NOW, tour_id=TOUR,
                             fields=("country", "src_itineraries"))


@pytest.mark.parametrize("kw,match", [
    ({"source_kind": "weather", "reason": "raw_tour_edited", "tour_id": TOUR}, "unknown source kind"),
    ({"source_kind": "raw_tour", "reason": "made_up", "tour_id": TOUR}, "unknown reason"),
    ({"source_kind": "atom", "reason": "raw_tour_edited", "source_ref": "a"}, "not a atom change"),
    ({"source_kind": "raw_tour", "reason": "raw_tour_edited"}, "names its tour"),
    ({"source_kind": "place_card", "reason": "place_card_facts_edited"}, "names its place_card id"),
    ({"source_kind": "atom", "reason": "manual_recheck", "source_ref": "a"}, "manual re-check names a tour"),
    ({"source_kind": "atom", "reason": "atom_edited", "source_ref": "a" * 129}, "at most 128"),
])
def test_a_bad_change_is_refused(kw, match):
    with pytest.raises(sf.FanoutRefused, match=match):
        sf.Change(changed_at=NOW, **kw).validate()


def test_the_reasons_match_the_migration_check():
    sql = open("api/migrations/392_source_change_mark.sql", encoding="utf-8").read()
    for reason in sf.REASONS:
        assert f"'{reason}'" in sql
    for kind in sf.SOURCE_KINDS + sf.OBJECT_KINDS:
        assert f"'{kind}'" in sql


def test_the_dependents_of_each_source():
    assert sf.DEPENDENTS == {"raw_tour": ("s1", "t2", "t9"), "master": ("t2", "t9"), "atom": ("t9",),
                             "place_card": ("t9",)}


# ── the re-check ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_recheck_clears_only_an_unchanged_factsheet_of_a_raw_tour_change():
    s1 = sf.dependent_from_row(_s1(fs_render="SHEET"))
    assert sf.recheck(s1, _change(), "SHEET") == sf.UNCHANGED
    assert sf.recheck(s1, _change(), "OTHER") == sf.MARK
    assert sf.recheck(s1, _change(), None) == sf.MARK
    assert sf.recheck(sf.dependent_from_row(_s1()), _change(), "SHEET") == sf.MARK          # no stored sheet
    t9 = sf.dependent_from_row(_t9())
    assert sf.recheck(t9, _change(), "SHEET") == sf.MARK
    assert sf.recheck(s1, _change(sf.MASTER, "master_published"), "SHEET") == sf.MARK


def test_details_carry_ids_never_text():
    for row in (_s1(fs_render="the stored sheet text"), _t2(fs_render="x"), _t9()):
        d = sf.dependent_from_row(row)
        args = sf.mark_args(d, _change(fields=("src_summary",)), "job-1")
        details = json.loads(args[-1])
        assert "the stored sheet text" not in args[-1] and "fs_render" not in details
        assert details["fields"] == ["src_summary"]
    assert sf.mark_args(sf.dependent_from_row(_t9()), _change(sf.ATOM, "atom_edited", source_ref="atom-1"), None)[:8] \
        == ("t9", "t9-a", TOUR, "atom", "atom-1", "atom_edited", NOW, EARLIER)


# ── the fan-out ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_raw_tour_edit_marks_its_dependents_and_calls_no_model():
    current = sf.current_tour_render(RAW)
    conn = FakeConn(s1=[_s1("s1-same", fs_render=current), _s1("s1-old", fs_render="an older sheet")],
                    t2=[_t2()], t9=[_t9()], raw=RAW)
    out = asyncio.run(sf.fan_out(conn, _change(fields=("src_itineraries",)), job_id="job-1"))
    assert out["dependents"] == 4 and out["llm_calls"] == 0
    assert out["unchanged"] == [{"object_kind": "s1", "object_id": "s1-same"}]
    assert sorted(m["object_id"] for m in out["marked"] + out["bumped"]) == ["s1-old", "t2-a", "t9-a"]
    assert out["by_kind"] == {"s1": 1, "t2": 1, "t9": 1}
    assert [a[5] for a in conn.upserts] == ["raw_tour_edited"] * 3 and all(a[8] == "job-1" for a in conn.upserts)
    # the scope of each read: the tour, before the change; T9 by the tour's atoms
    t9_args = next(a for s, a in conn.calls if s == sf.T9_SQL)
    assert t9_args[:3] == (TOUR, [], NOW) and t9_args[3] == ["approved", "held"]
    # never an UPDATE of an object: the only write is the mark upsert
    assert all(s in (sf.S1_SQL, sf.T2_SQL, sf.T9_SQL, sf.RAW_ROW_SQL, sf.UPSERT_SQL) for s, _ in conn.calls)


def test_a_master_publish_re_checks_t2_and_t9_not_s1():
    conn = FakeConn(s1=[_s1()], t2=[_t2()], t9=[_t9()])
    out = asyncio.run(sf.fan_out(conn, _change(sf.MASTER, "master_published", source_ref="pt-1")))
    assert sorted(m["object_kind"] for m in out["marked"] + out["bumped"]) == ["t2", "t9"]
    assert not any(s == sf.S1_SQL for s, _ in conn.calls)
    assert next(a for s, a in conn.calls if s == sf.T2_SQL) == (TOUR, NOW, ["rejected", "failed"])
    assert conn.upserts[0][4] == "pt-1"                       # the Master's published_tours id, for the record


def test_an_atom_edit_and_a_place_card_edit_re_check_only_the_pieces_citing_them():
    conn = FakeConn(t9=[_t9()])
    asyncio.run(sf.fan_out(conn, _change(sf.ATOM, "atom_deleted", source_ref="atom-1", tour_id=TOUR)))
    assert next(a for s, a in conn.calls if s == sf.T9_SQL)[:2] == (None, ["atom-1"])
    assert not any(s in (sf.S1_SQL, sf.T2_SQL) for s, _ in conn.calls)
    conn = FakeConn(t9=[_t9()], card_atoms=["atom-7", "atom-8"])
    out = asyncio.run(sf.fan_out(conn, _change(sf.PLACE_CARD, "place_card_facts_edited", source_ref="card-1")))
    assert next(a for s, a in conn.calls if s == sf.T9_SQL)[:2] == (None, ["atom-7", "atom-8"])
    assert conn.upserts[0][3:5] == ("place_card", "card-1") and out["by_kind"] == {"t9": 1}
    conn = FakeConn(t9=[_t9()], card_atoms=[])                    # a card with no linked atom: nothing to read
    assert asyncio.run(sf.fan_out(conn, _change(sf.PLACE_CARD, "place_cards_merged",
                                                source_ref="card-2")))["dependents"] == 0
    assert not any(s == sf.T9_SQL for s, _ in conn.calls)


def test_a_dry_run_writes_nothing():
    conn = FakeConn(t2=[_t2()], t9=[_t9()])
    out = asyncio.run(sf.fan_out(conn, _change(sf.MASTER, "manual_recheck"), dry_run=True))
    assert len(out["marked"]) == 2 and conn.upserts == [] and out["dry_run"] is True


def test_the_upsert_keeps_one_open_mark_per_object_and_source():
    sql = sf.UPSERT_SQL
    assert "ON CONFLICT (object_kind, object_id, source_kind, source_ref) WHERE cleared_at IS NULL" in sql
    assert "hits = m.hits + 1" in sql and "last_seen_at = now()" in sql


def test_read_tour_counts_open_marks_by_kind_and_reason():
    rows = [{"mark_id": "1", "object_kind": "t9", "reason": "atom_edited", "cleared_at": None, "detected_at": NOW,
             "details": '{"atom_ids": ["a"]}'},
            {"mark_id": "2", "object_kind": "t2", "reason": "master_published", "cleared_at": None,
             "detected_at": NOW, "details": {}},
            {"mark_id": "3", "object_kind": "t2", "reason": "master_published", "cleared_at": NOW,
             "detected_at": EARLIER, "details": {}}]
    conn = MagicMock(fetch=AsyncMock(return_value=rows))
    out = asyncio.run(sf.read_tour(conn, TOUR))
    assert out["open"] == 2 and out["cleared_recent"] == 1
    assert out["by_object_kind"] == {"t2": 1, "t9": 1} and out["by_reason"] == {"atom_edited": 1,
                                                                                "master_published": 1}
    assert out["marks"][0]["details"] == {"atom_ids": ["a"]} and out["marks"][0]["detected_at"] == NOW.isoformat()
    assert conn.fetch.await_args.args[1:] == (TOUR, sf.MARKS_LIMIT)


# ── enqueue and the job ─────────────────────────────────────────────────────────────────────────────────────────────

def test_enqueue_folds_a_burst_of_one_source_into_one_job_per_minute():
    assert sf.idempotency_key(_change()) == f"source_fanout:raw_tour:{TOUR}:2026-11-08T10:30"
    assert sf.idempotency_key(_change(sf.ATOM, "atom_edited", source_ref="atom-1")) == \
        "source_fanout:atom:atom-1:2026-11-08T10:30"
    with patch("shared.jobs.queue.enqueue", AsyncMock(return_value=("j1", True))) as enq:
        assert asyncio.run(sf.enqueue(object(), _change(), created_by="admin:x")) == "j1"
    assert enq.await_args.args[1] == "source_fanout" and enq.await_args.kwargs["idempotency_key"].endswith("10:30")
    with patch("shared.jobs.queue.enqueue", AsyncMock(return_value=("j1", False))):
        assert asyncio.run(sf.enqueue(object(), _change())) is None


def test_enqueue_safe_never_raises():
    with patch("shared.jobs.queue.enqueue", AsyncMock(side_effect=OSError("db down"))):
        out = asyncio.run(sf.enqueue_safe(object(), _change()))
    assert out["error"].startswith("OSError")
    out = asyncio.run(sf.enqueue_safe(object(), sf.Change(source_kind="raw_tour", reason="raw_tour_edited",
                                                          changed_at=NOW)))          # no tour: refused, not raised
    assert "names its tour" in out["error"]


def test_the_job_kind_is_registered_and_runs_the_fan_out():
    from services.jobs import source_fanout_job
    from shared.llm_client import capacity
    kind = registry.kinds()["source_fanout"]
    assert kind.max_attempts == 3 and not kind.deploy_blocking
    assert capacity.KIND_LINES["source_fanout"] is None              # no model call: never held by the backpressure
    conn = FakeConn(t9=[_t9()])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    ctx = MagicMock(payload=_change(sf.ATOM, "atom_edited", source_ref="atom-1").payload(), job_id="job-9",
                    pool=MagicMock(acquire=MagicMock(return_value=acquire)))
    out = asyncio.run(source_fanout_job.run(ctx))
    assert out["kind"] == "source_fanout" and out["by_kind"] == {"t9": 1} and conn.upserts[0][8] == "job-9"
    ctx.set_result.assert_called_once()
    ctx.payload = {"source_kind": "raw_tour"}
    with pytest.raises(registry.NonRetryable):
        asyncio.run(source_fanout_job.run(ctx))


# ── the hooks on the write paths ────────────────────────────────────────────────────────────────────────────────────

def _req(pool):
    return MagicMock(app=MagicMock(state=MagicMock(pool=pool)))


def _pool_with(conn):
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(acquire=MagicMock(return_value=acquire))


def test_a_raw_tour_edit_enqueues_the_fan_out_with_the_field_names(monkeypatch):
    from api.routers import admin_pipeline as ap
    monkeypatch.setattr(ap, "verify_admin_secret", lambda *_: None)
    conn = MagicMock(fetchrow=AsyncMock(return_value={"tour_id": TOUR, "changed_at": NOW}))
    request = _req(_pool_with(conn))
    request.json = AsyncMock(return_value={"src_summary": "A new invented summary."})
    with patch.object(sf, "enqueue_safe", AsyncMock(return_value={"job_id": "j"})) as hook:
        out = asyncio.run(ap.update_raw_tour_fields(TOUR, request, x_admin_secret="x"))
    assert out == {"tour_id": TOUR, "updated": ["src_summary"]}
    ch = hook.await_args.args[1]
    assert (ch.source_kind, ch.reason, ch.tour_id, ch.fields, ch.changed_at) == \
        ("raw_tour", "raw_tour_edited", TOUR, ("src_summary",), NOW)


def test_an_atom_edit_enqueues_the_fan_out_of_that_atom():
    from api.routers import admin_atoms
    row = {"atom_id": "atom-1", "tour_id": TOUR, "text": "x", "deleted": True, "visual_potential": 1, "media": [],
           "created_at": EARLIER, "updated_at": NOW}
    conn = MagicMock(fetchrow=AsyncMock(return_value=row))
    with patch.object(sf, "enqueue_safe", AsyncMock(return_value={"job_id": "j"})) as hook, \
            patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("r", True))), \
            patch.object(admin_atoms, "_record_atom_delete_outcome", AsyncMock()):
        asyncio.run(admin_atoms.patch_atom("atom-1", admin_atoms.AtomPatchRequest(deleted=True),
                                           _req(_pool_with(conn))))
    ch = hook.await_args.args[1]
    assert (ch.source_kind, ch.reason, ch.source_ref, ch.changed_at) == ("atom", "atom_deleted", "atom-1", NOW)


def test_a_place_card_facts_edit_enqueues_the_fan_out_of_the_card():
    from api.routers import admin_content_quality as cq
    with patch.object(sf, "enqueue_safe", AsyncMock(return_value={"job_id": "j"})) as hook:
        asyncio.run(cq._source_fanout_card(object(), "card-1", "place_card_facts_edited", NOW))
        asyncio.run(cq._source_fanout_card(object(), "card-1", "place_card_status_changed", "not a time"))
    first, second = (c.args[1] for c in hook.await_args_list)
    assert (first.source_kind, first.reason, first.source_ref, first.changed_at) == \
        ("place_card", "place_card_facts_edited", "card-1", NOW)
    assert isinstance(second.changed_at, datetime)                  # a missing time falls back to now


def test_a_master_publish_enqueues_the_fan_out_and_never_raises():
    from services.export import handler
    conn = MagicMock(fetchval=AsyncMock(return_value=NOW))
    with patch.object(sf, "enqueue_safe", AsyncMock(return_value={"job_id": "j"})) as hook:
        asyncio.run(handler._source_fanout_master(conn, TOUR, "pt-new"))
    ch = hook.await_args.args[1]
    assert (ch.source_kind, ch.reason, ch.tour_id, ch.source_ref) == ("master", "master_published", TOUR, "pt-new")
    conn = MagicMock(fetchval=AsyncMock(side_effect=OSError("closed")))
    assert asyncio.run(handler._source_fanout_master(conn, TOUR, "pt-new")) is None


def test_every_hook_is_wired_on_its_write_path():
    """A text guard: each source write path names its typed reason (the hook cannot be dropped silently)."""
    wired = {"api/routers/admin_pipeline.py": ["raw_tour_edited"],
             "services/export/handler.py": ["master_published"],
             "api/routers/admin_atoms.py": ["atom_deleted", "atom_edited"],
             "api/routers/admin_content_quality.py": ["place_card_facts_edited", "place_card_status_changed",
                                                      "place_cards_merged"]}
    for path, reasons in wired.items():
        text = open(path, encoding="utf-8").read()
        assert "source_fanout" in text, path
        for reason in reasons:
            assert f'"{reason}"' in text, (path, reason)


# ── the admin routes ────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_tour_fanout_read_and_its_503_before_the_migration():
    import asyncpg
    from fastapi import HTTPException

    from api.routers import admin_quality_lineage as lin
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    out = asyncio.run(lin.get_tour_fanout(_req(_pool_with(conn)), TOUR, limit=10))
    assert out["tour_id"] == TOUR and out["open"] == 0 and conn.fetch.await_args.args[1:] == (TOUR, 10)
    conn = MagicMock(fetch=AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("missing")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(lin.get_tour_fanout(_req(_pool_with(conn)), TOUR, limit=10))
    assert exc.value.status_code == 503 and "392" in exc.value.detail


def test_the_recheck_route_queues_a_dry_run_by_default_and_404s_an_unknown_tour():
    from fastapi import HTTPException

    from api.routers import admin_quality_lineage as lin
    pool = MagicMock(fetchval=AsyncMock(side_effect=[True, NOW]))
    with patch("shared.jobs.queue.enqueue", AsyncMock(return_value=("j7", True))) as enq:
        out = asyncio.run(lin.post_tour_recheck(_req(pool), TOUR, None, x_admin_user_id="u1"))
    assert out == {"job_id": "j7", "created": True, "kind": "source_fanout", "tour_id": TOUR,
                   "source_kind": "raw_tour", "dry_run": True}
    payload = enq.await_args.args[2]
    assert payload["reason"] == "manual_recheck" and payload["dry_run"] is True and payload["tour_id"] == TOUR
    assert enq.await_args.kwargs["created_by"] == "admin:u1"
    pool = MagicMock(fetchval=AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(lin.post_tour_recheck(_req(pool), TOUR, lin.RecheckBody(source_kind="master", dry_run=False),
                                          x_admin_user_id=None))
    assert exc.value.status_code == 404
