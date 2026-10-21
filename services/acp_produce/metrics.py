"""
services.acp_produce.metrics — AA-362 [N8-2] CloudWatch custom metrics for
N7 Produce pieces (held_rate, grounding_score floor, repair_count).

STEP 0 (AA-362) confirmed: the ONLY existing custom-metric emission
convention in this repo is api/routers/acp_health.py::_emit_cloudwatch_metrics
(AA-141) — namespace "acp" (lowercase, no slash), best-effort
boto3 cloudwatch.put_metric_data, silently logs+skips on error, never raises.
No "ACP/Pipeline" or "ACP/Produce" namespace exists anywhere in this repo or
in AA-CIS-Infra's modules/monitoring dashboard (grep confirmed 0 hits) — this
module reuses "acp" rather than inventing a second namespace convention.

gates.py/models.py/reliability.py were read in full during STEP 0: none of
them emit any CloudWatch metric today — run_gates() is a pure decision loop
(no I/O beyond the caller-supplied gate_fns/repair_fn), and
reliability.py's run_produce_slot() only writes DB checkpoints. Deliberately
NOT wired into run_gates() itself: that function's tests
(tests/unit/test_aa298_gates.py) run today with zero AWS mocking, and its
docstring/tests treat it as a pure repair-budget state machine — adding a
boto3 network call inside it would force every existing gates.py test to
mock boto3 for no benefit. Same separation reliability.py already uses
(checkpoint wrapper kept apart from the gate stack it wraps).

emit_piece_metrics() is meant to be called once per Piece, right after
run_gates() returns a terminal (passed/held) piece — NOT per gate, NOT per
repair round, NOT per atom (issue explicitly asked to minimize PutMetricData
call volume). The full N7 orchestrator that would call this doesn't exist
yet (docs/implementation-notes/AA-298.md: "chưa lắp ráp thành 1 luồng
end-to-end") — same situation atom_usage.write_usage_log() (AA-361) is in:
this is the ready-to-call building block, not a wired pipeline.

grounding_score: the issue text assumes a numeric score with a hard floor of
1.0. The real F1 gate (gates.py::gate_grounding) is binary pass/fail
(GateResult.passed: bool) — there is no numeric grounding score anywhere in
the real implementation (same "docstring/issue promises a shape the code
doesn't have" pattern already caught for P0-1/P0-2/B14 in AA-298). Mapped
here as 1.0 when F1_grounding passed, 0.0 when it failed — a HARD floor of
1.0 then means exactly "alarm the instant F1 fails even once," which matches
the issue's stated intent for that gate.

held_rate is NOT computed in Python: emitting two per-piece counts
(piece_passed/piece_held) and letting the CloudWatch alarm's metric math
compute the ratio over its own evaluation window is standard CloudWatch
practice and avoids this module needing to track any state across pieces.
"""
from __future__ import annotations

import os
from typing import Optional

import boto3
import structlog

from services.acp_produce.models import Piece
from services.quality import registry as qr

logger = structlog.get_logger()

NAMESPACE = "acp"


def piece_metric_data(piece: Piece) -> list[dict]:
    """Pure — the CloudWatch MetricData list for one Piece. Returns [] for a
    piece that hasn't reached a terminal state yet (status == "in_progress")
    since held/passed is meaningless mid-repair-loop."""
    if piece.status not in ("passed", "held"):
        return []

    metrics: list[dict] = [
        {"MetricName": "piece_passed", "Value": 1.0 if piece.status == "passed" else 0.0, "Unit": "Count"},
        {"MetricName": "piece_held", "Value": 1.0 if piece.status == "held" else 0.0, "Unit": "Count"},
        {"MetricName": "repair_count", "Value": float(piece.repair_count), "Unit": "Count"},
    ]

    f1_result = next((g for g in piece.gate_ledger if g.gate == qr.F1_GROUNDING), None)
    if f1_result is not None:
        metrics.append({
            "MetricName": "grounding_score",
            "Value": 1.0 if f1_result.passed else 0.0,
            "Unit": "None",
        })

    return metrics


def emit_piece_metrics(piece: Piece, region: Optional[str] = None) -> None:
    """Best-effort CloudWatch PutMetricData — same convention as
    api/routers/acp_health.py::_emit_cloudwatch_metrics(): silently logs and
    skips on any error, never raises. A CloudWatch outage must never fail
    the actual N7 pipeline."""
    metrics = piece_metric_data(piece)
    if not metrics:
        return
    try:
        cw = boto3.client("cloudwatch", region_name=region or os.environ.get("AWS_REGION", "us-west-1"))
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metrics)
    except Exception as exc:
        logger.warning("cloudwatch_emit_failed", error=str(exc), piece_id=piece.piece_id)


__all__ = ["NAMESPACE", "piece_metric_data", "emit_piece_metrics"]
