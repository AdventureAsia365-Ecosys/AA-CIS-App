"""AA-1048 (Quality W2) — claim identity (services/ground_truth/claims.py), the policy seam (policy.py) and the panel
format, consolidation, audit draw, final labels, contradictions, agreement, verdict_rule, rederive and manifest
(panel.py). Invented names and texts only."""
import hashlib
import json

import pytest

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import policy

TENANT = "00000000-0000-4000-8000-000000a10481"
OUTPUT = {"content_text": "Arrive in Velmora and settle in at the Harbour Lodge. On the second day you walk 12 km "
                          "to Ostrin Valley. Breakfast is included every day.\n\nAsk us about the Velmora trip.",
          "meta_description": "A slow walking week from Velmora to Ostrin Valley with lake days and village stays.",
          "slug": "velmora-walk"}
SOURCE = ("Day 1: Arrive in Velmora. Transfer to the Harbour Lodge. Dinner included.\n"
          "Day 2: Walk 12 km to Ostrin Valley. Optional: kayak on Brannoch Lake.")


# ── policy seam ────────────────────────────────────────────────────────────────────────────────────────────────


def test_policy_seam_reads_the_taxonomy_and_its_sha():
    from services.quality import registry

    classes = policy.policy_classes()
    assert policy.POLICY_SOURCE == "taxonomy"
    assert set(classes) == set(registry.classes())
    assert classes["fact.inclusion"].fails_item and not classes["other.f6"].fails_item
    assert "t9_blog" in classes["fact.number"].writers and "a1" in classes["fact.number"].writers
    with open(registry.TAXONOMY_PATH, "rb") as fh:
        assert policy.policy_sha() == hashlib.sha256(fh.read()).hexdigest()
    assert policy.known_class("fact.name") and not policy.known_class("trip_promise")


# ── claims ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_claim_hash_is_stable_and_normalised():
    sha = cl.content_sha(OUTPUT)
    assert cl.valid_hex64(sha)
    a = cl.claim_hash(sha, "content_text", "Breakfast is  included\n every day.")
    b = cl.claim_hash(sha, "content_text", "breakfast is included every day.")
    assert a == b
    assert a != cl.claim_hash(sha, "meta_description", "breakfast is included every day.")
    assert cl.normalise("It’s “quiet”") == "it's \"quiet\""


def test_split_is_frozen_per_object_and_near_the_held_out_share():
    shas = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(2000)]
    splits = [cl.split_of(s) for s in shas]
    assert splits == [cl.split_of(s) for s in shas]
    share = splits.count("held_out") / len(splits)
    assert 0.27 < share < 0.33


def test_split_group_puts_every_object_of_one_tour_on_one_side():
    assert cl.split_group({"tour_id": "tour-7", "sample_id": "t9b-1"}, "a" * 64) == "tour_id:tour-7"
    assert cl.split_group({"sample_id": "t9b-1", "piece_id": "p-1"}, "a" * 64) == "sample_id:t9b-1"
    assert cl.split_group({"version_id": "v-3"}, "a" * 64) == "version_id:v-3"
    assert cl.split_group({"tour_id": " ", "piece_id": None}, "b" * 64) == "content:" + "b" * 64
    assert cl.split_group(None, "c" * 64) == "content:" + "c" * 64
    # a blog, a social piece and a regenerated blog of one tour (different texts, so different content shas)
    outputs = [OUTPUT, {**OUTPUT, "content_text": "A short social line about Velmora."},
               {**OUTPUT, "content_text": OUTPUT["content_text"] + " Regenerated."}]
    for tour in ("tour-1", "tour-2", "tour-3", "tour-4"):
        sides = {ln["split"] for i, out in enumerate(outputs)
                 for ln in pn.export_records(set_id="CL-1", writer="t9_blog", scope="tenant", tenant_id=TENANT,
                                             refs={"piece_id": f"p-{i}", "tour_id": tour}, output=out,
                                             source_text=SOURCE)}
        assert len(sides) == 1, tour


def test_sentence_spans_and_claims_of():
    text = 'One "quoted line." Then a second sentence here! Day 2\nA third line with words.'
    spans = cl.sentence_spans(text)
    got = [text[s:e] for s, e in spans]
    assert got == ['One "quoted line."', "Then a second sentence here!", "A third line with words."]
    claims = cl.claims_of(OUTPUT)
    fields = dict(cl.field_texts(OUTPUT))
    assert [c.field for c in claims][:3] == ["content_text"] * 3
    for c in claims:
        assert cl.check_span(fields[c.field], c.span_start, c.span_end, c.claim_text)
        assert c.claim_hash == cl.claim_hash(c.content_sha256, c.field, c.claim_text)
    assert not cl.check_span(fields["content_text"], 0, 5, "nope!")
    dup = {"body": "Walk to the falls today. Walk to the falls today."}
    assert len(cl.claims_of(dup)) == 1


