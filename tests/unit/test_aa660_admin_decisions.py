"""AA-660 — admin decisions API: the question-update rules (same as the DB CHECKs, as readable 422s)."""
import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from api.routers.admin_decisions import QuestionUpdate, validate_update


def test_shadow_without_floors_is_fine():
    validate_update(QuestionUpdate(mode="shadow"))


def test_enforce_needs_a_calibration_record():
    with pytest.raises(HTTPException) as e:
        validate_update(QuestionUpdate(mode="enforce", accept_floor=0.9, reject_ceiling=0.1))
    assert e.value.status_code == 422 and "calibration" in e.value.detail


def test_enforce_needs_a_floor():
    with pytest.raises(HTTPException):
        validate_update(QuestionUpdate(mode="enforce", calibration_ref="docs/calibration/x.md"))


def test_reject_ceiling_must_be_below_accept_floor():
    with pytest.raises(HTTPException):
        validate_update(QuestionUpdate(mode="shadow", accept_floor=0.4, reject_ceiling=0.6))


def test_calibrated_enforce_passes():
    validate_update(QuestionUpdate(mode="enforce", accept_floor=0.85, reject_ceiling=0.15,
                                   calibration_ref="docs/calibration/a3_keyword_belongs.md"))


def test_floors_are_bounded():
    with pytest.raises(ValidationError):
        QuestionUpdate(mode="shadow", accept_floor=1.5)
