"""AA-845 — the a1_claim_supported ground-truth loop (option c, S245).

Covers:
  - record_outcome(keep_sources=, by=) / clear_outcome(): the SQL each sends, the JSON written,
    never raising;
  - grounding.review_units(): the subject keys equal what the S1 grounding run asks Jev with
    (subject_key(unit_source(tour, unit), sentence)), with the source itinerary split once;
  - tour_from_row / generated_from_row: same dicts as _execute_run_tour builds (JSON-string jsonb);
  - record_approved_outcome keeps a reviewer's "unsupported"; record_rejected_outcome is per sentence;
  - GET /admin/review-queue/{id}/claims and POST …/claims/label against a fake pool;
  - GET /admin/decisions/claim-sample stratification (seeded, per-tour cap).
Mock-only: no TypeSafe call, no DB."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from shared.llm_client import decide as d

# A real-shaped tour (Bhutan, 3 days), as raw_tours returns it: src_highlights is a JSON string.
RAW_ROW = {
    "src_name": "Bhutan Cultural Journey",
    "src_subtitle": "Paro, Thimphu and Punakha",
    "src_summary": "Three days across western Bhutan's valleys and dzongs.",
    "src_description": "A short cultural tour of western Bhutan.",
    "src_highlights": json.dumps(["Taktsang Monastery", "Punakha Dzong"]),
    "src_itineraries": (
        "Day 1: Arrive Paro\nArrive at Paro airport and transfer to Thimphu. Visit the Memorial Chorten.\n"
        "Day 2: Thimphu to Punakha\nDrive over Dochula Pass (3,100 m) to Punakha. Visit Punakha Dzong.\n"
        "Day 3: Paro\nHike to Taktsang Monastery. Depart in the evening."
    ),
    "country": "Bhutan",
    "duration": "3 days",
    "price_raw": "USD 1,200",
    "inclusions": "Hotels, guide, all meals",
    "exclusions": "International flights",
    "aa_subtitle": "Three days of dzongs and mountain passes",
    "aa_summary": "A short journey through Paro, Thimphu and Punakha. Each day ends at a riverside lodge.",
    "aa_highlights": json.dumps(["Hike up to Taktsang Monastery above Paro", "Punakha Dzong at the river fork"]),
    "aa_itineraries": (
        "Day 1 — Arrival in Paro\nLand in Paro and drive to Thimphu. Pay respects at the Memorial Chorten.\n"
        "Day 2 — Over Dochula\nCross Dochula Pass at 3,100 metres on the way to Punakha. "
        "Walk through Punakha Dzong in the afternoon.\n"
        "Day 3 — Tiger's Nest\nClimb to Taktsang Monastery before an evening departure."
    ),
}


class _Conn:
    def __init__(self, tag="UPDATE 3", fail=False):
        self.tag, self.fail, self.calls = tag, fail, []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        return self.tag


# ── record_outcome / clear_outcome ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_keep_sources_uses_guarded_update_and_adds_by():
    conn = _Conn("UPDATE 2")
    n = await d.record_outcome(conn, "a1_claim_supported", "claim:a:b", truth=True, source="review_approve",
                               keep_sources=("review_sentence_unsupported",), by="u-1")
    assert n == 2
    sql, args = conn.calls[0]
    assert "<> ALL($4::text[])" in sql and "outcome IS NULL" in sql
    assert args[3] == ["review_sentence_unsupported"]
    payload = json.loads(args[2])
    assert payload["truth"] is True and payload["by"] == "u-1"


@pytest.mark.asyncio
async def test_record_outcome_without_keep_sources_is_the_plain_update_and_no_by_key():
    conn = _Conn("UPDATE 1")
    await d.record_outcome(conn, "q", "s", truth=False, source="src")
    sql, args = conn.calls[0]
    assert len(args) == 3 and "ALL(" not in sql
    assert "by" not in json.loads(args[2])


@pytest.mark.asyncio
async def test_clear_outcome_only_clears_the_named_source_and_never_raises():
    conn = _Conn("UPDATE 4")
    assert await d.clear_outcome(conn, "a1_claim_supported", "claim:a:b", "review_sentence_unsupported") == 4
    sql, args = conn.calls[0]
    assert "SET outcome = NULL" in sql and "->> 'source' = $3" in sql
    assert args == ("a1_claim_supported", "claim:a:b", "review_sentence_unsupported")
    assert await d.clear_outcome(_Conn(fail=True), "q", "s", "src") == 0


# ── grounding builders ──────────────────────────────────────────────────────────────────────────────

def test_rows_build_the_same_dicts_as_the_s1_run():
    from services.content_generation import grounding as gr

    tour = gr.tour_from_row(RAW_ROW)
    assert tour["highlights"] == ["Taktsang Monastery", "Punakha Dzong"]     # JSON string parsed
    assert tour["price"] == "USD 1,200" and set(tour) == set(gr.SOURCE_FIELDS)
    gen = gr.generated_from_row(RAW_ROW)
    assert gen["highlights"][0].startswith("Hike up") and set(gen) == set(gr.GROUNDED_FIELDS)
    assert gr.generated_from_row({**RAW_ROW, "aa_highlights": None})["highlights"] == []


def test_review_units_keys_match_the_grounding_run_byte_for_byte():
    from services.content_generation import grounding as gr

    tour, gen = gr.tour_from_row(RAW_ROW), gr.generated_from_row(RAW_ROW)
    units = gr.review_units(tour, gen)
    plain = gr.sentence_units(gen)
    assert len(units) == len(plain) and any(u["day"] == 2 for u in units)
    for u, p in zip(units, plain):
        # What check_grounding/_judge_all asks: subject_key(unit_source(tour, u), sentence).
        assert u["subject_key"] == gr.subject_key(gr.unit_source(tour, p), p["sentence"])
    # A day-2 unit really gets a per-day source (not the whole tour), so the pre-split path ran.
    day2 = next(u for u in units if u["day"] == 2)
    assert "DAY 2:" in gr.unit_source(tour, day2) and gr.unit_source(tour, day2) != gr.source_text(tour)


def test_review_units_empty_without_source_or_content():
    from services.content_generation import grounding as gr
    assert gr.review_units({}, gr.generated_from_row(RAW_ROW)) == []
    assert gr.review_units(gr.tour_from_row(RAW_ROW), {}) == []


@pytest.mark.asyncio
async def test_approve_keeps_reviewer_unsupported_and_reject_is_per_sentence(monkeypatch):
    from services.content_generation import grounding as gr

    seen = []

    async def fake_record(conn, qkey, skey, *, truth, source, ref=None, keep_sources=(), by=None):
        seen.append(dict(q=qkey, s=skey, truth=truth, source=source, ref=ref, keep=keep_sources, by=by))
        return 1

    monkeypatch.setattr(d, "record_outcome", fake_record)
    tour, gen = gr.tour_from_row(RAW_ROW), gr.generated_from_row(RAW_ROW)
    n = await gr.record_approved_outcome(object(), tour, gen)
    assert n == len(gr.review_units(tour, gen))
    assert all(x["truth"] is True and x["keep"] == (gr.REVIEW_UNSUPPORTED_SOURCE,) for x in seen)

    seen.clear()
    assert await gr.record_rejected_outcome(object(), "claim:x:y", ref="rq-1", by="u-9") == 1
    assert seen == [dict(q="a1_claim_supported", s="claim:x:y", truth=False,
                         source="review_sentence_unsupported", ref="rq-1", keep=(), by="u-9")]


@pytest.mark.asyncio
async def test_latest_verdicts_parses_outcome_and_skips_empty_keys():
    from services.content_generation import grounding as gr

    class P:
        async def fetch(self, sql, questions, keys):
            assert "DISTINCT ON (subject_key, question_key)" in sql
            assert keys == ["k1"]
            return [{"subject_key": "k1", "question_key": "a1_claim_supported", "zone": "grey", "mode": "enforce",
                     "probability": 0.31, "created_at": None,
                     "outcome": json.dumps({"truth": False, "source": "review_sentence_unsupported"})}]

    out = await gr.latest_verdicts(P(), ["k1", "", None, "k1"])
    assert out[("a1_claim_supported", "k1")]["outcome"]["truth"] is False
    assert await gr.latest_verdicts(P(), []) == {}


# ── endpoints ───────────────────────────────────────────────────────────────────────────────────────

REVIEW_ID = "3f1c0d9e-6a59-4c1e-9d0b-2f8a7c1d2e31"


class _Pool:
    """fetchrow → the review row; fetch → decision_log verdict rows; execute → outcome writes."""

    def __init__(self, verdict_keys=(), outcome_for=None):
        self.verdict_keys = set(verdict_keys)
        self.outcome_for = outcome_for or {}
        self.executed = []

    async def fetchrow(self, sql, *args):
        return {**RAW_ROW, "tour_id": "t-1", "generated_content_id": "gc-1", "review_status": "pending",
                "human_edited": False}

    async def fetch(self, sql, questions, keys):
        return [{"subject_key": k, "question_key": q, "zone": "grey", "mode": "enforce", "probability": 0.3,
                 "created_at": None, "outcome": self.outcome_for.get(k)}
                for k in keys if k in self.verdict_keys for q in questions]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _req(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture
def _no_secret(monkeypatch):
    import api.routers.admin_pipeline as ap
    monkeypatch.setattr(ap, "verify_admin_secret", lambda *_: None)
    return ap


def _keys():
    from services.content_generation import grounding as gr
    return [u["subject_key"] for u in gr.review_units(gr.tour_from_row(RAW_ROW), gr.generated_from_row(RAW_ROW))]


@pytest.mark.asyncio
async def test_claims_lists_every_unit_with_its_verdict_and_label(_no_secret):
    keys = _keys()
    pool = _Pool(verdict_keys=keys[:2], outcome_for={
        keys[0]: json.dumps({"truth": False, "source": "review_sentence_unsupported", "by": "u-1"})})
    out = await _no_secret.admin_review_claims(REVIEW_ID, _req(pool), x_admin_secret="x")
    assert len(out["claims"]) == len(keys) and out["with_verdict"] == 2 and out["marked_unsupported"] == 1
    first = out["claims"][0]
    assert first["marked_unsupported"] and first["label"]["by"] == "u-1"
    assert first["verdict"]["probability"] == 0.3 and first["promise_probability"] == 0.3
    assert out["claims"][2]["verdict"] is None


@pytest.mark.asyncio
async def test_claims_404_on_a_bad_review_id(_no_secret):
    with pytest.raises(HTTPException) as e:
        await _no_secret.admin_review_claims("not-a-uuid", _req(_Pool()), x_admin_secret="x")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_label_marks_one_sentence_false_and_undo_clears_only_the_reviewer_label(_no_secret):
    ap = _no_secret
    keys = _keys()
    pool = _Pool(verdict_keys=keys)
    user = "8b0e7d4c-1f2a-4b3c-9d8e-7f6a5b4c3d2e"
    out = await ap.admin_label_review_claim(REVIEW_ID, ap.ClaimLabelBody(subject_key=keys[1]), _req(pool),
                                            x_admin_secret="x", x_admin_user_id=user)
    assert out["rows_updated"] == 1 and out["unsupported"] is True
    sql, args = pool.executed[-1]
    payload = json.loads(args[2])
    assert args[1] == keys[1] and payload == {**payload, "truth": False, "source": "review_sentence_unsupported",
                                              "ref": REVIEW_ID, "by": user}

    await ap.admin_label_review_claim(REVIEW_ID, ap.ClaimLabelBody(subject_key=keys[1], unsupported=False),
                                      _req(pool), x_admin_secret="x", x_admin_user_id=None)
    sql, args = pool.executed[-1]
    assert "SET outcome = NULL" in sql and args[2] == "review_sentence_unsupported"


@pytest.mark.asyncio
async def test_label_refuses_a_foreign_sentence_and_one_without_a_verdict(_no_secret):
    ap = _no_secret
    keys = _keys()
    pool = _Pool(verdict_keys=keys[:1])
    with pytest.raises(HTTPException) as e:
        await ap.admin_label_review_claim(REVIEW_ID, ap.ClaimLabelBody(subject_key="claim:0000000000:000000000000"),
                                          _req(pool), x_admin_secret="x", x_admin_user_id=None)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await ap.admin_label_review_claim(REVIEW_ID, ap.ClaimLabelBody(subject_key=keys[1]), _req(pool),
                                          x_admin_secret="x", x_admin_user_id=None)
    assert e.value.status_code == 409
    assert pool.executed == []                                   # nothing written on a refusal


def test_reject_route_writes_no_outcome():
    """S245 scope change: a whole-version reject must not stamp truth=False on every sentence."""
    import inspect

    import api.routers.admin_pipeline as ap
    src = inspect.getsource(ap.admin_reject_review)
    assert "record_outcome" not in src and "record_rejected_outcome" not in src


# ── claim-sample stratification ────────────────────────────────────────────────────────────────────

def test_pick_stratum_is_seeded_and_caps_per_tour():
    from api.routers.admin_decisions import pick_stratum

    cands = [{"tour_id": f"t{i % 3}", "subject_key": f"claim:{i}"} for i in range(30)]
    a = pick_stratum(cands, 10, "s1", 2, {})
    b = pick_stratum(list(reversed(cands)), 10, "s1", 2, {})
    assert [c["subject_key"] for c in a] == [c["subject_key"] for c in b]      # order-independent
    assert len(a) == 6                                                            # 3 tours × cap 2
    taken = {"t0": 2}
    assert all(c["tour_id"] != "t0" for c in pick_stratum(cands, 10, "s1", 2, taken))


@pytest.mark.asyncio
async def test_claim_sample_splits_low_and_mid_and_returns_the_unit_source(monkeypatch):
    import api.routers.admin_decisions as ad
    from services.content_generation import grounding as gr

    monkeypatch.setattr(ad, "verify_admin_secret", lambda *_: None)
    keys = _keys()
    probs = {k: (0.05 if i % 2 == 0 else 0.5) for i, k in enumerate(keys)}

    class P:
        async def fetch(self, sql, *args):
            if "generated_content" in sql:
                return [{**RAW_ROW, "tour_id": "t-1", "generated_content_id": "gc-1", "version_num": 2}]
            questions, ks = args
            return [{"subject_key": k, "question_key": q, "zone": "grey", "mode": "enforce",
                     "probability": probs[k], "created_at": None, "outcome": None}
                    for k in ks if k in probs for q in questions]

    out = await ad.claim_sample(_req(P()), n=4, low_share=0.5, low_max=0.2, high_max=0.85, tours=10,
                                per_tour=10, seed="aa845", country=None, x_admin_secret="x")
    assert out["population"]["units"] == len(keys)
    assert [i["stratum"] for i in out["items"]] == ["low", "low", "mid", "mid"]
    item = out["items"][0]
    unit = next(u for u in gr.review_units(gr.tour_from_row(RAW_ROW), gr.generated_from_row(RAW_ROW))
                if u["subject_key"] == item["subject_key"])
    assert item["source"] == gr.unit_source(gr.tour_from_row(RAW_ROW), unit)
    assert "_tour" not in item and isinstance(item["numeric_hit"], bool)

    with pytest.raises(HTTPException):
        await ad.claim_sample(_req(P()), n=4, low_share=0.5, low_max=0.9, high_max=0.85, tours=10,
                              per_tour=10, seed="aa845", country=None, x_admin_secret="x")