def test_field_texts_order_lists_and_nested():
    out = cl.field_texts({"zeta": "Last field text", "highlights": ["One item", {"k": "Two"}], "title": "First",
                          "nested": {"b": "inner b", "a": "inner a"}, "Bad Key": "skipped", "empty": " "})
    assert [f for f, _ in out] == ["title", "highlights", "nested.a", "nested.b", "zeta"]
    assert dict(out)["highlights"] == "One item\nTwo"
    assert cl.field_texts("plain body text") == [("body", "plain body text")] and cl.field_texts(3) == []


def test_align_returns_overlapping_source_lines_only():
    got = cl.align("You walk 12 km to Ostrin Valley.", SOURCE)
    assert got[0].startswith("Day 2") and len(got) <= 3
    assert cl.align("Totally unrelated words", SOURCE) == []
    assert cl.align("the and", SOURCE) == []


def test_validators():
    assert cl.valid_set("CL-1", "1.0.0") and cl.valid_set("t2@2", "0.1.12")
    assert not cl.valid_set("-x", "1.0.0") and not cl.valid_set("CL-1", "1.0")
    assert cl.valid_field("content_text") and not cl.valid_field("Content")


# ── panel export / import ──────────────────────────────────────────────────────────────────────────────────────


def _export():
    return pn.export_records(set_id="CL-1", writer="t9_blog", scope="tenant", tenant_id=TENANT,
                             refs={"sample_id": "t9b-901", "piece_id": None}, output=OUTPUT, source_text=SOURCE,
                             stratum="hard")


def test_export_records_carry_the_full_source_and_evidence():
    lines = _export()
    assert len(lines) == len(cl.claims_of(OUTPUT))
    first = lines[0]
    assert first["source_text"] == SOURCE and first["refs"] == {"sample_id": "t9b-901"}
    assert first["stratum"] == "hard" and first["split"] in cl.SPLITS
    assert first["split_group"] == "sample_id:t9b-901" and first["split"] == cl.split_of("sample_id:t9b-901")
    assert "fact.inclusion" in first["candidate_classes"]
    assert first["field_text"] == OUTPUT["content_text"]
    assert any("Day 2" in ev for ln in lines for ev in ln["evidence"])
    s, e = first["span"]
    assert first["field_text"][s:e] == first["claim_text"]


@pytest.mark.parametrize("kw,msg", [
    ({"writer": "t5"}, "writer"), ({"stratum": "mixed"}, "stratum"), ({"scope": "platform"}, "scope"),
    ({"refs": {"tour_id": "x"}}, "sample_id"),
])
def test_export_records_refuse(kw, msg):
    args = dict(set_id="CL-1", writer="t9_blog", scope="tenant", tenant_id=TENANT, refs={"sample_id": "t9b-1"},
                output=OUTPUT, source_text=SOURCE)
    args.update(kw)
    with pytest.raises(pn.PanelError, match=msg):
        pn.export_records(**args)


def _judge(h, lens, label="violation", cls="fact.inclusion", kind="agent_panel"):
    row = {"claim_hash": h, "class": cls, "label": label, "reason": f"{lens} reason", "labeller": "panel:zz"}
    if kind == "agent_panel":
        row["lens"] = lens
    else:
        row["labeller_kind"] = "tech_lead"
    return row


def test_parse_judgements_refuses_the_whole_file():
    lines = _export()
    by = {e["claim_hash"]: e for e in lines}
    h = lines[0]["claim_hash"]
    ok = pn.parse_judgements([_judge(h, "source"), _judge(h, "promise", "ok")], export_by_hash=by)
    assert [j.lens for j in ok] == ["source", "promise"]
    for bad, msg in ((_judge("f" * 64, "source"), "not in the export"), (_judge(h, "eyes"), "lens"),
                     ({**_judge(h, "source"), "class": "trip_promise"}, "not a policy class"),
                     ({**_judge(h, "source"), "label": "fail"}, "label"),
                     ({**_judge(h, "source"), "labeller_kind": "robot"}, "labeller_kind")):
        with pytest.raises(pn.PanelError, match=msg):
            pn.parse_judgements([_judge(h, "promise"), bad], export_by_hash=by)
    with pytest.raises(pn.PanelError, match="second judgement"):
        pn.parse_judgements([_judge(h, "source"), _judge(h, "source", "ok")], export_by_hash=by)
    lead = pn.parse_judgements([_judge(h, None, "ok", kind="tech_lead")], export_by_hash=by)
    assert lead[0].lens is None and lead[0].labeller == "panel:zz"
    rows = pn.read_jsonl(json.dumps(_judge(h, "source")) + "\n\n")
    assert rows[0]["claim_hash"] == h
    with pytest.raises(pn.PanelError, match="not JSON"):
        pn.read_jsonl("{oops")
    with pytest.raises(pn.PanelError, match="not an object"):
        pn.read_jsonl("[1]")


