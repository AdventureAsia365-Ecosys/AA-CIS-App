"""AA-790 (S258) — the fixed blog sample picker (scripts/aa790/fixed_sample.py): pure parts only. No DB, no LLM.
Invented ids and names only (public repo)."""
from collections import Counter

from scripts.aa790 import fixed_sample as fs


def _row(i, tenant, *, dest="Zzland", status="approved", label=None, seed=None, created="2026-10-01", req=None):
    return {"piece_id": f"00000000-0000-4000-8000-{i:012d}", "tenant": tenant, "tenant_id": f"t-{tenant}",
            "request_id": req or f"req-{i}", "atom_id": "a", "status": status, "created_at": created,
            "route_segment_count": 3 if i % 2 else 1, "destination": dest, "seed_sha": seed or f"s{i}",
            "writer_input_sha": "w", "content_sha": "c", "label": label}


def test_distinct_inputs_keeps_the_newest_piece_of_each_seed():
    rows = [_row(1, "A", seed="x", created="2026-10-01", req="r1"), _row(2, "A", seed="x", created="2026-10-05",
                                                                          req="r1"),
            _row(3, "B", seed="y", req="r2")]
    kept, counts = fs.distinct_inputs(rows)
    assert [r["piece_id"][-1] for r in kept] == ["2", "3"]
    assert counts == {"pieces": 3, "distinct_inputs": 2, "distinct_requests": 2}


def test_shares_are_equal_and_a_small_group_gives_its_rest_to_the_others():
    assert fs.shares(24, {"A": 30, "B": 30, "C": 30}) == {"A": 8, "B": 8, "C": 8}
    s = fs.shares(24, {"A": 40, "B": 40, "C": 3})
    assert s["C"] == 3 and sum(s.values()) == 24 and abs(s["A"] - s["B"]) <= 1
    assert sum(fs.shares(24, {"A": 5, "B": 4}).values()) == 9          # fewer inputs than the size: all of them


def test_select_is_deterministic_spread_and_takes_anchors_up_to_half():
    rows = [_row(i, t, dest=f"D{(i // 3) % 3}", status="held" if i % 4 == 0 else "approved",
                 label={"verdict": "fail", "source": "golden:agent_panel"} if i % 5 == 0 else None)
            for i, t in enumerate(["A", "B", "C"] * 20, start=1)]
    a = fs.select(rows, size=24, seed=790)
    assert a == fs.select(list(reversed(rows)), size=24, seed=790)
    assert len(a) == 24 and len({p["piece_id"] for p in a}) == 24
    assert Counter(p["tenant"] for p in a) == {"A": 8, "B": 8, "C": 8}
    for t in "ABC":
        mine = [p for p in a if p["tenant"] == t]
        assert sum(p["why"] == "anchor" for p in mine) <= 4
        assert len({p["destination"] for p in mine}) == 3
    assert fs.select(rows, size=24, seed=791) != a


def test_select_puts_forced_ids_first_and_tops_up_when_unlabelled_run_out():
    rows = [_row(i, "A", label={"verdict": "pass", "source": "content_piece_label"}) for i in range(1, 7)]
    rows += [_row(7, "A")]
    forced = rows[3]["piece_id"]
    picks = fs.select(rows, size=5, seed=1, force_ids=(forced, "zz-not-there"))
    assert picks[0]["piece_id"] == forced and picks[0]["why"] == "forced"
    assert len(picks) == 5 and Counter(p["why"] for p in picks) == {"forced": 1, "anchor": 3, "spread": 1}


def test_to_row_needs_a_seed_and_a_brand_pack_and_reads_the_label():
    base = {"piece_id": "p1", "tenant_id": "t", "tenant": "Zz", "request_id": "r", "atom_id": "a", "status": "held",
            "created_at": "2026-10-01", "route_segment_count": 2, "content_text": "Zz text."}
    wi = {"seed": "Zz seed.", "brand_pack": {"brand_name": "Zz"}, "destination": "Zzland"}
    assert fs.to_row({**base, "writer_input": {"seed": "Zz"}}, {}, {}) is None
    golden = {"p1": {"label_verdict": "fail", "label_source": "agent_panel", "sample_id": "t9b-003",
                     "post_edit": False}}
    r = fs.to_row({**base, "writer_input": wi}, golden, {"p1": ["pass", "pass"]})
    assert r["label"] == {"verdict": "fail", "source": "golden:agent_panel", "sample_id": "t9b-003"}
    assert r["seed_sha"] == fs.sha("Zz seed.") and r["destination"] == "Zzland"
    golden["p1"]["post_edit"] = True     # a post-edit golden text is not the piece's own text
    assert fs.to_row({**base, "writer_input": wi}, golden, {"p1": ["pass", "fail", "pass"]})["label"] == \
        {"verdict": "pass", "source": "content_piece_label"}
