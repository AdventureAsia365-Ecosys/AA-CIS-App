"""AA-671 S257 — photo -> destination of its own tour, unit level (synthetic names only).

- match.resolve_destination: rules in order (exact, core, name in label, label in name), the first rule that names a
  place decides, a tie is `ambiguous` (never the first one listed), generic labels never match by core / containment,
  order-independent; a label found only inside one place name is `weak` (a suggestion, never a destination): a town
  label is not its landmark (review S257).
- photo_destination_match job: DRY RUN by default (writes nothing), counts every outcome per country with samples,
  one guarded UPDATE on apply (marked 'auto'), weak answers never written, covers filled only on request and
  previewed on every run, stops while a photo_sync runs, bad payloads are non-retryable.
- POST /admin/photos/destination-match: a PHOTOS_WRITE policy route declared before /{photo_id}/assign, DRY RUN by
  default, one job at a time (check + insert under one advisory lock).
- POST /admin/photos/{id}/assign: a folder-wide tour change empties the automatic destinations of the rows whose tour
  changes; a destination given by hand is marked 'manual'.
The SQL runs on real Postgres in tests/integration/test_aa671_photo_destination_match.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core.staff_authz import PHOTOS_WRITE, POLICY
from api.routers import admin_photos
from services.jobs import photo_destination_match_job as J
from services.photos import match as M
from shared.jobs.registry import NonRetryable, get_kind


# ── resolve_destination ──────────────────────────────────────────────────────────────────────────────────────────────

def _r(label, cands):
    return M.resolve_destination(label, cands)


@pytest.mark.parametrize("label,cands,dest,rule", [
    ("Zorvan Island", [("d1", "Zorvan Island"), ("d2", "Zorvan"), ("d3", "Lake Quellin")], "d1", M.RULE_EXACT),
    ("Hội Ân Plaza", [("d1", "Hoi An Plaza"), ("d2", "Other Place")], "d1", M.RULE_EXACT),        # accents folded
    ("The Brass Gate", [("d1", "Brass Gate")], "d1", M.RULE_EXACT),                              # leading article
    ("Flaming Rocks", [("d1", "Zarbag (Flaming Rocks)")], "d1", M.RULE_EXACT),                   # parenthetical form
    ("Quellin", [("d1", "Zorvan Island"), ("d3", "Lake Quellin")], "d3", M.RULE_CORE),           # kind word dropped
    ("Quellin Lake", [("d3", "Lake Quellin")], "d3", M.RULE_CORE),
    ("Zorvan Island sunset", [("d1", "Zorvan Island"), ("d2", "Zorvan")], "d1", M.RULE_NAME_IN_LABEL),  # longest
    ("Morning at Brass Gate", [("d1", "Brass Gate"), ("d2", "Zorvan")], "d1", M.RULE_NAME_IN_LABEL),
])
def test_matched_by_the_first_rule_that_names_a_place(label, cands, dest, rule):
    got = _r(label, cands)
    assert (got.outcome, got.destination_id, got.rule) == (M.MATCHED, dest, rule)


@pytest.mark.parametrize("label,cands,suggested", [
    # a town label against the landmarks named after the town: the landmark is a different place
    ("Paro", [("tn", "Paro Taktsang"), ("pd", "Punakha Dzong")], "tn"),
    ("Thimphu", [("td", "Thimphu Dzong"), ("bb", "Buddha Dordenma")], "td"),
    ("Bangkok", [("wa", "Wat Arun Bangkok"), ("gp", "Grand Palace")], "wa"),
    ("Taktsor", [("p", "Paro Taktsor"), ("q", "Paro")], "p"),
])
def test_a_label_found_only_inside_a_place_name_is_weak_never_a_destination(label, cands, suggested):
    got = _r(label, cands)
    assert (got.outcome, got.destination_id, got.rule, got.suggestion) == (M.WEAK, None, M.RULE_LABEL_IN_NAME,
                                                                         suggested)
    assert M.match_destination(label, cands) is None


def test_a_town_label_matches_the_town_itself_when_the_tour_has_it():
    got = _r("Paro", [("tn", "Paro Taktsang"), ("town", "Paro"), ("pd", "Punakha Dzong")])
    assert (got.outcome, got.destination_id, got.rule) == (M.MATCHED, "town", M.RULE_EXACT)


def test_the_itinerary_spelling_counts_as_a_name_of_the_destination():
    got = _r("Tiger Nest Rock", [("d1", "Paro Taktsor"), ("d1", "Tiger Nest Rock"), ("d2", "Paro")])
    assert got.destination_id == "d1" and got.rule == M.RULE_EXACT


@pytest.mark.parametrize("label,cands,rule,tied", [
    ("Night Bazaar", [("a", "Night Bazaar"), ("b", "night bazaar")], M.RULE_EXACT, ("a", "b")),
    ("Zorvan Island", [("b", "Zorvan Island"), ("a", "Zorvan Island")], M.RULE_EXACT, ("a", "b")),
    ("Brass Gate and Stone Gate", [("a", "Brass Gate"), ("b", "Stone Gate")], M.RULE_NAME_IN_LABEL, ("a", "b")),
    # two separate places in one label: the longer name does not win over a name that is not nested in it
    ("Brass Gate and Iron Gate", [("a", "Brass Gate"), ("b", "Iron Gate")], M.RULE_NAME_IN_LABEL, ("a", "b")),
    ("Taktsor", [("a", "Paro Taktsor"), ("b", "Taktsor Hill")], M.RULE_LABEL_IN_NAME, ("a", "b")),
])
def test_a_tie_is_ambiguous_never_the_first_one_listed(label, cands, rule, tied):
    got = _r(label, cands)
    assert (got.outcome, got.destination_id, got.rule, got.candidates) == (M.AMBIGUOUS, None, rule, tied)
    assert _r(label, list(reversed(cands))) == got                               # order-independent


def test_a_stronger_rule_decides_before_a_weaker_one_is_tried():
    # exact names one place; the other place's name inside the label does not make it ambiguous
    got = _r("Zorvan Island", [("d1", "Zorvan Island"), ("d2", "Zorvan")])
    assert got.outcome == M.MATCHED and got.destination_id == "d1" and got.rule == M.RULE_EXACT


def test_generic_labels_never_match_by_core_or_containment():
    assert _r("Temple", [("a", "Zorvan Temple")]).outcome == M.NO_MATCH
    assert _r("Night Market", [("a", "Zorvan Night Market Street")]).outcome == M.NO_MATCH
    assert _r("Night Market", [("a", "Night Market (Zorvan)")]).destination_id == "a"   # but exact still works


@pytest.mark.parametrize("label,cands,outcome", [
    ("x", [("a", "Zorvan")], M.NO_LABEL),
    ("", [("a", "Zorvan")], M.NO_LABEL),
    (None, [("a", "Zorvan")], M.NO_LABEL),
    ("Zorvan", [], M.NO_CANDIDATE),
    ("Zorvan", [("a", None), (None, "Zorvan"), ("b", "  ")], M.NO_CANDIDATE),
    ("Quellin", [("a", "Zorvan Island")], M.NO_MATCH),
    ("Img 2041", [("a", "Zorvan Island")], M.NO_MATCH),
])
def test_why_nothing_matched(label, cands, outcome):
    got = _r(label, cands)
    assert got.outcome == outcome and got.destination_id is None


def test_short_names_do_not_match_inside_a_label():
    assert _r("Abc Zorvan", [("a", "Abc")]).outcome == M.NO_MATCH                 # under MIN_KEY


# ── plan() and the job ───────────────────────────────────────────────────────────────────────────────────────────────

T1 = "aaaaaaaa-0000-4000-8000-00000000a001"
T2 = "aaaaaaaa-0000-4000-8000-00000000a002"


def _photo(n, label, tour=T1, country="Zz Bhutan", source="auto"):
    return {"id": f"bbbbbbbb-0000-4000-8000-00000000b{n:03d}", "country": country, "tour_id": tour,
            "place_label": label, "match_source": source, "destination_id": None}


CANDS = {T1: [("d-gate", "Brass Gate"), ("d-isle", "Zorvan Island"), ("d-m1", "Night Bazaar"),
              ("d-m2", "Night Bazaar")]}


def test_plan_counts_every_outcome_per_country_with_samples():
    rows = [_photo(1, "Brass Gate (2)"), _photo(2, "Zorvan Island sunset", source="manual"), _photo(3, "Night Bazaar"),
            _photo(4, "Quellin"), _photo(5, "Brass Gate", tour=T2, country="Zz Taiwan"), _photo(6, "x"),
            _photo(7, "Zorvan")]
    out = J.plan(rows, CANDS)
    assert out["outcomes"] == {"matched": 2, "ambiguous": 1, "weak": 1, "no_match": 1, "no_candidate": 1,
                               "no_label": 1}
    assert out["rules"] == {M.RULE_EXACT: 1, M.RULE_NAME_IN_LABEL: 1}
    assert out["writes"] == [(rows[0]["id"], "d-gate", T1, M.RULE_EXACT),
                             (rows[1]["id"], "d-isle", T1, M.RULE_NAME_IN_LABEL)]
    assert out["by_country"]["Zz Taiwan"] == {"matched": 0, "ambiguous": 0, "weak": 0, "no_match": 0,
                                              "no_candidate": 1, "no_label": 0}
    weak, = out["samples"]["weak"]                               # "Zorvan" inside "Zorvan Island": never written
    assert weak["photo_id"] == rows[6]["id"] and weak["suggestion"] == {"destination_id": "d-isle",
                                                                        "name": "Zorvan Island"}
    assert rows[6]["id"] not in {w[0] for w in out["writes"]}
    amb, = out["ambiguous"]
    assert [c["destination_id"] for c in amb["candidates"]] == ["d-m1", "d-m2"]
    assert out["samples"]["matched"][1]["destination"] == "Zorvan Island"
    assert out["samples"]["matched"][1]["match_source"] == "manual"
    assert out["samples"]["no_match"][0]["tour_places"] == ["Brass Gate", "Night Bazaar", "Zorvan Island"]


def test_plan_samples_are_capped():
    rows = [_photo(n, "Quellin") for n in range(J.MAX_SAMPLES + 5)]
    out = J.plan(rows, CANDS)
    assert out["outcomes"]["no_match"] == J.MAX_SAMPLES + 5 and len(out["samples"]["no_match"]) == J.MAX_SAMPLES


class _JobPool:
    """Plays the job's queries over in-memory place_photo rows."""

    def __init__(self, rows, cands, sync_running=False, apply_result=None, covers=None):
        self.rows, self.cands, self.sync_running, self.apply_result = rows, cands, sync_running, apply_result
        self.applied, self.cover_fills, self.preview_args = [], 0, None
        self.covers = covers or {}         # destination id -> cover url (absent = no destination row)

    def _open(self, country):
        return [r for r in self.rows if r["destination_id"] is None and (country is None or r["country"] == country)]

    async def fetchval(self, sql, *args):
        if sql == J.SYNC_RUNNING_SQL:
            return 1 if self.sync_running else None
        assert sql == J.OPEN_COUNT_SQL
        return len(self._open(args[0]))

    async def fetchrow(self, sql, *args):
        if sql == J.COVERS_PREVIEW_SQL:
            self.preview_args = args
            base, prefix, planned = args
            served = {r["destination_id"] for r in self.rows if r["destination_id"]}
            new = [d for d in planned if d in self.covers and d not in served]
            return {"pending_now": 0, "new_empty": sum(self.covers[d] is None for d in new),
                    "new_job_owned": sum(bool(self.covers[d]) and self.covers[d].startswith(prefix) for d in new)}
        assert sql == J.SERVED_SQL
        with_dest = [r for r in self.rows if r["destination_id"]]
        return {"photos": len(with_dest), "destinations": len({r["destination_id"] for r in with_dest})}

    async def fetch(self, sql, *args):
        if sql == J.APPLY_SQL:
            return self._apply(*args)
        if sql == J.OPEN_ROWS_SQL:
            country, limit = args
            return sorted(self._open(country), key=lambda r: (r["country"], r["id"]))[:limit]
        assert "geo.tour_place" in sql
        return [{"tour_id": t, "destination_id": d, "destination_name": n, "place_name": None}
                for t, pairs in self.cands.items() if t in args[0] for d, n in pairs]

    def acquire(self):
        return _Acquired(self)

    def transaction(self):
        return _Acquired(self)

    async def execute(self, sql, *args):
        if "set_config('app.tenant_id'" in sql:
            self.pinned = args
            return "SELECT 1"
        if "shared.destinations d SET cover_image_url" in sql:
            self.cover_fills += 1
            return "UPDATE 1"
        raise AssertionError(sql)

    def _apply(self, ids, dests, tours):
        self.applied.append(list(zip(ids, dests, tours)))
        out = []
        for pid, dest, tour in zip(ids, dests, tours):
            row = next(r for r in self.rows if r["id"] == pid)
            if row["destination_id"] is None and row["tour_id"] == tour:
                if self.apply_result is not None and len(out) >= self.apply_result:
                    continue                    # changed by someone else meanwhile
                row["destination_id"] = dest
                out.append({"id": pid})
        return out


