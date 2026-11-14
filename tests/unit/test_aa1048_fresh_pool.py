"""AA-1048 (S281 lane l8) — the fresh pool of R1-fired claims (services/ground_truth/fresh_pool.py): pieces an earlier
round touched are kept out (out of sample by construction), the sample is deterministic, proportional and capped by
the setting, the labelling sheet is checked row by row, the sheets' majority and the stored label decide the groups,
and the manifest lets two_step.py verify the pool. $0; every text, place and hash is invented."""
import hashlib
import json

import pytest

from services.ground_truth import fresh_pool as fp
from services.ground_truth import instrument_eval as ev
from services.ground_truth import r1
from services.ground_truth import two_step as ts

SOURCE = "Day 1: Arrive in Pellow.\nDay 2: Walk the dunes of Sarrow.\nDay 3: Drive back to Pellow."


def _h(n: int) -> str:
    return hashlib.sha256(f"invented-fresh-{n}".encode()).hexdigest()


def _piece(n: int) -> str:
    return hashlib.sha256(f"invented-piece-{n}".encode()).hexdigest()


# Pieces of each split (the split is a frozen hash of the piece; found once, in a fixed order).
TRAIN = [n for n in range(200) if r1.split_of_line({"content_sha256": _piece(n)}) == "train"][:6]
HELD = [n for n in range(200) if r1.split_of_line({"content_sha256": _piece(n)}) == "held_out"][:2]


def _line(n, piece_n, writer="t2", claim=None):
    claim = claim or f"Sentence {n} about the dunes of Sarrow."
    return {"claim_hash": _h(n), "writer": writer, "field": "itineraries", "claim_text": claim,
            "field_text": f"Intro line.\n{claim}\nClosing line.", "source_text": SOURCE,
            "content_sha256": _piece(piece_n), "stratum": "representative", "claim_class": "fact.embellish"}


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


# ── eligible ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_every_piece_of_an_excluded_pool_or_run_is_kept_out():
    old_pool = [_line(1, TRAIN[0])]
    exports = [_line(2, TRAIN[1]), _line(3, TRAIN[2]), _line(4, TRAIN[3]), _line(5, TRAIN[0])]
    report = json.dumps({"run_id": "3018008d-aaaa", "votes": {_h(3)[:16]: [True, True, True]}})
    pieces, runs = fp.shaped_pieces([old_pool], [report], exports)
    assert pieces == {_piece(TRAIN[0]), _piece(TRAIN[2])} and runs == ["3018008d"]
    left = fp.eligible(exports, pieces, "train")
    assert [e["claim_hash"] for e in left] == [_h(2), _h(4)]                # claim 5 shares the pool's piece


def test_a_run_prefix_found_nowhere_or_a_bare_prefix_list_is_refused():
    with pytest.raises(fp.PoolRefused, match="cannot be kept out"):
        fp.shaped_pieces([], [json.dumps({"run_id": "33835b87-x", "votes": {"ab" * 8: [True]}})], [_line(1, TRAIN[0])])
    with pytest.raises(fp.PoolRefused, match="run report"):
        fp.shaped_pieces([], [json.dumps(["ab" * 8])], [])


def test_the_split_filter_reads_the_frozen_split():
    exports = [_line(1, TRAIN[0]), _line(2, HELD[0])]
    assert [e["claim_hash"] for e in fp.eligible(exports, set(), "train")] == [_h(1)]
    assert [e["claim_hash"] for e in fp.eligible(exports, set(), "held_out")] == [_h(2)]
    assert len(fp.eligible(exports, set(), "all")) == 2


def test_eligible_command_writes_the_claims_and_a_manifest_without_text(tmp_path, capsys):
    exports = _w(tmp_path / "claims.jsonl", [_line(1, TRAIN[0]), _line(2, TRAIN[1])])
    pool = _w(tmp_path / "old-pool.jsonl", [_line(9, TRAIN[0])])
    assert fp.main(["eligible", "--export", exports, "--exclude-pool", pool, "--out", str(tmp_path / "e.jsonl"),
                    "--manifest", str(tmp_path / "e.json")]) == 0
    rep = json.loads(capsys.readouterr().out)
    man = json.loads((tmp_path / "e.json").read_text())
    assert rep["claims"] == 1 and man["excluded_pieces"] == [_piece(TRAIN[0])[:16]]
    assert "dunes" not in (tmp_path / "e.json").read_text()
    assert fp.main(["eligible", "--export", exports, "--out", str(tmp_path / "x.jsonl"),
                    "--manifest", str(tmp_path / "x.json")]) == 2                    # nothing named to keep out


