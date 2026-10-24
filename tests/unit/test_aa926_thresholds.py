"""AA-926 — services/quality/thresholds.yaml: refs resolve, the calibration format check works, every
`used_by` file exists, and every consumer reads the one pass line."""
from pathlib import Path

import pytest

from services.quality import lint, registry

ROOT = Path(__file__).resolve().parents[2]


def test_every_key_resolves_and_pass_line_is_seven():
    keys = registry.thresholds_doc()["thresholds"]
    for k in keys:
        registry.threshold(k)
    assert registry.pass_line() == 7.0
    for k, e in keys.items():
        if e.get("ref") == "pass_line":
            assert registry.threshold(k) == 7.0, k


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        registry.threshold("a1.no_such_threshold")


def test_used_by_files_exist():
    for k, e in registry.thresholds_doc()["thresholds"].items():
        for f in e.get("used_by") or []:
            assert (ROOT / f).is_file(), (k, f)


def test_consumers_read_the_registry_values():
    from api.routers import admin_llm_ops, admin_pipeline, admin_settings
    from services.acp_content_writing import service
    from services.acp_produce import tenant_pipeline
    from services.acp_shared import debate
    from services.content_generation import brand_fit, graph, judge_node

    assert graph.MIN_QUALITY == judge_node._MIN_QUALITY == admin_llm_ops._JUDGE_PASS_LINE == 7.0
    assert tenant_pipeline._T3_PASS_LINE == debate.BRAND_FIT_CUT_THRESHOLD == 7.0
    assert admin_settings.PIPELINE_GATES["brand_audit_threshold"] == graph.MIN_QUALITY
    assert admin_pipeline._PUBLISH_FLOOR == admin_pipeline._LOW_QUALITY_FLOOR == 7.0
    assert graph.MISSING_FIELD_CAP == 4.0 and judge_node._TIE_BAND == 0.5 and judge_node._MISSION_ABSENT_CAP == 6.0
    assert brand_fit._REPEAT_BAND == (6.0, 8.0)
    assert service._REUSE_SIMILARITY_THRESHOLD == 0.92


def _errors_with(monkeypatch, entry: dict) -> list[str]:
    doc = {"version": "qthr-v1", "thresholds": {**registry.thresholds_doc()["thresholds"], "x.test": entry}}
    monkeypatch.setattr(registry, "thresholds_doc", lambda: doc)
    res = lint.Result()
    lint.check_schema(res, set(lint.tracked_files()))
    return [e for e in res.errors if "x.test" in e]


CAL_NULL = {"run_id": None, "precision": None, "recall": None, "n": None, "calibrated": False}


def test_uncalibrated_null_run_id_is_fine(monkeypatch):
    assert _errors_with(monkeypatch, {"value": 1.0, "used_by": [], "calibration": dict(CAL_NULL)}) == []


def test_calibrated_needs_a_well_formed_run_id(monkeypatch):
    assert _errors_with(monkeypatch, {"value": 1.0, "used_by": [], "calibration": {**CAL_NULL, "calibrated": True}})
    ok = {**CAL_NULL, "calibrated": True, "run_id": "qcal-20261101-a1-judge", "precision": 0.9, "recall": 0.8,
          "n": 120}
    assert _errors_with(monkeypatch, {"value": 1.0, "used_by": [], "calibration": ok}) == []


def test_value_and_ref_are_exclusive_and_calibration_is_required(monkeypatch):
    assert _errors_with(monkeypatch, {"value": 1.0, "ref": "pass_line", "used_by": [], "calibration": dict(CAL_NULL)})
    assert _errors_with(monkeypatch, {"value": 1.0, "used_by": []})
    assert _errors_with(monkeypatch, {"value": 1.0, "used_by": ["no/such/file.py"], "calibration": dict(CAL_NULL)})