class _Acquired:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self.pool

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    job_id = "job-under-test"

    def __init__(self, pool, payload):
        self.pool, self.payload, self.result, self.progressed = pool, payload, None, []

    async def progress(self, **f):
        self.progressed.append(f)

    def set_result(self, r):
        self.result = r


def _run(pool, payload=None):
    ctx = _Ctx(pool, payload if payload is not None else {})
    return asyncio.run(J.run(ctx)), ctx


def _rows():
    return [_photo(1, "Brass Gate"), _photo(2, "Zorvan Island"), _photo(3, "Night Bazaar"), _photo(4, "Quellin")]


def test_kind_is_a_registered_background_kind():
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = get_kind(J.KIND)
    assert k is not None and k.background and k.concurrency == 1 and not k.deploy_blocking


def test_dry_run_is_the_default_and_writes_nothing():
    pool = _JobPool(_rows(), CANDS)
    res, ctx = _run(pool)
    assert res["dry_run"] is True and pool.applied == [] and res["written"] == 0 and res["covers_set"] == 0
    assert res["fill_covers"] is False and pool.cover_fills == 0
    assert pool.pinned == ("00000000-0000-0000-0000-000000000001",)          # raw_tours read as the AA master
    assert res["outcomes"]["matched"] == 2 and res["outcomes"]["ambiguous"] == 1 and res["outcomes"]["no_match"] == 1
    assert res["open_before"] == res["open_after"] == 4 and res["served_before"] == res["served_after"]
    assert "writes" not in res and "ambiguous" not in res                   # the full lists stay out of the result
    assert ctx.result == res and ctx.progressed[-1]["phase"] == "done"