def _panel_rows(lines, n_disputed=1):
    rows = []
    for i, e in enumerate(lines):
        h = e["claim_hash"]
        labels = ["violation", "violation", "ok"] if i < n_disputed else ["ok", "ok", "ok"]
        for lens, lab in zip(pn.LENSES, labels):
            rows.append(_judge(h, lens, lab, cls="fact.number"))
    return rows


def test_consolidate_states_audit_draw_and_rows():
    lines = _export()
    by = {e["claim_hash"]: e for e in lines}
    js = pn.parse_judgements(_panel_rows(lines), export_by_hash=by)
    cons = pn.consolidate(lines, js, set_id="CL-1", set_version="1.0.0", policy_sha="a" * 64)
    states = list(cons.states.values())
    assert states.count("disputed") == 1
    assert states.count("audit") == max(1, int((len(lines) - 1) * pn.AUDIT_PCT + 0.5))
    assert cons.panel_label[lines[0]["claim_hash"]] == "violation"
    assert len(cons.rows) == 3 * len(lines) and not cons.problems
    r = cons.rows[0]
    assert r["adjudication"] == "disputed" and r["lens"] == "source" and r["policy_sha"] == "a" * 64
    assert r["sample_id"] == "t9b-901" and r["tenant_id"] == TENANT and r["stratum"] == "hard"
    # reproducible draw
    again = pn.consolidate(lines, js, set_id="CL-1", set_version="1.0.0")
    assert again.states == cons.states
    # a claim with no judgement is reported, not silently dropped
    part = pn.consolidate(lines, js[:3], set_id="CL-1", set_version="1.0.0")
    assert part.problems and "no judgement" in part.problems[0]
    with pytest.raises(pn.PanelError, match="bad set"):
        pn.consolidate(lines, js, set_id="CL 1", set_version="1.0.0")
    # the split is recomputed from the group key: an export line that says otherwise refuses the import
    other = "train" if lines[0]["split"] == "held_out" else "held_out"
    with pytest.raises(pn.PanelError, match="split"):
        pn.consolidate([{**lines[0], "split": other}] + lines[1:], js, set_id="CL-1", set_version="1.0.0")
    assert {r["split"] for r in cons.rows} == {lines[0]["split"]}


def test_text_guard_flags_claim_export_and_label_lines():
    """The CI golden-set text guard knows the claim-label file formats (real T2 / T9 / S1 text never in this public
    repo, review S272): an export line, a panel line and a finals line are flagged; the brief's placeholders are not."""
    from services.golden_set import text_guard as tg

    line = _export()[0]
    assert "claim-label export marker" in tg.markers(json.dumps(line, sort_keys=True))
    panel_line = json.dumps(_judge(line["claim_hash"], "source"))
    assert tg.markers(panel_line) == ["claim-label line marker"]
    finals_line = json.dumps({"claim_class": "fact.number", "claim_hash": line["claim_hash"],
                              "claim_text": line["claim_text"], "label": "ok"})
    assert tg.markers("header\n" + finals_line + "\n") == ["claim-label line marker"]
    assert tg.markers('{"claim_hash": "<64 hex>", "lens": "source", "label": "ok"}') == []
    assert tg.markers('{"claim_hash": "' + "a" * 64 + '", "label": "ok"}') == []     # a hash alone is no text
    assert tg.markers('{"claim_hash": "' + "a" * 64 + '",\n "lens": "source"}') == []  # one line, not two


def test_text_guard_flags_pretty_printed_and_yaml_claim_files():
    """Review S272: the line marker missed any pretty-printed claim file (the CLI writes JSON with indent=1) and YAML;
    the whole-file marker (a 64-hex claim_hash value anywhere + a claim_text key anywhere) catches them."""
    import yaml

    from services.golden_set import text_guard as tg

    row = {"claim_hash": "ab" * 32, "claim_text": "Lunch at the camp.", "label": "ok"}
    assert tg.markers(json.dumps(row)) == ["claim-label line marker"]
    assert tg.markers(json.dumps(row, indent=1)) == ["claim-label file marker"]
    assert tg.markers(json.dumps([row, row], indent=2)) == ["claim-label file marker"]
    assert tg.markers(yaml.safe_dump(row)) == ["claim-label file marker"]
    assert tg.markers(yaml.safe_dump({"rows": [row]}, default_style="'")) == ["claim-label file marker"]
    assert tg.markers(json.dumps({"claim_hash": "ab" * 32, "label": "ok"}, indent=1)) == []    # no claim text
    assert tg.markers(json.dumps({"claim_hash": "<64 hex>", "claim_text": "x"}, indent=1)) == []
    assert tg.markers(json.dumps({"claim_hash": "ab" * 33, "claim_text": "x"}, indent=1)) == []  # not a sha256


