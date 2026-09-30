"""AA-701 — Jev questions next to the T10 gates (observe only, allow-listed tenants)."""
import asyncio
from unittest.mock import patch

from services.acp_content_writing import jev_observe as jo
from shared.llm_client.decide import Decision, Verdict

OFFERED_ATOM = "Optional: hot stone bath at extra cost."
BLOG = ("## Paro\nYou soak in a hot stone bath after the hike [R:atom_1].\n\n"
        "## FAQ\n**Q: Is the bath included?**\nA: It is an optional extra you can add.\n")


def _asks(**kw):
    base = dict(content_text=BLOG, atom_text=OFFERED_ATOM, goal_key="inspire", cta="Design This Journey",
                brand_rubric_text="Quiet luxury, restrained.", channel="blog")
    base.update(kw)
    return jo.build_asks(**base)


def test_build_asks_covers_each_question_and_strips_tags():
    asks = _asks()
    keys = [a[0] for a in asks]
    assert jo.Q_RUBRIC in keys and jo.Q_VOICE in keys and jo.Q_CTA in keys
    assert keys.count(jo.Q_FAQ) == 1
    assert all("[R:" not in str(a[2]) for a in asks)


def test_offer_question_per_promised_sentence():
    text = "You soak in a hot stone bath after the hike. Then you rest by the fire."
    offers = [a for a in _asks(content_text=text, channel="facebook") if a[0] == jo.Q_OFFER]
    assert [a[2]["sentence"] for a in offers] == ["You soak in a hot stone bath after the hike.",
                                                  "Then you rest by the fire."]
    assert offers[0][2]["offered_moment"] == OFFERED_ATOM


def test_no_faq_question_outside_blog_and_no_cta_question_without_cta():
    keys = [a[0] for a in _asks(channel="facebook", cta="")]
    assert jo.Q_FAQ not in keys and jo.Q_CTA not in keys


def _run(zone_first="grey", other_allowed=True, similarity=0.9):
    calls = []

    async def fake(stage, key, state, qs, **kw):
        calls.append(qs[0])
        return Decision(verdicts={qs[0]: Verdict(qs[0], "shadow", zone_first if len(calls) == 1 else "grey")})

    class Pool:
        pass

    async def other_text(pool, pid):
        return "Another tenant's post about the hot stone bath."

    with patch("shared.llm_client.decide.decide", fake), \
         patch("shared.llm_client.decide.tenant_allowed", lambda t: other_allowed), \
         patch.object(jo, "_other_piece_text", other_text):
        out = asyncio.run(jo.observe_t10(
            tenant_id="t-1", attempt_key="r:1", content_text=BLOG, atom_text=OFFERED_ATOM, goal_key="inspire",
            cta="Design This Journey", brand_rubric_text="Quiet luxury.", channel="blog",
            nearest_other={"piece_id": "p-2", "tenant_id": "t-2", "similarity": similarity}, pool=Pool()))
    return out, calls


def test_observe_asks_all_and_same_piece_when_other_tenant_allowed():
    out, calls = _run()
    assert calls[-1] == jo.Q_SAME and out["asked"] == len(calls)


def test_same_piece_not_asked_when_other_tenant_not_allowed_or_far():
    _, calls = _run(other_allowed=False)
    assert jo.Q_SAME not in calls
    _, calls = _run(similarity=0.80)
    assert jo.Q_SAME not in calls


def test_skipped_tenant_stops_after_one_lookup():
    out, calls = _run(zone_first="skipped")
    assert len(calls) == 1 and out == {"asked": 0, "skipped": True}


def test_no_tenant_asks_nothing_and_errors_never_raise():
    assert asyncio.run(jo.observe_t10(tenant_id=None, attempt_key="x", content_text=BLOG, atom_text="",
                                      goal_key="inspire", cta=None, brand_rubric_text="", channel="blog")) \
        == {"asked": 0, "skipped": True}

    async def boom(*a, **k):
        raise RuntimeError("x")
    with patch("shared.llm_client.decide.decide", boom):
        out = asyncio.run(jo.observe_t10(tenant_id="t", attempt_key="x", content_text=BLOG, atom_text="",
                                         goal_key="inspire", cta="c", brand_rubric_text="b", channel="blog"))
    assert out["asked"] == 0
