"""AA-1105 — the pure rules of the learning API (services/learn/grading.py) and the learn bundle reader
(services/learn/bundle.py) on a fake S3 client. Invented data only."""
from __future__ import annotations

import io
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from api.services import docs_reader
from services.learn import bundle as lb
from services.learn import grading as g
from tests.unit.aa1105_learn_fake import manifest

T0 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


# ── grading ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_grade_scores_bound_items_only_and_counts_missing_answers_wrong():
    items = {"Z9-Q1": {"id": "Z9-Q1", "answer": [0], "explain": "e", "source": "s"},
             "Z9-Q2": {"id": "Z9-Q2", "answer": [0, 2], "explain": "", "source": "s"}}
    score, graded = g.grade(["Z9-Q1", "Z9-Q2", "Z9-Q3"], items, {"Z9-Q1": [0], "Z9-Q2": [2]})
    assert score == 50.0 and [r["item_id"] for r in graded] == ["Z9-Q1", "Z9-Q2"]   # Q3 left the bundle
    assert graded[1]["correct_choices"] == [0, 2] and graded[1]["correct"] is False
    assert g.grade(["Z9-Q3"], items, {}) == (0.0, [])


def test_draw_and_public_item():
    items = [{"id": f"Z9-Q{n}", "kind": "multi", "prompt": "p", "choices": ["a", "b"], "answer": [0]}
             for n in range(1, 6)]
    picked = g.draw(items, 3, random.Random(7))
    assert len(picked) == 3 and len({i["id"] for i in picked}) == 3
    assert len(g.draw(items, 50)) == 5 and g.draw(items, -1) == []
    assert g.public_item(items[0]) == {"id": "Z9-Q1", "kind": "multiple", "prompt": "p", "choices": ["a", "b"]}


def test_retake_and_months():
    assert g.retake_at(T0, 24, T0 + timedelta(hours=1)) == T0 + timedelta(hours=24)
    assert g.retake_at(T0, 24, T0 + timedelta(hours=25)) is None and g.retake_at(None, 24, T0) is None
    assert g.add_months(datetime(2027, 1, 31, tzinfo=timezone.utc), 1).day == 28
    assert g.add_months(datetime(2028, 1, 31, tzinfo=timezone.utc), 1).day == 29
    assert g.add_months(T0, 12).year == 2027


def test_certificate_status():
    path = {"id": "p", "modules": [{"id": "Z9", "required": True}]}
    modules = {"Z9": {"id": "Z9", "status": "active", "version": "1.2.0"}}
    row = {"status": "valid", "expires_at": None, "module_versions": {"Z9": "1.0.0"}}
    assert g.certificate_status(row, path, modules, T0) == "valid"
    assert g.certificate_status({**row, "expires_at": T0}, path, modules, T0) == "expired"
    assert g.certificate_status({**row, "status": "revoked"}, path, modules, T0) == "revoked"
    assert g.certificate_status(row, path, {"Z9": {**modules["Z9"], "version": "2.0.0"}}, T0) == "renewal_due"
    added = {**path, "modules": [*path["modules"], {"id": "Z8", "required": True}]}
    assert g.certificate_status(row, added, {**modules, "Z8": {"id": "Z8", "status": "active",
                                                               "version": "1.0.0"}}, T0) == "renewal_due"
    assert g.renewal_reason(None, path, modules, T0) is None
    assert g.renewal_reason({**row, "expires_at": T0}, path, modules, T0) == "expired"
    assert g.renewal_reason(row, path, {"Z9": {**modules["Z9"], "version": "2.0.0"}}, T0) == "major_version"
    assert g.renewal_reason({**row, "status": "renewal_due"}, path, modules, T0) == "major_version"
    assert g.renewal_reason(row, added, {**modules, "Z8": {"id": "Z8", "status": "active",
                                                           "version": "1.0.0"}}, T0) == "module_added"


