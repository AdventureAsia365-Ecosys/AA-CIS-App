"""AA-862 — scripts/aa856_global_profile_probe.py `preflight --invoke --account acc3`: the gate for
migration 240 (satellite answered, 1536 floats, same model as acc2 by cosine). DB, STS and Bedrock
are stubbed; the probe's real run is in the api container."""
import asyncio
import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa856_global_profile_probe.py")
_spec = importlib.util.spec_from_file_location("aa856_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

DIM = probe.PROBE_DIMENSIONS


def _ok(account, vector):
    return {"ok": True, "account": account, "model_id": probe.GLOBAL_PROFILE, "dimensions": len(vector),
            "input_tokens": 8, "tokens_estimated": False, "cost_usd": 0.000001, "ms": 40,
            "_resp": SimpleNamespace(vectors=[vector], model_used="m", account=account, input_tokens=8,
                                     cost_usd=0.000001, tokens_estimated=False)}


def _fail(account):
    return {"ok": False, "account": account, "model_id": probe.GLOBAL_PROFILE,
            "error_type": "ClientError", "error": "ThrottlingException", "ms": 30}


def _run(invokes, jobs=()):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[], [], list(jobs)])
    conn.close = AsyncMock()
    by_account = dict(invokes)
    with patch.object(probe, "_identity", return_value={"account": "111122223333"}), \
         patch.object(probe, "_connect", AsyncMock(return_value=conn)), \
         patch.object(probe, "_invoke_global", side_effect=lambda account="acc2": by_account[account]), \
         patch.object(probe, "_log_probe", AsyncMock()) as m_log:
        out = asyncio.run(probe.preflight(True, "acc3"))
    return out, m_log


def test_same_vectors_open_the_240_gate():
    v = [0.01 * (i % 7 + 1) for i in range(DIM)]
    out, m_log = _run({"acc3": _ok("acc3", v), "acc2": _ok("acc2", list(v))})
    assert out["gate"]["apply_240"] is True
    assert out["gate"]["cosine_vs_acc2"] == pytest.approx(1.0)
    assert m_log.await_count == 2


def test_a_different_model_keeps_the_gate_closed():
    a = [1.0 if i % 2 else 0.0 for i in range(DIM)]
    b = [0.0 if i % 2 else 1.0 for i in range(DIM)]
    out, _ = _run({"acc3": _ok("acc3", a), "acc2": _ok("acc2", b)})
    assert out["gate"]["same_model"] is False and out["gate"]["apply_240"] is False


def test_an_acc2_throttle_leaves_the_cosine_unproven():
    v = [0.5] * DIM
    out, m_log = _run({"acc3": _ok("acc3", v), "acc2": _fail("acc2")})
    assert out["gate"]["satellite_invoke_ok"] is True and out["gate"]["cosine_vs_acc2"] is None
    assert out["gate"]["apply_240"] is False
    assert m_log.await_count == 1


def test_a_refused_satellite_keeps_the_gate_closed():
    out, _ = _run({"acc3": _fail("acc3"), "acc2": _ok("acc2", [0.5] * DIM)})
    assert out["gate"]["satellite_invoke_ok"] is False and out["gate"]["apply_240"] is False


def test_jobs_in_flight_are_reported_but_do_not_block_240():
    v = [0.3] * DIM
    out, _ = _run({"acc3": _ok("acc3", v), "acc2": _ok("acc2", v)},
                  jobs=[{"kind": "place_extract", "status": "running", "jobs": 1}])
    assert out["gate"]["no_jobs_in_flight"] is False and out["gate"]["apply_240"] is True