def test_apply_writes_only_unambiguous_matches_and_is_idempotent():
    from structlog.testing import capture_logs
    pool = _JobPool(_rows(), CANDS)
    with capture_logs() as logs:
        res, _ = _run(pool, {"dry_run": False})
    assert res["written"] == 2 and res["changed_meanwhile"] == 0
    assert res["covers_set"] == 0 and pool.cover_fills == 0              # covers only on request (TripPlanner reads)
    assert res["served_before"] == {"photos": 0, "destinations": 0}
    assert res["served_after"] == {"photos": 2, "destinations": 2} and res["open_after"] == 2
    assert [r["destination_id"] for r in pool.rows] == ["d-gate", "d-isle", None, None]
    matched = [e for e in logs if e["event"] == "photo_destination_matched"]
    assert len(matched) == 2 and {e["rule"] for e in matched} == {M.RULE_EXACT}
    assert [e for e in logs if e["event"] == "photo_destination_ambiguous"][0]["photo_id"] == pool.rows[2]["id"]
    again, _ = _run(pool, {"dry_run": False})
    assert again["planned"] == 2 and again["written"] == 0 and len(pool.applied) == 1     # nothing left to write


def test_apply_marks_its_writes_auto_and_counts_manual_rows():
    rows = _rows()
    rows[1]["match_source"] = "manual"                    # tour set by hand, destination empty
    pool = _JobPool(rows, CANDS)
    res, _ = _run(pool, {"dry_run": False})
    assert res["written"] == 2 and res["written_manual_rows"] == 1
    assert "destination_match_source = 'auto'" in J.APPLY_SQL     # provenance: never mistaken for a person's choice


