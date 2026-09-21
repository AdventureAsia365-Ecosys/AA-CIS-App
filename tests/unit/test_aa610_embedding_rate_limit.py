"""AA-610 (Sub 2): services.acp_shared.content_embedding._pace_calls() — this account's real
Bedrock quota for Cohere Embed v4 is 20 requests/minute (confirmed live via `aws service-quotas
list-service-quotas`), not a theoretical ceiling. A live re-atomize test hit ThrottlingException
on effectively every call once volume rose past a handful of atoms, because nothing paced calls
to that ceiling. Mocks time.monotonic()/time.sleep() — no real waiting in this test suite."""
from unittest.mock import patch

from services.acp_shared import content_embedding


def _reset_pacing():
    content_embedding._last_call_at = 0.0


def test_first_call_never_waits():
    _reset_pacing()
    with patch("services.acp_shared.content_embedding.time.monotonic", return_value=100.0), \
         patch("services.acp_shared.content_embedding.time.sleep") as m_sleep:
        content_embedding._pace_calls()
    m_sleep.assert_not_called()


def test_call_too_soon_after_previous_waits_the_remainder():
    _reset_pacing()
    content_embedding._last_call_at = 100.0
    # 1.0s has passed since the last call; the pacing floor is 3.5s, so this call must wait
    # exactly the remaining 2.5s — not the full 3.5s (that would double-pace), not 0 (that would
    # ignore the floor entirely).
    with patch("services.acp_shared.content_embedding.time.monotonic", return_value=101.0), \
         patch("services.acp_shared.content_embedding.time.sleep") as m_sleep:
        content_embedding._pace_calls()
    m_sleep.assert_called_once()
    (waited,), _ = m_sleep.call_args
    assert abs(waited - 2.5) < 1e-9


def test_call_after_the_floor_has_already_elapsed_never_waits():
    _reset_pacing()
    content_embedding._last_call_at = 100.0
    # 5s has already passed since the last call — comfortably past the 3.5s floor.
    with patch("services.acp_shared.content_embedding.time.monotonic", return_value=105.0), \
         patch("services.acp_shared.content_embedding.time.sleep") as m_sleep:
        content_embedding._pace_calls()
    m_sleep.assert_not_called()


def test_pace_calls_updates_last_call_at():
    _reset_pacing()
    with patch("services.acp_shared.content_embedding.time.monotonic", return_value=200.0), \
         patch("services.acp_shared.content_embedding.time.sleep"):
        content_embedding._pace_calls()
    assert content_embedding._last_call_at == 200.0


def test_compute_embedding_calls_pace_before_invoking_bedrock():
    # AA-610 (Sub 2) — the pacing gate must run BEFORE the network call, not after (pacing
    # after a throttled call already happened is too late to have prevented it).
    _reset_pacing()
    call_order = []
    with patch("services.acp_shared.content_embedding._pace_calls",
               side_effect=lambda: call_order.append("pace")), \
         patch("services.acp_shared.content_embedding._client") as m_client:
        m_client.return_value.invoke_model.side_effect = lambda **_: call_order.append("invoke") or {
            "body": __import__("io").BytesIO(
                b'{"embeddings": {"float": [[' + b",".join([b"0.1"] * 1536) + b']]}}',
            ),
        }
        content_embedding.compute_embedding("some text")
    assert call_order == ["pace", "invoke"]
