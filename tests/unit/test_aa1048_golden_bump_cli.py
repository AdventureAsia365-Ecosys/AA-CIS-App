"""AA-1048 (Quality W2) — the golden-set version bump that revokes test-stub labels (scripts/aa729/labels.py revoke)
and the local commands of scripts/aa1048/cli.py (export-fixture, rederive, detectors, mutations, sample-size). A
synthetic fixture with invented names only; no DB, no model call."""
import json

import pytest

from scripts.aa1048 import cli
from scripts.aa729 import common, labels
from services.golden_set.derive import FIXTURE_SCHEMA

TENANT = "00000000-0000-4000-8000-000000a10482"


def _sample(sid, verdict, reason="ok by the panel", source="agent_panel", text="Walk 12 km to Ostrin Valley."):
    ctx = {"atom_text": "Ostrin Valley — walk\nSource itinerary: Walk 12 km to Ostrin Valley.", "channel": "blog",
           "brand_audience": {"brand_name": "Lumen Trails"}, "trip_name": "Velmora Walk", "cta": "Ask us."}
    return {"sample_id": sid, "stratum": "t9_blog", "kind": "t9", "channel": "blog", "label_source": source,
            "label": common.make_label(verdict, source, reason=reason),
            "refs": {"tenant_id": TENANT, "piece_id": f"00000000-0000-4000-8000-{int(sid[-3:]):012d}"},
            "input": {"context": ctx}, "output": {"content_text": f"{text} The valley is quiet in spring.",
                                                  "seo_title": "Velmora walk"}}


def _fixture():
    samples = [_sample("t9b-001", "pass"), _sample("t9b-003", "fail", text="Walk 40 km to Ostrin Valley."),
               _sample("t9b-002", "pass", reason="ZZ S239 test label", source="human_label"),
               _sample("t9b-004", "pass"), _sample("t9b-005", "fail")]
    return {"schema": FIXTURE_SCHEMA, "fixture_version": "1.3.0", "seed": 729, "samples": samples}


def test_revoke_drops_the_stub_and_lists_every_flip():
    fx = _fixture()
    assert labels.is_stub_label(fx["samples"][2]["label"]) and not labels.is_stub_label(fx["samples"][0]["label"])
    new, info = labels.revoke(fx, approved_by="tech_lead", reason="test stub, not a judgement")
    assert new["fixture_version"] == "1.4.0" and fx["fixture_version"] == "1.3.0"      # the input is not changed
    assert [s["sample_id"] for s in new["samples"]] == ["t9b-001", "t9b-003", "t9b-004", "t9b-005"]
    assert info["revoked"] == [{"sample_id": "t9b-002", "verdict": "pass", "label_source": "human_label",
                                "stub": True, "to": "revoked"}]
    assert info["labels_before"] == {"t9_blog:fail": 2, "t9_blog:pass": 3}
    assert info["labels_after"] == {"t9_blog:fail": 2, "t9_blog:pass": 2}
    assert isinstance(info["split_moves"], list)
    for m in info["split_moves"]:
        assert m["from"] != m["to"]
    assert new["label_history"][-1]["revoked"] == ["t9b-002"]
    assert new["label_history"][-1]["source"] == "revoke"


def test_revoke_refusals_and_explicit_ids():
    fx = _fixture()
    with pytest.raises(ValueError, match="approved_by"):
        labels.revoke(fx, approved_by=" ", reason="x")
    with pytest.raises(ValueError, match="reason"):
        labels.revoke(fx, approved_by="tl", reason="")
    with pytest.raises(ValueError, match="unknown"):
        labels.revoke(fx, approved_by="tl", reason="x", ids=["t9b-999"])
    clean = {**fx, "samples": [s for s in fx["samples"] if s["sample_id"] != "t9b-002"]}
    with pytest.raises(ValueError, match="nothing to revoke"):
        labels.revoke(clean, approved_by="tl", reason="x")
    _new, info = labels.revoke(fx, approved_by="tl", reason="x", ids=["t9b-004"])
    assert info["revoked"][0]["stub"] is False