def test_covers_are_previewed_on_a_dry_run_and_filled_only_on_request():
    covers = {"d-gate": None, "d-isle": "https://cdn.example/content/photos/old?size=large"}
    pool = _JobPool(_rows(), CANDS, covers=covers)
    res, _ = _run(pool)
    assert res["covers_preview"]["new_empty"] == 1 and pool.cover_fills == 0
    assert pool.preview_args[2] == ["d-gate", "d-isle"] and pool.preview_args[1].endswith("/content/photos/")
    res, _ = _run(_JobPool(_rows(), CANDS), {"dry_run": False, "fill_covers": True})
    assert res["covers_set"] == 1
    pool = _JobPool(_rows(), CANDS)
    res, _ = _run(pool, {"dry_run": True, "fill_covers": True})
    assert res["covers_set"] == 0 and pool.cover_fills == 0           # a dry run never writes covers


def test_a_row_changed_meanwhile_is_counted_not_written():
    from structlog.testing import capture_logs
    pool = _JobPool(_rows(), CANDS, apply_result=1)
    with capture_logs() as logs:
        res, _ = _run(pool, {"dry_run": False})
    assert res["written"] == 1 and res["changed_meanwhile"] == 1
    assert len([e for e in logs if e["event"] == "photo_destination_matched"]) == 1      # only what was written


def test_apply_stops_while_a_photo_sync_runs_but_a_dry_run_does_not():
    pool = _JobPool(_rows(), CANDS, sync_running=True)
    res, _ = _run(pool, {"dry_run": False})
    assert res["stopped"] == "photo_sync_running" and pool.applied == []
    res, _ = _run(pool)
    assert res["stopped"] is None and res["outcomes"]["matched"] == 2