@pytest.mark.parametrize("status", ["valid", "revoked"])
def test_no_renewal_reason_for_a_valid_or_revoked_certificate(status):
    path = {"id": "p", "modules": [{"id": "Z9", "required": True}]}
    modules = {"Z9": {"id": "Z9", "status": "active", "version": "1.2.0"}}
    row = {"status": status, "expires_at": None, "module_versions": {"Z9": "1.0.0"}}
    with pytest.raises(ValueError):
        g.renewal_reason(row, path, modules, T0)


def test_card_checks():
    card = {"due": g.iso(T0 + timedelta(days=2)), "last_review": g.iso(T0)}
    assert g.card_problem(card, T0, T0, None, T0, 60) is None
    assert "earlier" in g.card_problem({**card, "due": g.iso(T0 - timedelta(days=1))}, T0, T0, None, T0, 60)
    assert "future" in g.card_problem(card, T0, T0, None, T0 - timedelta(hours=1), 60)
    assert "before" in g.card_problem(card, T0, T0, T0 + timedelta(hours=1), T0, 60)
    assert "zone" in g.card_problem({**card, "due": "2026-11-15T10:00:00"}, T0, T0, None, T0, 60)


def test_code_is_128_bit_and_only_its_hash_is_kept():
    code, sha = g.new_code()
    assert len(code) == 22 and sha == g.code_sha256(code) and len(sha) == 64 and code not in sha


def test_badge_has_no_personal_data():
    assert set(g.badge({"id": "p", "title": "P"})) == {"type", "achievement", "criteria", "issuer"}


# ── bundle reader ────────────────────────────────────────────────────────────────────────────────────────────────

class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 names
        self.reads.append(Key)
        if Key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3({})
    monkeypatch.setattr(docs_reader, "_s3_client", lambda: fake)
    monkeypatch.setattr(docs_reader, "_bucket", lambda: "bucket")
    monkeypatch.setenv("DOCS_READER_S3_PREFIX", "docs-reader")
    lb.clear_cache()
    yield fake
    lb.clear_cache()


@pytest.mark.asyncio
async def test_bundle_reads_manifest_and_bodies_once(s3):
    s3.objects["docs-reader/learn/manifest.json"] = json.dumps(manifest()).encode()
    s3.objects["docs-reader/learn/quizzes/Z9.json"] = json.dumps({"items": [{"id": "Z9-Q1", "answer": [0]}]}).encode()
    bundle = await lb.load()
    assert set(bundle.paths) == {"sample-path", "solo-path", "agency-path"} and bundle.sha == "f" * 40
    assert await lb.load() is bundle
    items = await lb.quiz(bundle, bundle.modules["Z9"])
    assert items[0]["answer"] == [0]
    await lb.quiz(bundle, bundle.modules["Z9"])
    assert s3.reads.count("docs-reader/learn/quizzes/Z9.json") == 1


@pytest.mark.asyncio
async def test_bundle_missing_or_bad_is_unavailable(s3):
    with pytest.raises(lb.LearnUnavailable):
        await lb.load()
    s3.objects["docs-reader/learn/manifest.json"] = json.dumps({"format": 2}).encode()
    with pytest.raises(lb.LearnUnavailable):
        await lb.load()
    s3.objects["docs-reader/learn/manifest.json"] = b"{not json"
    with pytest.raises(lb.LearnUnavailable):
        await lb.load()


@pytest.mark.asyncio
async def test_object_keys_are_shape_checked(s3):
    bundle = lb.index(manifest())
    with pytest.raises(lb.LearnUnavailable):
        await lb.lesson_body(bundle, "../secrets.md")
    with pytest.raises(lb.LearnUnavailable):
        await lb.section_body(bundle, "pages/AA-Ecosys/docs/x.md")
    with pytest.raises(lb.LearnUnavailable):
        await lb.lesson_body(bundle, "lessons/Z9-sample/01-sample-lesson.md")    # named but missing
    assert lb.media_key_ok("learn-media/Z9/01-sample-lesson.en.vtt")
    assert not lb.media_key_ok("docs-reader/learn/manifest.json")
    with pytest.raises(lb.LearnUnavailable):
        await lb.presign("photos/x.jpg", 60)
