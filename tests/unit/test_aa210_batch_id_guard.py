"""AA-210: unit tests for the _is_uuid guard on the pipeline_runs accounting UPDATE.

Pure truth-table tests — no DB, no network. Confirms non-UUID batch_ids (e.g. ad-hoc
verification labels) are rejected so the `WHERE batch_id = $N::uuid` cast cannot crash.
"""

import pytest

from api.routers.admin_pipeline import _is_uuid


# AA-993: fixed values only. A uuid4() in the parameter list gives a different test ID on every collection, and
# pytest-xdist refuses to run when its workers collect different IDs.
@pytest.mark.parametrize("value", [
    "2f6c1d5e-8a3b-4c7d-9e0f-1a2b3c4d5e6f",
    "ca893afe-27e2-431b-9596-b92514e7f98c",
])
def test_is_uuid_accepts_valid_uuid(value):
    assert _is_uuid(value) is True


@pytest.mark.parametrize("value", [
    "verify-s68-sonnet",
    "",
    None,
    12345,
])
def test_is_uuid_rejects_non_uuid(value):
    assert _is_uuid(value) is False
