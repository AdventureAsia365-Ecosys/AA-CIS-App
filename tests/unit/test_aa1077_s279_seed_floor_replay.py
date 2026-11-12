"""AA-1077 (S279) — the $0 seed floor replay (scripts/aa1077/seed_floor_replay.py), pure parts only: the per-piece
numbers, the measured ratio (material-bound pieces of the newest writer version), the newest piece per Subject and the
keep / skip verdicts per floor. No DB. Invented ids and places only (Corvane Fort, Lake Aurel)."""
from scripts.aa1077 import seed_floor_replay as rp

PARA = ("The walls of Corvane Fort run along the shore above Lake Aurel, and the lanes inside them lead past the old "
        "warehouses to the lighthouse on the southern bastion.")


def _text(chars: int) -> str:
    out = "## Corvane Fort\n\n"
    while len(out) < chars:
        out += PARA + "\n\n"
    return out[:chars]


def _row(pid, status, chars, *, subject="5ub1", writer="w7", seed_words=10, moments=(), created="2026-11-12 10:00"):
    seed = " ".join(["walk"] * seed_words)
    return {"piece_id": pid + "0000-0000", "tenant": "audit-x", "status": status, "content_text": _text(chars),
            "writer_input": {"seed": seed, "route_moments": [{"atom_id": "a", "seed": m} for m in moments],
                             "writer_prompt_version": writer},
            "subject_id": subject + "0000-0000", "segment_id": "seg-x", "tour_id": "70a50000-0000",
            "created_at": created}


def test_t9_seed_words_adds_the_route_moments():
    wi = {"seed": "one two three", "route_moments": [{"atom_id": "a", "seed": "four five"}, {"seed": None}, "x"]}
    assert rp.t9_seed_words(wi) == 5 and rp.t9_seed_words(None) == 0


def test_piece_line_ratio_uses_the_t7_estimate_then_the_writer_seed():
    line = rp.piece_line(_row("aaaa", "held", 4425, seed_words=600), 640)
    assert (line["chars"], line["side"], line["t7_words"], line["t9_words"]) == (4425, "short", 640, 600)
    assert line["ratio"] == round(4425 / 640, 3) and line["tour"] == "70a50000" and line["subject"] == "5ub10000"
    assert rp.piece_line(_row("bbbb", "held", 4425, seed_words=600), 0)["ratio"] == round(4425 / 600, 3)
    assert rp.piece_line(_row("cccc", "held", 4425, seed_words=0), None)["ratio"] is None


def _lines():
    return [
        rp.piece_line(_row("p1", "held", 3300, writer="w5", seed_words=150, created="2026-11-10 08:00"), 150),
        rp.piece_line(_row("p2", "held", 4425, subject="5ub2", seed_words=640), 640),           # short, newest
        rp.piece_line(_row("p3", "held", 4200, subject="5ub3", seed_words=700), 700),           # short, newest
        rp.piece_line(_row("p4", "approved", 5200, subject="5ub4", seed_words=1500), 1500),     # in band: not used
    ]


def test_measured_ratio_is_the_median_of_the_newest_writers_short_pieces():
    m = rp.measured_ratio(_lines())
    assert m["writer"] == "w7" and m["n"] == 2
    assert m["ratio"] == round((4425 / 640 + 4200 / 700) / 2, 3)
    assert rp.measured_ratio([_lines()[3]]) == {"ratio": None, "n": 0, "writer": None}


def test_newest_piece_per_subject_decides():
    lines = _lines() + [rp.piece_line(_row("p5", "approved", 5000, subject="5ub2", seed_words=900,
                                           created="2026-11-13 09:00"), 900)]
    subs = {s["subject"]: s["piece"] for s in rp.subjects(lines)}
    assert subs["5ub20000"].startswith("p5")


def test_verdicts_per_floor_count_short_and_approved_skips():
    subs = rp.subjects(_lines())
    configured = rp.verdicts(subs, rp.floor_at(600, 5.8))          # 811 words
    by = {r["subject"]: r["verdict"] for r in configured["rows"]}
    assert by["5ub10000"] == "seed_below_min" and by["5ub20000"] == "seed_below_length_floor"
    assert by["5ub30000"] == "seed_below_length_floor" and by["5ub40000"] == "keep"
    assert (configured["kept"], configured["skipped"], configured["short_skipped"], configured["short_total"],
            configured["approved_skipped"]) == (1, 3, 3, 3, 0)
    flat = rp.verdicts(subs, rp.floor_at(600, None))
    assert (flat["floor"]["min_seed_words"], flat["skipped"]) == (600, 1)


def test_summary_recommends_the_measured_ratio():
    s = rp.summarize(_lines(), 600, 5.8)
    assert s["pieces"] == 4 and s["subjects"] == 4
    assert s["recommended_env"]["T7_BLOG_CHARS_PER_SEED_WORD"] == s["measured"]["ratio"]
    assert s["recommended_env"]["sized_min_seed_words"] == s["at_measured"]["floor"]["sized"] > 600
    assert "at_measured" not in rp.summarize([_lines()[3]], 600, 5.8)
