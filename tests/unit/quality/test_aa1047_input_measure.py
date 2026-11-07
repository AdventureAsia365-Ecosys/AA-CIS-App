"""AA-1047 (Quality W1) — the input measurement script (scripts/quality/aa1047_input_measure.py): the pure measurements
over invented rows, the output carries counts and ids only (no text), and the database run is read-only SELECTs over a
fake connection. Invented names and places only."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from scripts.quality import aa1047_input_measure as m

TOUR_A = "7a000000-0000-4000-8000-00000000000a"
TOUR_B = "7a000000-0000-4000-8000-00000000000b"
TENANT = "00000000-0000-4000-8000-000000a10473"

RAW_A = {"tour_id": TOUR_A, "src_name": "Varn Valley Walk", "src_summary": "A walk in the Varn Valley.",
         "src_highlights": '["Rim path of Lumen Gorge"]',
         "src_itineraries": "Day 1: Tavo\nWalk the rim path of Lumen Gorge to the Tavo lookout, 12 km.\n"
                            "Day 2: Orsa\nRow across Orsa Lake to the island chapel. Includes lunch.",
         "country": "Zemlia", "duration": "2 days", "price_raw": "1,250 USD", "inclusions": "Guide\nLunch",
         "exclusions": "Tips", "group_size": "2-12", "best_time_to_go": None}
RAW_B = {**RAW_A, "tour_id": TOUR_B, "src_name": "Kesra Dunes Ride", "src_itineraries": "Day 1: Kesra\nRide.",
         "src_summary": "", "src_highlights": None, "inclusions": "", "exclusions": "", "duration": "1 day"}


def test_anchor_share_counts_each_atom_once():
    atoms = [{"atom_id": "a1", "tour_id": TOUR_A, "text": "Lumen Gorge — walk",
              "evidence": "Walk the rim path of Lumen Gorge to the Tavo lookout"},
             {"atom_id": "a2", "tour_id": TOUR_A, "text": "Orsa Lake — boat", "evidence": None},
             {"atom_id": "a3", "tour_id": TOUR_A, "text": "Balloon flight", "evidence": "Hot-air balloon over Kesra"},
             {"atom_id": "a4", "tour_id": "7a000000-0000-4000-8000-0000000000ff", "text": "x", "evidence": "y"}]
    out = m.measure_anchors(atoms, [RAW_A])
    assert (out["atoms"], out["checked"], out["anchored"], out["unanchored"], out["unknown_no_raw"]) == (4, 3, 2, 1, 1)
    assert out["unanchored_share"] == round(1 / 3, 4) and out["no_evidence"] == 1 and out["tours"] == 1


def test_pointers_and_figures_cap():
    report, s1 = m.measure_pointers([RAW_A, RAW_B])
    assert report["active_sources"] == 2 and report["pointer_failures"] == 0 and report["bar_met"] is True
    assert report["failing_tour_ids"] == [] and s1["s1_figures_cap_per_tour"]["n"] == 2


def test_pointer_failures_are_reported_with_ids(monkeypatch):
    from services.quality import factsheet as fs

    real = fs.build_tour_factsheet

    def broken(tour, **kw):
        sheet = real(tour, **kw)
        return fs.FactSheet(builder=sheet.builder, lines=sheet.lines, excluded=sheet.excluded, meta=sheet.meta,
                            text=sheet.text, pointer_failures=({"section": "days", "value": "x", "field": "f"},))
    monkeypatch.setattr(fs, "build_tour_factsheet", broken)
    report, _ = m.measure_pointers([RAW_A])
    assert report["sources_with_failures"] == 1 and report["failing_tour_ids"] == [TOUR_A]
    assert report["by_section"] == {"days": 1} and report["bar_met"] is False


def _wi(**kw):
    seed = "\n".join(["Lumen Gorge — walk", "Location: Varn, Zemlia", "Source itinerary: Walk the rim.",
                      "Other trips that include this same moment (for context only — never promise these details "
                      "for this trip):"] + [f"- Sibling line number {i} about the gorge path." for i in range(40)])
    return {"seed": seed, "facts": "", "brand_pack": {"brand_name": "Trailwise"}, "trip": "Zemlia Loop",
            "destination": "Zemlia", "channel": "blog", **kw}


def test_piece_sheet_matches_the_stored_grounding():
    from services.acp_content_writing import service

    wi = _wi(writer_trip="Zemlia")
    assert m.piece_sheet(wi).render() == service.grounding_from_writer_input(wi)


def test_excluded_per_piece(monkeypatch):
    monkeypatch.setattr(m, "READER_CAP", 300)
    pieces = [{"piece_id": "p1", "tenant_id": TENANT, "writer_input": _wi()},
              {"piece_id": "p2", "tenant_id": TENANT, "writer_input": {**_wi(), "seed": "short", "channel": "linkedin"}}]
    out = m.measure_excluded(pieces, {(TENANT, "Zemlia"): 3})
    assert out["pieces"] == 2 and out["channels"] == {"blog": 1, "linkedin": 1}
    assert out["facts_budget_per_piece"]["max"] == 3 and out["facts_budget_per_piece"]["with_any"] == 2
    reader = out["reader_budget_per_piece"]
    assert reader["with_any"] == 1 and reader["by_kind"] == {"O": reader["max"]} and reader["cap_chars"] == 300


def test_drift_counts_blocking_masters_and_skips_human_edited():
    clean = {**RAW_A, "aa_name": "Varn Valley Walk", "aa_summary": "A walk in the Varn Valley.",
             "aa_itineraries": "Day 1 — Tavo\nWalk the rim path of Lumen Gorge, 12 km.\n"
                               "Day 2 — Orsa\nRow across Orsa Lake to the island chapel.",
             "aa_highlights": '["Rim path of Lumen Gorge"]', "human_edited": False}
    drifted = {**clean, "aa_summary": "A walk of 45 km with a stop in Pellmar.", "pt_id": "p2"}
    human = {**drifted, "human_edited": True}
    out = m.measure_drift([clean, drifted, human])
    assert (out["active_masters"], out["human_edited"], out["checked"]) == (3, 1, 2)
    assert out["blocking"] == 1 and out["blocking_rate"] == 0.5 and out["tune_provenance_before_w3"] is True
    assert out["masters_by_fact_class"].get("number") == 1


def test_output_carries_no_text_column():
    """The SELECTs read text to measure it; the JSON printed carries counts, shares and ids only."""
    atoms = [{"atom_id": "a1", "tour_id": TOUR_A, "text": "Lumen Gorge — walk", "evidence": "Walk the rim path"}]
    blob = json.dumps({"a": m.measure_anchors(atoms, [RAW_A]), "p": m.measure_pointers([RAW_A]),
                       "d": m.measure_drift([{**RAW_A, "aa_name": "Varn Valley Walk", "human_edited": False}])},
                      default=str)
    for phrase in ("Lumen", "Varn", "Tavo", "Orsa", "rim path"):
        assert phrase not in blob


class _Rec(dict):
    pass


class FakeConn:
    def __init__(self, tables):
        self.tables = tables
        self.sql: list[str] = []
        self.closed = False

    def transaction(self, readonly=False):
        assert readonly is True
        conn = self

        class _Tr:
            async def start(self):
                conn.sql.append("BEGIN READ ONLY")

            async def rollback(self):
                conn.sql.append("ROLLBACK")
        return _Tr()

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        for key, rows in self.tables.items():
            if key in sql:
                return [_Rec(r) for r in rows]
        return []

    async def close(self):
        self.closed = True


def test_run_is_read_only_selects(monkeypatch):
    piece = {"piece_id": "p1", "tenant_id": TENANT, "writer_input": json.dumps(_wi())}
    master = {**RAW_A, "pt_id": "p", "aa_name": "Varn Valley Walk", "human_edited": False}
    conn = FakeConn({"FROM gold_aa_internal.published_tours": [master],
                     "FROM acp_contract.v_active_tour_atoms": [{"atom_id": "a1", "tour_id": TOUR_A,
                                                              "text": "Lumen Gorge", "evidence": "Walk the rim path"}],
                     "rt.tour_id = ANY": [RAW_A], "rt.source_status = 'active'": [RAW_A, RAW_B],
                     "FROM acp_shared.content_piece": [piece], "FROM acp_shared.facts": []})

    async def fake_connect(*a, **kw):
        assert kw.get("ssl") == "require"
        return conn
    import asyncpg
    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost/z")
    out = asyncio.run(m.run("all", piece_days=60, limit=None))
    assert set(out) >= {"pointers", "excluded_s1", "anchors", "excluded_t9", "master_drift"}
    assert conn.sql[0] == "BEGIN READ ONLY" and conn.sql[-1] == "ROLLBACK" and conn.closed
    for sql in conn.sql[1:-1]:
        assert re.match(r"\s*SELECT\b", sql), sql
        assert "*" not in sql.split("FROM")[0], sql            # no whole-row select
    assert out["pointers"]["active_sources"] == 2 and out["excluded_t9"]["pieces"] == 1


def test_main_rejects_an_unknown_measure(monkeypatch, capsys):
    monkeypatch.setenv("MEASURE", "everything")
    assert m.main() == 2
    assert "unknown MEASURE" in capsys.readouterr().out


def test_main_prints_only_the_error_class(monkeypatch, capsys):
    async def boom(*a, **kw):
        raise RuntimeError("value 'Varn' leaked")
    monkeypatch.setenv("MEASURE", "drift")
    monkeypatch.setattr(m, "run", boom)
    assert m.main() == 1
    assert capsys.readouterr().out.strip() == json.dumps({"error": "RuntimeError"})


@pytest.mark.parametrize("values,expected", [([], {"n": 0}), ([0, 0, 2], {"n": 3, "with_any": 1, "max": 2})])
def test_distribution(values, expected):
    out = m.distribution(values)
    assert {k: out[k] for k in expected} == expected
