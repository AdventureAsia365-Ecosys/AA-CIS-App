"""AA-792 guard — the A1 (admin, non-tenant) writer prompts are pinned byte-for-byte.

services/content_generation/prompts.py + batch_prompt.py are shared by A1 (sync generate_node),
the Bedrock Batch manifest (materialize_s1_prompt) and T2 (tenant rewrite). AA-792 gives T2 its own
prompt; this test makes sure that change (and any later one) can never move the A1 bytes silently.
The hashes below were captured on origin/main @20a2387, BEFORE any AA-792/AA-794 prompt edit.

If an A1 prompt change is INTENDED, re-capture the hashes in the same PR and say so in the PR body
(an A1 prompt change re-baselines every A1 wave, AA-653). Never update them to make a T2 change pass.
"""
import hashlib

import pytest

from services.content_generation import batch_prompt, graph, itinerary_utils

_TOUR = {
    "name": "Bhutan Highlands Trek",
    "country": "Bhutan",
    "duration": "10 Days",
    "summary": "A trek through the Paro and Punakha valleys with dzong visits.",
    "description": "Walk between monasteries and farmhouses in western Bhutan.",
    "highlights": ["Tiger's Nest hike", "Punakha Dzong", "Dochula Pass at 3,100 m"],
    "itineraries": (
        "Day 1: Arrive in Paro, transfer to the hotel. Meals: Dinner\n"
        "Day 2: Hike to Tiger's Nest monastery and back to Paro town. (B, L, D)\n"
        "Day 3: Drive over Dochula Pass to Punakha, visit Punakha Dzong and the suspension bridge.\n"
        "Day 4: Depart from Paro."
    ),
    "inclusions": "Guide, permits, hotels",
    "exclusions": "Flights",
}
_SEO = {
    "keywords": {"top_keywords": ["bhutan trek", "tiger's nest hike", "punakha dzong tour"]},
    "people_also_ask": ["Is Bhutan expensive?", "How hard is the Tiger's Nest hike?"],
}
_FEW_SHOTS = [{"input": "Old name: Nepal Tour", "output": "Nepal: Kathmandu Valley Walks — 7 Days"}]

_UNBRANDED = {"tour": _TOUR, "seo": _SEO, "few_shots": [], "subtitle_focus": "standard"}
_BRANDED = {
    "tour": _TOUR, "seo": _SEO, "few_shots": _FEW_SHOTS, "subtitle_focus": "seo",
    "rewrite_language": "en-GB",
    "brand_system_prompt": "We are a slow-travel brand for curious walkers.",
    "brand_style_guide": "Short sentences. Concrete nouns. No superlatives.",
    "brand_forbidden_words": ["Epic", "unforgettable"],
    "brand_core_idea": "Walk slowly, see more.",
    "brand_customer_segment": "Couples 50+",
    "brand_customer_mindset": "Wants depth over box-ticking.",
    "brand_voice_examples": ["calm", "precise"],
    "brand_good_examples": "Morning mist lifts off the Paro valley as the trail climbs.",
}

# sha256 of each prompt string, captured on origin/main @20a2387 (see module docstring).
_GOLDEN = {
    "batch_unbranded_system": "739ff340e9a74bc985ab8535e84c726fb4acf5a464ac7884320fc1cdb83cd340",
    "batch_unbranded_user": "8a9bf12468c5539dcb0875aeb6bc31dd852a136b48703c1418cb43b0f5865ac2",
    "batch_branded_system": "1d74cf364afd4d6913ee7ea51c14921e5848575c51cb7d4fd68fce86a24e5693",
    "batch_branded_user": "e41fbedc24f66dc9b9093a7d6d320204e254a65289548db63e8b5318b0ab3f06",
    "sync_unbranded_system": "739ff340e9a74bc985ab8535e84c726fb4acf5a464ac7884320fc1cdb83cd340",
    "sync_unbranded_user": "8a9bf12468c5539dcb0875aeb6bc31dd852a136b48703c1418cb43b0f5865ac2",
    "sync_branded_retry_system": "1d74cf364afd4d6913ee7ea51c14921e5848575c51cb7d4fd68fce86a24e5693",
    "sync_branded_retry_user": "cfeccf409be657e771a791e06d4e42cec36cbd036a33dab5a09e0b59ee6ad87b",
    "nudge_system": "10fade9d0aff801bf5b42e528f75c1edbe93bf4d876a23c93889569c9b173ca2",
    "nudge_user": "0db7646f02eef9855cc749b00b7ef64647a77340474d24b585a1f47c4e7187d0",
}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _default_flags(monkeypatch):
    # The defaults the prompts were captured under: per-day targets ON (default), structured facts OFF.
    monkeypatch.delenv("S1_PER_DAY_TARGETS", raising=False)
    monkeypatch.delenv("S1_STRUCTURED_FACTS", raising=False)


class _Captured(Exception):
    pass


class _FakeClient:
    """Captures the LLMRequest generate_node / the nudge would send, then aborts the call."""
    requests: list = []

    def generate(self, request):
        _FakeClient.requests.append(request)
        raise _Captured()


def _sync_prompts(monkeypatch, state: dict) -> tuple[str, str]:
    _FakeClient.requests = []
    monkeypatch.setattr(graph, "LLMClient", _FakeClient)
    graph.generate_node(dict(state))  # generate_node swallows the exception (generation_failed)
    assert len(_FakeClient.requests) == 1
    req = _FakeClient.requests[0]
    return req.system_prompt, req.user_prompt


def _actual(monkeypatch) -> dict:
    out = {}
    out["batch_unbranded_system"], out["batch_unbranded_user"], _ = batch_prompt.materialize_s1_prompt(
        dict(_UNBRANDED))
    out["batch_branded_system"], out["batch_branded_user"], _ = batch_prompt.materialize_s1_prompt(
        dict(_BRANDED))
    out["sync_unbranded_system"], out["sync_unbranded_user"] = _sync_prompts(monkeypatch, _UNBRANDED)
    out["sync_branded_retry_system"], out["sync_branded_retry_user"] = _sync_prompts(
        monkeypatch, {**_BRANDED, "retry_count": 1, "feedback": "SUBTITLE_GENERIC: name a place."})
    _FakeClient.requests = []
    with pytest.raises(_Captured):
        itinerary_utils.nudge_itinerary_day(
            _FakeClient(), "Hike to Tiger's Nest monastery. (B, L, D)", "Tiger's Nest",
            "A short hike.", 60)
    out["nudge_system"] = _FakeClient.requests[0].system_prompt
    out["nudge_user"] = _FakeClient.requests[0].user_prompt
    return out


@pytest.mark.parametrize("key", sorted(_GOLDEN))
def test_a1_prompt_bytes_unchanged(monkeypatch, key):
    actual = _actual(monkeypatch)[key]
    assert _sha(actual) == _GOLDEN[key], (
        f"A1 prompt '{key}' changed. If intended, re-capture _GOLDEN and say so in the PR (AA-792 guard)."
    )


def test_sync_and_batch_attempt1_system_prompts_match(monkeypatch):
    # AA-606 contract: the sync writer and the Bedrock Batch manifest build the same system prompt.
    a = _actual(monkeypatch)
    assert a["sync_unbranded_system"] == a["batch_unbranded_system"]
    assert a["sync_unbranded_user"] == a["batch_unbranded_user"]
    assert a["sync_branded_retry_system"] == a["batch_branded_system"]
