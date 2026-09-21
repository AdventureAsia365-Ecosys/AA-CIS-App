"""AA-499 (AA-494 Decision 5) — services/acp_shared/content_embedding.py. boto3 client is
patched, same convention test_aa449_angle_gate_generate.py uses for LLMClient.

Model is Cohere Embed v4 (`us.cohere.embed-v4:0`), NOT Titan Embed — migration 041/124's own
comment assumed Titan, but a real `aws bedrock list-foundation-models` call (this build's own
live-verify) found Titan Embed is not offered on this account/region at all; Cohere Embed v4 is
the only embedding-capable model either account lists. Response shape verified live:
`{"embeddings": {"float": [[1536 floats]]}}`."""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.acp_shared import content_embedding as ce_mod
from services.acp_shared.content_embedding import (
    EMBEDDING_DIMENSIONS, compute_embedding, embedding_to_pgvector_literal,
)


def _cohere_response(vector: list[float]) -> dict:
    return {"id": "abc", "texts": ["x"], "embeddings": {"float": [vector]}, "response_type": "embeddings_by_type"}


def _boto_client_returning(payload: dict):
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    client.invoke_model.return_value = {"body": body}
    return client


@pytest.fixture(autouse=True)
def _no_real_pacing_wait():
    """AA-610 (Sub 2) — compute_embedding() now calls _pace_calls() (this account's real
    Bedrock quota is 20 req/minute, see content_embedding.py's own comment on that constant),
    which time.sleep()s for real outside a test. This file's tests exercise compute_embedding()
    directly (only _client() is mocked), so without this fixture every test after the first in
    a run would pay a real ~3.5s wait — not what this file is testing (that's covered by
    test_aa610_embedding_rate_limit.py's own dedicated, mocked-time tests)."""
    with patch.object(ce_mod, "time") as m_time:
        m_time.monotonic.return_value = 0.0
        yield m_time


class TestComputeEmbedding:
    def test_valid_response_returns_the_vector(self):
        vector = [0.1] * EMBEDDING_DIMENSIONS
        with patch.object(ce_mod, "_client", return_value=_boto_client_returning(_cohere_response(vector))):
            result = compute_embedding("Some real piece text.")
        assert result == vector

    def test_empty_text_returns_none_without_calling_bedrock(self):
        client = _boto_client_returning(_cohere_response([0.1] * EMBEDDING_DIMENSIONS))
        with patch.object(ce_mod, "_client", return_value=client):
            result = compute_embedding("   ")
        assert result is None
        client.invoke_model.assert_not_called()

    def test_client_error_is_soft_fail_not_raised(self):
        from botocore.exceptions import ClientError
        client = MagicMock()
        client.invoke_model.side_effect = ClientError({"Error": {"Code": "Throttling"}}, "InvokeModel")
        with patch.object(ce_mod, "_client", return_value=client):
            result = compute_embedding("Some text.")
        assert result is None

    def test_malformed_response_wrong_length_is_soft_fail(self):
        with patch.object(ce_mod, "_client", return_value=_boto_client_returning(_cohere_response([0.1, 0.2]))):
            result = compute_embedding("Some text.")
        assert result is None

    def test_missing_embeddings_key_is_soft_fail(self):
        with patch.object(ce_mod, "_client", return_value=_boto_client_returning({"texts": ["x"]})):
            result = compute_embedding("Some text.")
        assert result is None

    def test_empty_embeddings_list_is_soft_fail(self):
        with patch.object(ce_mod, "_client",
                           return_value=_boto_client_returning({"embeddings": {"float": []}})):
            result = compute_embedding("Some text.")
        assert result is None

    def test_input_text_and_model_id_reach_the_request(self):
        client = _boto_client_returning(_cohere_response([0.1] * EMBEDDING_DIMENSIONS))
        with patch.object(ce_mod, "_client", return_value=client):
            compute_embedding("A very specific piece of text.")
        call = client.invoke_model.call_args
        assert call.kwargs["modelId"] == "us.cohere.embed-v4:0"
        body = json.loads(call.kwargs["body"])
        assert body["texts"] == ["A very specific piece of text."]
        assert body["output_dimension"] == EMBEDDING_DIMENSIONS
        assert body["input_type"] == "search_document"


class TestEmbeddingToPgvectorLiteral:
    def test_formats_as_bracketed_comma_separated_floats(self):
        literal = embedding_to_pgvector_literal([0.1, -0.2, 3.0])
        assert literal.startswith("[") and literal.endswith("]")
        assert literal == "[0.1,-0.2,3.0]"

    def test_round_trips_a_realistic_length_vector(self):
        vector = [0.001 * i for i in range(EMBEDDING_DIMENSIONS)]
        literal = embedding_to_pgvector_literal(vector)
        parsed = [float(x) for x in literal.strip("[]").split(",")]
        assert len(parsed) == EMBEDDING_DIMENSIONS
        assert parsed[10] == vector[10]
