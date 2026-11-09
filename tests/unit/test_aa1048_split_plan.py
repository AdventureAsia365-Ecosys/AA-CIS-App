"""AA-1048 W2 (S273) — the held-out split plan: target, export stats, the picks (held-out tours only, round robin, a
cap per tour, already-exported and tourless objects never picked), the missing objects and their cost, the id list
for select_samples --ids, and the CLI `split-plan` / `export-fixture --split`. Invented ids only."""
import json
import uuid

import pytest

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import sample_size as ss
from services.ground_truth import split_plan as sp


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=0xA1048000 + n))


def _tours(side: str, k: int, start: int = 0) -> list[str]:
    out, n = [], start
    while len(out) < k:
        t = _uuid(n)
        if cl.split_of(f"tour_id:{t}") == side:
            out.append(t)
        n += 1
    return out


def test_target_is_the_r1_bar_at_the_panel_rate():
    need = ss.min_flagged(0.92, ss.BARS[0])
    assert sp.target_held_out_claims() == max(-(-need // 0.06), -(-20 // 0.06)) == 784
    assert sp.target_held_out_claims(observed_precision=0.85) is None


def test_plan_picks_held_out_tours_round_robin_with_a_cap():
    held, train = _tours("held_out", 3), _tours("train", 2, start=500)
    cands = [sp.Candidate(_uuid(1000 + i * 10 + j), "t9_blog", t) for i, t in enumerate(held) for j in range(4)]
    cands += [sp.Candidate(_uuid(2000 + i), "t9_blog", t) for i, t in enumerate(train)]
    cands += [sp.Candidate(_uuid(3000), "t9_blog", None)]
    out = sp.plan(cands, target_claims=600, writers=("t9_blog",), per_tour_cap=2)["writers"]["t9_blog"]
    assert out["need_objects"] == 10 and out["held_out_candidates"] == 12 and out["without_tour"] == 1
    assert len(out["picks"]) == 6                      # 3 tours x cap 2
    first_round = {c.tour_id for c in cands if c.id in out["picks"][:3]}
    assert first_round == set(held)
    assert out["missing_objects"] == 4 and out["missing_cost_usd"] == pytest.approx(0.44)
    assert set(out["held_out_tours"]) == set(held)


def test_plan_counts_what_the_export_has_and_skips_its_objects():
    held = _tours("held_out", 2)
    cands = [sp.Candidate(_uuid(4000 + i), "t2", held[i % 2]) for i in range(6)]
    res = sp.plan(cands, target_claims=200, have={"t2": 100}, exclude=[_uuid(4000).upper()], writers=("t2",),
                  per_tour_cap=None)
    row = res["writers"]["t2"]
    assert row["need_held_out_claims"] == 100 and row["need_objects"] == 2
    assert _uuid(4000) not in row["picks"] and len(row["picks"]) == 2 and row["missing_objects"] == 0
    assert res["missing_cost_usd"] == 0
    done = sp.plan(cands, target_claims=200, have={"t2": 250}, writers=("t2",))["writers"]["t2"]
    assert done["need_objects"] == 0 and done["picks"] == []


def test_ids_file_is_what_select_samples_reads():
    from scripts.aa729.select_samples import parse_ids

    held = _tours("held_out", 1)
    res = sp.plan([sp.Candidate(_uuid(5000), "t9_social", held[0])], target_claims=10, writers=("t9_social",))
    ids = sp.ids_file(res)
    assert ids == [{"id": _uuid(5000), "fills": "aa1048_held_out:t9_social"}]
    assert list(parse_ids(ids)) == [_uuid(5000)]


def _lines(tour: str, piece: str, sample: str):
    out = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                           "two. Breakfast is served at the lodge each morning."}
    return pn.export_records(set_id="ZZ-SP", writer="t9_blog", scope="tenant", tenant_id=_uuid(9),
                             refs={"sample_id": sample, "piece_id": piece, "tour_id": tour}, output=out,
                             source_text="Day 1: Velmora.")


def test_export_stats_counts_sides_and_ids():
    h, t = _tours("held_out", 1)[0], _tours("train", 1, start=700)[0]
    lines = _lines(h, _uuid(6000), "zz-1") + _lines(t, _uuid(6001), "zz-2")
    st = sp.export_stats(lines)
    row = st["writers"]["t9_blog"]
    assert row["groups"] == 2 and row["held_out_groups"] == 1 and row["held_out_claims"] == row["claims"] // 2
    assert {_uuid(6000), _uuid(6001), h, t} <= st["ids"]


def test_cli_split_plan_with_live_writes_the_id_list(tmp_path):
    from scripts.aa1048 import cli

    h = _tours("held_out", 2)
    (tmp_path / "claims.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _lines(h[0], _uuid(7000), "zz-3")))
    live = {"candidates": [{"id": _uuid(7000), "writer": "t9_blog", "tour_id": h[0], "status": "approved"},
                           {"id": _uuid(7001), "writer": "t9_blog", "tour_id": h[1], "status": "held"}]}
    (tmp_path / "live.json").write_text(json.dumps(live))
    ids = tmp_path / "ids.json"
    args = ["split-plan", "--export", str(tmp_path / "claims.jsonl"), "--live", str(tmp_path / "live.json"),
            "--ids-out", str(ids)]
    assert cli.main(args) == 0
    # one export per set: --export repeats and the stats read every file
    (tmp_path / "more.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _lines(h[1], _uuid(7002), "zz-4")))
    two = cli.cmd_split_plan(cli.build_parser().parse_args(
        ["split-plan", "--export", str(tmp_path / "claims.jsonl"), "--export", str(tmp_path / "more.jsonl")]))
    one = cli.cmd_split_plan(cli.build_parser().parse_args(["split-plan", "--export", str(tmp_path / "claims.jsonl")]))
    assert two["export"]["t9_blog"]["held_out_claims"] > one["export"]["t9_blog"]["held_out_claims"]
    picked = json.loads(ids.read_text())
    assert [x["id"] for x in picked] == [_uuid(7001)]           # the exported piece is never picked again
    with pytest.raises(ValueError):
        cli.live_from_json({"nope": 1})