def test_final_labels_release_evidence_agreement_and_contradictions():
    lines = _export()
    by = {e["claim_hash"]: e for e in lines}
    raw = _panel_rows(lines)
    h0, h1 = lines[0]["claim_hash"], lines[1]["claim_hash"]
    raw.append(_judge(h0, None, "ok", cls="fact.number", kind="tech_lead"))
    cons = pn.consolidate(lines, pn.parse_judgements(raw, export_by_hash=by), set_id="CL-1", set_version="1.0.0")
    finals = pn.final_labels(cons.rows)
    assert finals[h0].source == "tech_lead" and finals[h0].label == "ok"
    assert finals[h1].source == "panel_agreed" and finals[h1].label == "ok"
    assert set(pn.release_evidence(finals)) == {h0}
    assert set(pn.release_evidence(finals, audit_ok=True)) == set(finals)
    # an audited claim the tech lead flips counts in the panel error rate; a dispute does not
    audited = next(h for h, s in cons.states.items() if s == "audit")
    rows = cons.rows + [dict(next(r for r in cons.rows if r["claim_hash"] == audited), labeller_kind="tech_lead",
                             lens=None, label="violation", adjudication="final")]
    rep = pn.agreement_report(rows)
    assert rep["audit"] == {"audited": 1, "flipped": 1, "panel_error_rate": 1.0}
    assert rep["classes"]["fact.number"]["disputed"] == 1
    assert pn.contradictions(finals) == []
    twin = pn.FinalLabel("e" * 64, "fact.number", "violation", "tech_lead", "t9_blog", "c" * 64,
                         finals[h1].claim_text.upper())
    found = pn.contradictions({**finals, twin.claim_hash: twin})
    assert found and found[0]["labels"] == ["ok", "violation"]
    disputed_only = [r for r in cons.rows if r["claim_hash"] == h0 and r["labeller_kind"] == "agent_panel"]
    assert pn.final_labels(disputed_only)[h0].source == "none"


def test_verdict_rule_and_rederive_list_every_flip():
    f = lambda cls, lab, sid="t9b-901": pn.FinalLabel(cls + lab + sid, cls, lab, "tech_lead", "t9_blog", "c" * 64,  # noqa: E731
                                                      "x", sid)
    assert pn.verdict_rule([f("fact.number", "ok"), f("other.f6", "violation")]) == "pass"   # warn class
    assert pn.verdict_rule([f("fact.number", "violation")]) == "fail"
    assert pn.verdict_rule([f("fact.number", "unsure")]) == "unverified"
    fixture = {"samples": [{"sample_id": "t9b-901", "label": {"verdict": "pass"}, "label_source": "agent_panel"},
                           {"sample_id": "t9b-902", "label": {"verdict": "fail"}, "label_source": "agent_panel"},
                           {"sample_id": "t9b-903", "label": {"verdict": "fail"}},
                           {"sample_id": "t9b-904", "label": {"verdict": "fail"}}]}
    finals = {x.claim_hash: x for x in (f("fact.number", "violation", "t9b-901"), f("fact.name", "violation", "t9b-902"),
                                        f("fact.number", "unsure", "t9b-903"))}
    got = pn.rederive(fixture, finals)
    assert got["flips"] == [{"sample_id": "t9b-901", "from": "pass", "to": "fail", "label_source": "agent_panel",
                             "violations": ["fact.number"]}]
    assert got["unchanged"] == 1 and got["unverified"] == ["t9b-903"] and got["not_covered"] == 1


def test_manifest_holds_no_text():
    lines = _export()
    by = {e["claim_hash"]: e for e in lines}
    cons = pn.consolidate(lines, pn.parse_judgements(_panel_rows(lines), export_by_hash=by), set_id="CL-1",
                          set_version="1.0.0", policy_sha="b" * 64)
    m = pn.manifest(cons.rows, set_id="CL-1", set_version="1.0.0")
    blob = json.dumps(m)
    assert "Velmora" not in blob and "reason" not in blob
    assert m["claims"] == len(lines) and m["objects"] == 1 and m["policy_sha"] == ["b" * 64]
    flipped = [dict(r, label="ok") if i == 0 else r for i, r in enumerate(cons.rows)]
    assert pn.manifest(flipped, set_id="CL-1", set_version="1.0.0")["digest"] != m["digest"]