# ── sample ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_fire_votes_read_the_r1_calibrate_report_and_the_votes_log():
    rep = json.dumps({"votes": {fp.FIRE_INSTRUMENT: {_h(1): [True, None, True]}, "other": {_h(2): [False]}}})
    assert fp.read_fire_votes(rep) == {_h(1): [True, None, True]}
    log = "VOTES " + json.dumps({"instrument": fp.FIRE_INSTRUMENT, "votes": {_h(2)[:12]: "101"}})
    assert fp.read_fire_votes(log) == {_h(2)[:12]: [True, False, True]}
    with pytest.raises(fp.PoolRefused):
        fp.read_fire_votes("{}")


def test_pick_is_deterministic_proportional_and_salted():
    fired = [_line(n, TRAIN[n % 3], writer="t2" if n < 12 else "t9_blog") for n in range(18)]
    a = fp.pick(fired, 9, "s1")
    assert a == fp.pick(list(reversed(fired)), 9, "s1") and len(a) == 9
    assert sum(e["writer"] == "t2" for e in a) == 6                                 # 12 : 6 of 18 -> 6 : 3 of 9
    assert [e["claim_hash"] for e in fp.pick(fired, 9, "s2")] != [e["claim_hash"] for e in a]
    assert len(fp.pick(fired, 50, "s1")) == 18


def _eligible(tmp_path, n=10):
    path = tmp_path / "eligible.jsonl"
    _w(path, [_line(k, TRAIN[k % 4], writer="t2" if k % 2 else "t9_blog") for k in range(n)])
    man = tmp_path / "eligible.json"
    man.write_text(json.dumps({"eligible_sha256": fp._sha(path.read_text(encoding="utf-8")),
                               "excluded_runs": ["3018008d", "33835b87", "2977dd78"],
                               "excluded_pieces": [_piece(TRAIN[5])[:16]], "split": "train"}), encoding="utf-8")
    votes = tmp_path / "votes.json"
    votes.write_text(json.dumps({"votes": {fp.FIRE_INSTRUMENT: {
        _h(k): [True, True, False] if k != 0 else [False, False, True] for k in range(n)}}}), encoding="utf-8")
    return str(path), str(man), str(votes)


def test_sample_writes_the_sheet_sources_and_a_text_free_manifest(tmp_path, capsys):
    elig, man, votes = _eligible(tmp_path)
    out = tmp_path / "sample"
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", votes, "--size", "6",
                    "--out-dir", str(out)]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["fired"] == 9 and rep["claims"] == 6                                 # claim 0 was not fired
    sheet = [json.loads(x) for x in (out / "sheet.jsonl").read_text().splitlines()]
    assert [r["n"] for r in sheet] == list(range(1, 7))
    assert all(r[f] == "" for r in sheet for f in fp.SHEET_FIELDS)
    assert sheet[0]["before"] == "Intro line." and sheet[0]["after"] == "Closing line."
    assert "dunes" not in (out / "sample-manifest.json").read_text()
    assert len((out / "sources.jsonl").read_text().splitlines()) == len({r["piece"] for r in sheet})


def test_sample_refuses_a_size_over_the_setting_and_another_eligible_file(tmp_path, capsys):
    elig, man, votes = _eligible(tmp_path)
    cap = fp.sample_max()
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", votes, "--size", str(cap + 1),
                    "--out-dir", str(tmp_path / "s")]) == 2
    assert "claim_fresh_pool_sample_max" in json.loads(capsys.readouterr().out)["refused"]
    with open(elig, "a", encoding="utf-8") as f:
        f.write(json.dumps(_line(99, TRAIN[0])) + "\n")
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", votes,
                    "--out-dir", str(tmp_path / "s")]) == 2
    assert "sha256 differs" in json.loads(capsys.readouterr().out)["refused"]


# ── sheet and import ──────────────────────────────────────────────────────────────────────────────────────────────


def _row(n, label="ok", ok_reason="paraphrase", step1="restatement", reason="The source says the same in words."):
    return {"n": n, "label": label, "ok_reason": ok_reason, "step1": step1, "reason": reason}