def test_country_and_limit_narrow_the_run():
    rows = _rows() + [_photo(9, "Brass Gate", country="Zz Taiwan")]
    res, _ = _run(_JobPool(rows, CANDS), {"country": "Zz Taiwan"})
    assert res["planned"] == 1 and res["open_before"] == 1
    res, _ = _run(_JobPool(rows, CANDS), {"limit": 2})
    assert res["planned"] == 2 and res["open_before"] == 5


@pytest.mark.parametrize("payload", [{"limit": 0}, {"limit": J.MAX_LIMIT + 1}, {"limit": "50"}, {"limit": True},
                                     {"country": 5}, {"dry_run": "false"}, {"dry_run": 0},
                                     {"fill_covers": "yes"}])
def test_bad_payload_is_non_retryable(payload):
    with pytest.raises(NonRetryable):
        _run(_JobPool([], {}), payload)


# ── the admin enqueue route ──────────────────────────────────────────────────────────────────────────────────────────

def test_enqueue_route_is_a_policy_write_declared_before_the_catch_all():
    assert POLICY[("POST", "/admin/photos/destination-match")] == PHOTOS_WRITE
    paths = [r.path for r in admin_photos.router.routes]
    assert paths.index("/admin/photos/destination-match") < paths.index("/admin/photos/{photo_id}/assign")


class _EnqPool:
    """Pool and connection at once: the route takes a connection and holds an advisory lock in a transaction."""

    def __init__(self, active):
        self.active, self.locked, self.in_tx = active, [], False

    def acquire(self):
        return _Acquired(self)

    def transaction(self):
        pool = self

        class _Tx:
            async def __aenter__(self):
                pool.in_tx = True

            async def __aexit__(self, *exc):
                pool.in_tx = False
                return False
        return _Tx()

    async def execute(self, sql, *args):
        assert "pg_advisory_xact_lock" in sql and self.in_tx
        self.locked.append(args)

    async def fetchrow(self, sql, *args):
        assert args == (J.KIND,) and self.locked and self.in_tx
        return self.active


def _enqueue(monkeypatch, body, active=None):
    seen = {}

    async def fake_enqueue(pool, kind, payload, **kw):
        assert pool.in_tx and pool.locked == [(J.KIND,)]                # inserted under the same lock
        seen.update(kind=kind, payload=payload, **kw)
        return "job-1", True
    monkeypatch.setattr(admin_photos, "verify_admin_secret", lambda s: None)
    monkeypatch.setattr("shared.jobs.registry.enqueue", fake_enqueue)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_EnqPool(active))))
    return asyncio.run(admin_photos.destination_match(body, req, "secret", "staff-1")), seen


def test_enqueue_is_a_dry_run_by_default(monkeypatch):
    out, seen = _enqueue(monkeypatch, admin_photos.DestinationMatchRequest())
    assert out == {"job_id": "job-1", "created": True, "status": "queued", "dry_run": True}
    assert seen["kind"] == J.KIND and seen["created_by"] == "admin:staff-1"
    assert seen["payload"] == {"dry_run": True, "country": None, "fill_covers": False}


def test_enqueue_apply_with_country_and_limit(monkeypatch):
    out, seen = _enqueue(monkeypatch, admin_photos.DestinationMatchRequest(dry_run=False, country="Bhutan", limit=50))
    assert out["dry_run"] is False
    assert seen["payload"] == {"dry_run": False, "country": "Bhutan", "fill_covers": False, "limit": 50}


def test_enqueue_returns_the_active_job(monkeypatch):
    out, seen = _enqueue(monkeypatch, admin_photos.DestinationMatchRequest(),
                         active={"id": "job-0", "status": "running"})
    assert out == {"job_id": "job-0", "created": False, "status": "running"} and seen == {}


def test_enqueue_refuses_a_bad_limit(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _enqueue(monkeypatch, admin_photos.DestinationMatchRequest(limit=J.MAX_LIMIT + 1))
    assert e.value.status_code == 422


# ── the assign route: folder-wide tour change ────────────────────────────────────────────────────────────────────────

def test_folder_tour_change_empties_automatic_destinations_only_where_the_tour_changes():
    sql = " ".join(admin_photos.FOLDER_ASSIGN_SQL.split())
    rule = ("CASE WHEN tour_id IS DISTINCT FROM $3::uuid AND destination_match_source IS DISTINCT FROM 'manual' "
            "THEN NULL ELSE")
    assert f"destination_id = {rule} destination_id END" in sql
    assert f"destination_match_source = {rule} destination_match_source END" in sql
    assert "status <> 'rejected'" in sql
