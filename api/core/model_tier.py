"""AA-1024 A2 (root ADR 0009 decision 5) — a request body never picks its own model.

``model_tier`` is accepted only in an eval or probe context (amends CIS ADR 0006 §4): a production run uses its
stage route (``shared.llm_role_config``, AI providers › Routing), so the route, its fallbacks, the data scopes and the
kill switch always apply. The request models of the rewrite / ingest / batch endpoints therefore have no
``model_tier`` field, and a body that still sends one with a value gets a 422 (``refuse_model_tier``, a pydantic
"before" validator: FastAPI turns its ValueError into a 422). ``"model_tier": null`` is accepted and ignored, so an old
client that sends an explicit null keeps working.

Internal eval and probe callers (services/eval/regression.py, the probes, the AA-237 / AA-736 one-shot upgrade) keep
passing a model to ``_rewrite_tour`` / ``LLMRequest`` directly; they are not request bodies.
"""
from __future__ import annotations

from typing import Any

MODEL_TIER_REFUSED = ("model_tier is not accepted: a run uses its stage route (AI providers > Routing). A per-request "
                      "model is for eval and probe runs only (ADR 0009 decision 5)")


def refuse_model_tier(data: Any) -> Any:
    """For ``@model_validator(mode="before")``: ValueError when the body sends ``model_tier`` with a value; the key is
    dropped otherwise (an explicit null)."""
    if isinstance(data, dict) and "model_tier" in data:
        if data.get("model_tier") not in (None, ""):
            raise ValueError(MODEL_TIER_REFUSED)
        data = {k: v for k, v in data.items() if k != "model_tier"}
    return data
