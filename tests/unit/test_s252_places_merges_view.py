"""S252 — the Places > Merges tab crashed once a dedupe run held a conflict: the job stores possible / conflicts /
defects as lists, the admin view passed them through, and the tab rendered the raw list (React cannot render objects).
The view now returns counts plus capped detail lists."""
from api.routers.admin_places import _DETAIL_CAP, _country_view


def _conflict(i):
    return {"keeper_id": f"k{i}", "destination_id": f"d{i}", "clash": {"a": "a", "b": "b", "reason": "kinds"}}


def test_lists_become_counts_with_a_capped_detail_list():
    v = {"status": "dry_run", "merges": 3, "possible": [["a", "b", "maybe"]], "conflicts": [_conflict(1)],
         "defects": [{"a": "x", "b": "y", "reason": "different_kinds/geocode_defect"}] * 2,
         "merges_planned": [{"merged_id": "m"}], "diff_hash": "h"}
    out = _country_view(v)
    assert (out["possible"], out["conflicts"], out["defects"]) == (1, 1, 2)
    assert out["conflicts_detail"] == [_conflict(1)]
    assert len(out["defects_detail"]) == 2 and out["merges_planned"] == [{"merged_id": "m"}]
    for k in ("possible", "conflicts", "defects", "merges", "usable"):
        assert out[k] is None or isinstance(out[k], int), k


def test_detail_lists_are_capped():
    out = _country_view({"conflicts": [_conflict(i) for i in range(_DETAIL_CAP + 7)]})
    assert out["conflicts"] == _DETAIL_CAP + 7 and len(out["conflicts_detail"]) == _DETAIL_CAP


def test_counts_already_numbers_and_missing_fields_stay_safe():
    out = _country_view({"possible": 4})
    assert out["possible"] == 4 and out["conflicts"] == 0 and out["defects"] == 0
    assert "possible_detail" not in out and out["merges_planned"] == [] and out["undos"] == []