def test_the_sheet_is_checked_row_by_row():
    good = [_row(1), _row(2, "violation", "", "new_claim")]
    assert fp.check_sheet(good, 2)[2]["label"] == "violation"
    bad = [_row(1, ok_reason=""), _row(1), _row(3, label="maybe", step1="x", reason=" ")]
    with pytest.raises(fp.PoolRefused) as e:
        fp.check_sheet(bad, 3, "lens-a")
    msg = str(e.value)
    for part in ("lens-a", "ok_reason", "twice", "label 'maybe'", "step1 'x'", "no reason", "rows missing: [2]"):
        assert part in msg
    with pytest.raises(fp.PoolRefused, match="not a row number"):
        fp.check_sheet([_row(7)], 1)


@pytest.mark.parametrize("labels,stored,expected", [
    (["violation", "violation", "ok"], None, ("tp", "violation")),
    (["ok", "ok", "violation"], "ok", ("fp_paraphrase", "ok")),
    (["ok", "violation", "unsure"], None, (None, "no_panel_majority")),
    (["unsure", "unsure", "ok"], None, (None, "panel_unsure")),
    (["violation", "violation", "violation"], "ok", (None, "stored_label_disagrees")),
])
def test_the_panel_majority_and_the_stored_label_decide_together(labels, stored, expected):
    answers = [{"label": lab, "ok_reason": "paraphrase" if lab == "ok" else "", "step1": "restatement"}
               for lab in labels]
    assert fp.decide(answers, stored) == expected


def test_an_ok_claim_takes_its_reason_majority_or_other():
    mixed = [{"label": "ok", "ok_reason": r, "step1": "restatement"} for r in ("verbatim", "background", "other")]
    assert fp.decide(mixed, None) == ("fp_other", "ok")
    bg = [{"label": "ok", "ok_reason": "background", "step1": "restatement"}] * 2
    assert fp.decide(bg, None) == ("fp_background", "ok")


def test_import_builds_a_pool_that_two_step_accepts_as_fresh(tmp_path, capsys):
    elig, man, votes = _eligible(tmp_path, n=8)
    out = tmp_path / "sample"
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", votes, "--out-dir", str(out)]) == 0
    capsys.readouterr()
    n = len((out / "sheet.jsonl").read_text().splitlines())
    rows = [_row(k, "violation", "", "new_claim") if k % 2 else _row(k) for k in range(1, n + 1)]
    sheets = [_w(tmp_path / f"lens-{x}.jsonl", rows) for x in "abc"]
    args = ["import", "--candidates", str(out / "candidates.jsonl"), "--sample-manifest",
            str(out / "sample-manifest.json"), "--eligible-manifest", man, "--out", str(tmp_path / "pool.jsonl"),
            "--manifest", str(tmp_path / "fresh.json")]
    for s in sheets:
        args += ["--sheet", s]
    assert fp.main(args) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["pool"] == n and set(rep["by_group"]) == {"tp", "fp_paraphrase"}
    pool_text = (tmp_path / "pool.jsonl").read_text(encoding="utf-8")
    pool = ev.read_pool(pool_text)
    assert {p.group for p in pool} == {"tp", "fp_paraphrase"}
    manifest = json.loads((tmp_path / "fresh.json").read_text())
    assert set(manifest["step1_gold"].values()) == {"new_claim", "restatement"}
    oos = ts.fresh_check(manifest, pool_text, pool, ["2977dd78", "3018008d", "33835b87"])
    assert ts.oos_refusal(oos) is None and oos["excluded_pieces"] == 1


def test_import_refuses_a_sheet_of_another_sample(tmp_path, capsys):
    elig, man, votes = _eligible(tmp_path, n=6)
    out = tmp_path / "sample"
    fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", votes, "--out-dir", str(out)])
    capsys.readouterr()
    (out / "candidates.jsonl").write_text((out / "candidates.jsonl").read_text() + "\n", encoding="utf-8")
    sheet = _w(tmp_path / "lens.jsonl", [_row(1)])
    assert fp.main(["import", "--candidates", str(out / "candidates.jsonl"), "--sample-manifest",
                    str(out / "sample-manifest.json"), "--eligible-manifest", man, "--sheet", sheet,
                    "--out", str(tmp_path / "p.jsonl"), "--manifest", str(tmp_path / "m.json")]) == 2
    assert "sha256 differs" in json.loads(capsys.readouterr().out)["refused"]
