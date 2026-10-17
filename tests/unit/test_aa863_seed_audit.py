"""AA-863 — admin seed view: the seed atom's place / country next to each Subject and T9 piece, and
the mismatch flags (place under subject_seed.match_grade, country vs the Segment's country)."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import admin_content_quality as cq
from api.routers import admin_dashboard
from services.acp_shared import seed_audit
from services.acp_shared.seed_audit import load_piece_seeds, load_subject_seeds, seed_check
from services.acp_shared.subject_seed import Member, choose_route_anchor

SECRET = "s3cret"
TENANT = str(uuid.uuid4())
OTHER_TENANT = str(uuid.uuid4())
T_KR, T_PK, T_BT = "tour-kr", "tour-pk", "tour-bt"

SEED_KEYS = {"atom_id", "place", "country", "source", "skip_reason", "subject_kind", "segment_id",
             "segment_label", "subject_place", "segment_countries", "match", "place_mismatch",
             "country_mismatch", "mismatch"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


def _m(atom_id, place, country, tour_id=T_KR, evidence=None, card_id=None):
    return Member(atom_id=atom_id, tour_id=tour_id, place=place, country=country, evidence=evidence,
                  card_id=card_id)


# ── seed_check (pure) ──────────────────────────────────────────────────────────────────────────

def test_exact_place_in_the_segment_country_is_not_flagged():
    out = seed_check("Gyeongju", _m("a1", "Gyeongju", "South Korea"), segment_countries=["South Korea"])
    assert out["match"] == "exact"
    assert (out["place_mismatch"], out["country_mismatch"], out["mismatch"]) == (False, False, False)


def test_s239_gyeongju_written_from_a_pakistan_museum_is_flagged_twice():
    """The S239 case: the Subject "Gyeongju" seeded by "Lok Virsa Museum" in a mixed Segment."""
    out = seed_check("Gyeongju", _m("z1", "Lok Virsa Museum", "Pakistan", T_PK),
                     segment_countries=["South Korea", "Pakistan"])
    assert out["match"] is None and out["place_mismatch"] is True
    assert out["country_mismatch"] is True and out["mismatch"] is True
    assert out["segment_countries"] == ["Pakistan", "South Korea"]


def test_place_match_uses_match_grade_tiers_including_the_loose_tier():
    # GRADE_TOKENS: choose_representative accepts it when nothing stronger matched, so it is no mismatch.
    out = seed_check("Bukchon", _m("a1", "Bukchon Hanok Village", "South Korea"),
                     segment_countries=["South Korea"])
    assert out["match"] == "loose_words" and out["place_mismatch"] is False
    out = seed_check("Paro", _m("a2", "Taktsang Monastery (Paro)", "Bhutan", T_BT), segment_countries=["Bhutan"])
    assert out["match"] == "parenthetical" and out["place_mismatch"] is False


def test_place_match_is_alias_card_aware():
    aliases = {("national museum of bhutan", "bhutan"): "card-1", ("ta dzong", "bhutan"): "card-1"}
    seed = _m("a1", "Ta Dzong", "Bhutan", T_BT)
    assert seed_check("National Museum of Bhutan", seed, segment_countries=["Bhutan"])["place_mismatch"] is True
    out = seed_check("National Museum of Bhutan", seed, segment_countries=["Bhutan"], alias_cards=aliases)
    assert out["match"] == "core_or_card" and out["place_mismatch"] is False


def test_country_differs_from_a_single_country_segment():
    out = seed_check("Vientiane", _m("a1", "Vientiane", "Laos"), segment_countries=["Thailand"])
    assert out["place_mismatch"] is False and out["country_mismatch"] is True and out["mismatch"] is True


def test_country_compare_is_accent_and_case_folded():
    out = seed_check("Hanoi", _m("a1", "Hanoi", "Việt Nam"), segment_countries=["viet nam", "VIET NAM", None, ""])
    assert out["country_mismatch"] is False
    assert out["segment_countries"] == ["viet nam"]


def test_unknown_country_is_not_flagged():
    assert seed_check("Hanoi", _m("a1", "Hanoi", None), segment_countries=["Vietnam"])["country_mismatch"] is False
    assert seed_check("Hanoi", _m("a1", "Hanoi", "Vietnam"), segment_countries=[None])["country_mismatch"] is False


def test_route_is_country_checked_only():
    out = seed_check(None, _m("a1", "Somewhere else", "Bhutan", T_BT), segment_countries=["Bhutan"], route=True)
    assert out["match"] is None and out["place_mismatch"] is False and out["mismatch"] is False
    out = seed_check("Paro", _m("a1", "Lok Virsa Museum", "Pakistan", T_PK), segment_countries=["Bhutan"],
                     route=True)
    assert out["place_mismatch"] is False and out["country_mismatch"] is True


def test_no_seed_is_not_flagged():
    out = seed_check("Gyeongju", None, segment_countries=["South Korea", "Pakistan"])
    assert out["mismatch"] is False and out["place_mismatch"] is False and out["country_mismatch"] is False


def test_choose_route_anchor_prefers_the_place_then_falls_back_to_the_route_tour():
    members = [_m("a2", "Paro Town", "Bhutan", T_BT), _m("a1", "Punakha", "Bhutan", T_BT, evidence="e"),
               _m("a0", "Paro", "Bhutan", "elsewhere")]
    assert choose_route_anchor("Paro", members, T_BT).atom_id == "a2"
    assert choose_route_anchor("Thimphu", members, T_BT).atom_id == "a1"  # evidence first
    assert choose_route_anchor("Paro", members, "no-such-tour") is None


# ── batched reads (fake connection, dispatched on the SQL) ──────────────────────────────────────

class FakeConn:
    def __init__(self, **results):
        self.results = results
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        for key, marker in (("pieces", "FROM acp_shared.content_piece cp"),
                            ("requests", "DISTINCT ON (agr.subject_id)"),
                            ("aliases_chain", "WITH RECURSIVE chain"),
                            ("facts", "LEFT JOIN LATERAL"),
                            ("members", "m.segment_id, ta.atom_id"),
                            ("tours", "tenant_tour_versions"),
                            ("cards", "place_card_alias")):
            if marker in sql:
                self.calls.append((key, args))
                res = self.results.get(key, [])
                if isinstance(res, Exception):
                    raise res
                return res(args) if callable(res) else res
        raise AssertionError(f"unexpected query: {sql[:80]}")

    def count(self, key):
        return sum(1 for k, _ in self.calls if k == key)


def _piece_row(pid, atom_id, place, country, segment_id=None, route_id=None, tenant=TENANT, **kw):
    return {"piece_id": pid, "tenant_id": tenant, "atom_id": atom_id, "seed_tour_id": kw.get("tour", T_KR),
            "seed_place": place, "seed_evidence": None, "seed_country": country, "seed_card_id": None,
            "segment_id": segment_id, "route_id": route_id, "hub_name": kw.get("hub_name"),
            "route_country": kw.get("route_country")}


@pytest.mark.asyncio
async def test_load_piece_seeds_is_set_based_and_shapes_every_row():
    rows = [
        _piece_row("p1", "a1", "Gyeongju", "South Korea", segment_id="seg-old"),
        _piece_row("p2", "z1", "Lok Virsa Museum", "Pakistan", segment_id="seg-old", tour=T_PK),
        _piece_row("p3", "r1", "Paro Town", "Bhutan", route_id="route-1", hub_name="Paro Valley Loop",
                   route_country="Bhutan", tour=T_BT),
        _piece_row("p4", "gone", None, None, segment_id="seg-live"),
    ]
    rows[3]["seed_tour_id"] = None  # the atom row no longer exists
    conn = FakeConn(
        pieces=rows,
        aliases_chain=[{"start_id": "seg-old", "segment_id": "seg-live"}],
        facts=[{"segment_id": "seg-live", "canonical_place": "Gyeongju", "canonical_action": "visit",
                "owner_scope": "platform", "countries": ["South Korea"]},
               # another tenant's own atoms never count toward this tenant's Segment country
               {"segment_id": "seg-live", "canonical_place": "Gyeongju", "canonical_action": "visit",
                "owner_scope": OTHER_TENANT, "countries": ["Japan"]}],
    )
    out = await load_piece_seeds(conn, ["p1", "p2", "p3", "p4", "p1"])
    assert [conn.count(k) for k in ("pieces", "aliases_chain", "facts", "cards")] == [1, 1, 1, 1]
    assert conn.calls[0][1] == (["p1", "p2", "p3", "p4"],)
    assert set(out) == {"p1", "p2", "p3", "p4"} and all(set(v) == SEED_KEYS for v in out.values())

    p1, p2, p3, p4 = (out[k] for k in ("p1", "p2", "p3", "p4"))
    assert (p1["place"], p1["country"], p1["source"]) == ("Gyeongju", "South Korea", "request")
    assert p1["segment_id"] == "seg-live" and p1["segment_label"] == "Gyeongju — visit"
    assert p1["segment_countries"] == ["South Korea"] and p1["mismatch"] is False
    assert p2["place_mismatch"] is True and p2["country_mismatch"] is True
    assert p3["subject_kind"] == "route" and p3["segment_label"] == "Paro Valley Loop" and p3["mismatch"] is False
    assert p4["atom_id"] == "gone" and p4["place"] is None and p4["skip_reason"] == "seed_atom_missing"
    assert p4["mismatch"] is False


@pytest.mark.asyncio
async def test_load_piece_seeds_without_a_subject_shows_the_seed_only():
    conn = FakeConn(pieces=[_piece_row("p1", "a1", "Hanoi", "Vietnam")])
    out = await load_piece_seeds(conn, ["p1"])
    assert conn.count("facts") == 0
    assert out["p1"]["subject_kind"] is None and out["p1"]["place"] == "Hanoi" and out["p1"]["mismatch"] is False


@pytest.mark.asyncio
async def test_load_piece_seeds_is_fail_soft_and_skips_empty_input():
    assert await load_piece_seeds(FakeConn(pieces=RuntimeError("db down")), ["p1"]) == {}
    conn = FakeConn()
    assert await load_piece_seeds(conn, []) == {} and conn.calls == []


def _subject(sid, **kw):
    base = {"subject_id": sid, "segment_id": None, "canonical_place": None, "canonical_action": None,
            "route_id": None, "hub_name": None, "route_tour_id": None, "route_first_segment_id": None,
            "route_first_place": None, "route_country": None}
    return {**base, **kw}


def _member_row(segment_id, atom_id, tour_id, place, country, evidence=None):
    return {"segment_id": segment_id, "atom_id": atom_id, "tour_id": tour_id, "place": place,
            "evidence": evidence, "country": country, "card_id": None}


@pytest.mark.asyncio
async def test_load_subject_seeds_frozen_then_computed_then_skipped():
    s_picked, s_proposed, s_noseed, s_route = (str(uuid.uuid4()) for _ in range(4))
    subjects = [
        _subject(s_picked, segment_id="seg-g", canonical_place="Gyeongju", canonical_action="visit"),
        _subject(s_proposed, segment_id="seg-g", canonical_place="Gyeongju", canonical_action="visit"),
        _subject(s_noseed, segment_id="seg-u", canonical_place="Uran Togoo", canonical_action="hike"),
        _subject(s_route, route_id="route-1", hub_name="Paro Valley Loop", route_tour_id=T_BT,
                 route_first_segment_id="seg-p", route_first_place="Paro", route_country="Bhutan"),
    ]
    conn = FakeConn(
        requests=[{"subject_id": s_picked, "atom_id": "z1", "seed_tour_id": T_PK, "seed_place": "Lok Virsa Museum",
                   "seed_evidence": None, "seed_country": "Pakistan", "seed_card_id": None}],
        tours=[{"tour_id": T_KR}, {"tour_id": T_PK}, {"tour_id": T_BT}],
        members=[_member_row("seg-g", "a1", T_KR, "Gyeongju", "South Korea"),
                 _member_row("seg-g", "z1", T_PK, "Lok Virsa Museum", "Pakistan"),
                 _member_row("seg-u", "u1", T_KR, "Seoul", "South Korea"),
                 _member_row("seg-p", "p1", T_BT, "Paro Town", "Bhutan")],
    )
    out = await load_subject_seeds(conn, TENANT, subjects)
    assert [conn.count(k) for k in ("requests", "tours", "members", "cards")] == [1, 1, 1, 1]
    assert conn.calls[0][1][1] == TENANT  # requests are read for this tenant only
    assert all(set(v) == SEED_KEYS for v in out.values())

    picked = out[s_picked]  # the frozen seed of a pre-AA-857 pick: flagged
    assert (picked["source"], picked["atom_id"], picked["place"]) == ("request", "z1", "Lok Virsa Museum")
    assert picked["place_mismatch"] is True and picked["country_mismatch"] is True
    assert picked["segment_countries"] == ["Pakistan", "South Korea"]

    proposed = out[s_proposed]  # what a pick would use now (choose_representative)
    assert (proposed["source"], proposed["atom_id"], proposed["country"]) == ("computed", "a1", "South Korea")
    assert proposed["place_mismatch"] is False and proposed["country_mismatch"] is True  # mixed Segment

    noseed = out[s_noseed]
    assert noseed["atom_id"] is None and noseed["skip_reason"] == "no_member_matches_subject_place"
    assert noseed["mismatch"] is False

    route = out[s_route]
    assert (route["subject_kind"], route["atom_id"], route["segment_label"]) == ("route", "p1", "Paro Valley Loop")
    assert route["mismatch"] is False


@pytest.mark.asyncio
async def test_load_subject_seeds_is_fail_soft():
    conn = FakeConn(requests=RuntimeError("db down"))
    assert await load_subject_seeds(conn, TENANT, [_subject("s1", segment_id="x", canonical_place="X")]) == {}


# ── endpoints carry `seed` on every row ─────────────────────────────────────────────────────────

def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


@pytest.mark.asyncio
async def test_list_pieces_attaches_the_seed_of_each_row(monkeypatch):
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    seed = {"atom_id": "a1", "place": "Gyeongju", "country": "South Korea", "mismatch": False}
    loader = AsyncMock(return_value={str(p1): seed})
    monkeypatch.setattr(cq.seed_audit, "load_piece_seeds", loader)
    conn = AsyncMock()
    conn.fetchval.side_effect = [False, 2]
    conn.fetch.return_value = [{"piece_id": p1, "failed_gates": "[]", "flag_gates": "[]"},
                               {"piece_id": p2, "failed_gates": "[]", "flag_gates": "[]"}]
    out = await cq.list_pieces(_request(conn), limit=50, offset=0, tenant_id=None, channel=None, status=None,
                               gate=None, q=None, label=None, sort="created_at", sort_dir="desc",
                               x_admin_secret=SECRET)
    loader.assert_awaited_once()
    assert loader.await_args.args[1] == [p1, p2]
    assert out["data"][0]["seed"] == seed and out["data"][1]["seed"] is None


@pytest.mark.asyncio
async def test_list_slate_passes_live_ids_and_attaches_the_seed(monkeypatch):
    sid = uuid.uuid4()
    monkeypatch.setattr(admin_dashboard, "live_segment_labels",
                        AsyncMock(return_value={"old1": ("live1", "Delhi", "explore")}))
    loader = AsyncMock(return_value={str(sid): {"atom_id": "a1", "mismatch": True}})
    monkeypatch.setattr(admin_dashboard.seed_audit, "load_subject_seeds", loader)
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"subject_id": sid, "tenant_id": uuid.uuid4(), "tenant_name": "WanderLux", "channel": "blog",
         "state": "proposed", "score": 1.0, "segment_id": "old1", "route_id": None, "cleared_bar_reason": "{}",
         "created_at": "2026-10-16T00:00:00", "canonical_place": "stale", "canonical_action": "visit",
         "hub_name": None, "route_tour_name": None, "route_segment_count": None, "segment_tour_names": None,
         "route_tour_id": None, "route_first_segment_id": None, "route_first_place": None, "route_country": None},
    ]
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    req = MagicMock()
    req.app.state.pool = pool
    out = await admin_dashboard.list_slate(req, tenant_id=TENANT, channel=None, x_admin_secret=SECRET)
    tenant_arg, subjects = loader.await_args.args[1], loader.await_args.args[2]
    assert tenant_arg == TENANT
    assert (subjects[0]["segment_id"], subjects[0]["canonical_place"]) == ("live1", "Delhi")
    row = out["data"][0]
    assert row["seed"] == {"atom_id": "a1", "mismatch": True}
    for internal in ("route_tour_id", "route_first_segment_id", "route_first_place", "route_country"):
        assert internal not in row


def test_seed_audit_is_not_imported_by_tenant_routers():
    """Admin only: no tenant-facing router reads the seed view."""
    import pathlib
    root = pathlib.Path(seed_audit.__file__).resolve().parents[2] / "api" / "routers"
    users = sorted(p.name for p in root.glob("*.py") if "seed_audit" in p.read_text(encoding="utf-8"))
    assert users == ["admin_content_quality.py", "admin_dashboard.py"]