def test_revoke_command_line(tmp_path, capsys):
    src, out = tmp_path / "fx.json", tmp_path / "fx2.json"
    src.write_text(json.dumps(_fixture()))
    assert labels.main(["revoke", str(src), "--approved-by", "tl", "--reason", "stub", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["fixture_version"] == "1.4.0"
    assert json.loads(capsys.readouterr().out)["revoked"][0]["sample_id"] == "t9b-002"


# ── scripts/aa1048/cli.py, local commands ──────────────────────────────────────────────────────────────────────


def test_export_fixture_rederive_and_detectors(tmp_path):
    fx = _fixture()
    lines = cli.export_fixture(fx, set_id="CL-1", writers=["t9_blog"])
    # four items share one text: their claims are exported once (under the first), t9b-003 has its own
    assert {ln["refs"]["sample_id"] for ln in lines} == {"t9b-001", "t9b-003"}
    assert len({ln["claim_hash"] for ln in lines}) == len(lines)
    assert all(ln["scope"] == "tenant" and ln["tenant_id"] == TENANT for ln in lines)
    assert "Source itinerary" in lines[0]["source_text"]
    assert cli.export_fixture(fx, set_id="CL-1", writers=["a1"]) == []
    export = tmp_path / "claims.jsonl"
    export.write_text(cli._jsonl(lines))
    bad = next(ln for ln in lines if "40 km" in ln["claim_text"])
    finals = [{"claim_hash": ln["claim_hash"], "claim_class": "fact.number",
               "label": "violation" if ln is bad else "ok", "source": "tech_lead", "writer": "t9_blog",
               "sample_id": ln["refs"]["sample_id"], "content_sha256": ln["content_sha256"],
               "claim_text": ln["claim_text"]} for ln in lines]
    fin = tmp_path / "finals.jsonl"
    fin.write_text(cli._jsonl(finals))
    fxp = tmp_path / "fx.json"
    fxp.write_text(json.dumps(fx))
    re = cli.cmd_rederive(cli.build_parser().parse_args(["rederive", str(fxp), "--finals", str(fin)]))
    # t9b-003 keeps its fail (the 40 km claim); t9b-005 was labelled fail with no violating claim: it flips to pass.
    # t9b-001 / -002 / -004 / -005 share one text: the claims reach all four through the content sha.
    assert [(f["sample_id"], f["to"]) for f in re["flips"]] == [("t9b-005", "pass")] and re["unchanged"] == 4
    finals[0]["label"] = "violation"                     # the shared text now has a violation: its pass items flip
    fin.write_text(cli._jsonl(finals))
    re = cli.cmd_rederive(cli.build_parser().parse_args(["rederive", str(fxp), "--finals", str(fin)]))
    assert [f["sample_id"] for f in re["flips"]] == ["t9b-001", "t9b-002", "t9b-004"]
    rows_out, th = tmp_path / "rows.json", tmp_path / "rows.yaml"
    a = cli.build_parser().parse_args(["detectors", "--export", str(export), "--finals", str(fin), "--out",
                                       str(rows_out), "--thresholds", str(th), "--only",
                                       "novel_numbers,F1_grounding", "--split", "all"])
    res = cli.cmd_detectors(a)
    rows = json.loads(rows_out.read_text())
    assert res["rows"] == 2 and {r["detector"] for r in rows} == {"novel_numbers", "F1_grounding"}
    assert all(r["tp"] == 1 and r["split"] == "all" for r in rows)
    assert not any(r["passes"] for r in rows) and res["enforce"] == []      # a pooled split never passes
    assert "det.novel_numbers.fact.number" in th.read_text()
    # the default split is held_out: only the claims whose group key hashes there are measured
    held = cli.cmd_detectors(cli.build_parser().parse_args(
        ["detectors", "--export", str(export), "--finals", str(fin), "--out", str(rows_out), "--only",
         "novel_numbers"]))
    assert held["split"] == "held_out"
    assert held["claims"] == sum(1 for ln in lines if ln["split"] == "held_out")


def test_agreement_map_reads_report_output_or_a_plain_map():
    report = {"ok": True, "result": {"agreement": {"classes": {"fact.number": {"claims": 4, "agreement": 0.75}},
                                                   "audit": {}}}}
    assert cli.agreement_map(report) == {"fact.number": 0.75}
    assert cli.agreement_map(report["result"]["agreement"]) == {"fact.number": 0.75}
    assert cli.agreement_map({"fact.name": 0.9, "fact.number": None}) == {"fact.name": 0.9, "fact.number": None}
    with pytest.raises(ValueError):
        cli.agreement_map({"fact.name": "high"})


def test_outputs_are_refused_inside_a_work_tree_unless_ignored(tmp_path):
    import subprocess

    from scripts.aa1048 import outpath

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("scratch/\n")
    with pytest.raises(outpath.OutRefused, match="not ignored"):
        outpath.check_out(str(repo / "claims.jsonl"))
    with pytest.raises(outpath.OutRefused):
        outpath.check_out(str(repo / "new" / "deeper" / "rows.json"))        # a folder that does not exist yet
    assert outpath.check_out(str(repo / "scratch" / "claims.jsonl")).endswith("claims.jsonl")
    outside = tmp_path / "plain"
    outside.mkdir()
    assert outpath.check_out(str(outside / "claims.jsonl")) == str(outside / "claims.jsonl")
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(_fixture()))
    assert cli.main(["export-fixture", str(fx), "--set", "CL-1", "--out", str(repo / "claims.jsonl")]) == 2
    assert not (repo / "claims.jsonl").exists()


def test_mutations_and_sample_size_commands(tmp_path, capsys):
    out = tmp_path / "m.json"
    res = cli.cmd_mutations(cli.build_parser().parse_args(["mutations", "--texts", "2", "--out", str(out)]))
    assert res["cases"] == 4 * 2 * 12 and json.loads(out.read_text())["recall"]
    md = cli.cmd_sample_size(cli.build_parser().parse_args(["sample-size", "--markdown"]))
    assert md.startswith("| Bar |")
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps([{"detector": "x", "class": "fact.number", "fire_rate": 0.5,
                                  "violation_rate": 0.5, "precision": 1.0}]))
    got = cli.cmd_sample_size(cli.build_parser().parse_args(["sample-size", "--rates", str(rates), "--budget", "5"]))
    assert got[0]["status"].startswith("stays warn")
    assert cli.main(["sample-size", "--markdown"]) == 0
    assert "| Bar |" in capsys.readouterr().out


def test_main_reports_a_refusal(tmp_path, capsys):
    export, panel = tmp_path / "e.jsonl", tmp_path / "p.jsonl"
    export.write_text("")
    panel.write_text("{oops\n")
    # rederive with a broken finals file is a local refusal (exit 2), never a crash
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(_fixture()))
    assert cli.main(["rederive", str(fx), "--finals", str(panel)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
