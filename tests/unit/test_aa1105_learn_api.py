"""AA-1105 — the AA Academy learning API (/learn, contract learn.http.v1) against an in-memory store and an invented
bundle: every body is validated against the contract's own schema (a byte copy of the root contract, pinned by
sha256), the route class accepts only WORKFORCE x academy-web, and the learning rules hold (drafts, grading on the
server, retake delay, review cards, certificates, the public verify view and its rate limit)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from api.core import identity_config, learn_identity, partner_identity
from api.core.m2m_auth import DEV_M2M_ISSUER
from api.routers import learn as learn_router
from services.learn import bundle as lb
from services.learn import grading as g
from tests.unit.aa1105_learn_fake import ACADEMY, QUIZZES, SUB, Harness, WORKFORCE

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "learn.http.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/learn.http.v1.openapi.yaml (info.version 1.0). Update the root file first, copy it
# here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "1ec8dfc84e87ae4b970f12235728a8e9b2dd37e98fd427c0f44cc72f7030f9d9"
FIXED_ID = "11050000-0000-4000-8000-000000000001"


def conforms(body, schema: str) -> None:
    Draft202012Validator({**DOC, "$ref": f"#/components/schemas/{schema}"}).validate(body)


@pytest.fixture
def h():
    harness = Harness()
    try:
        yield harness
    finally:
        harness.close()


def answers(item_ids, wrong=()):
    """Correct responses for the bound items (wrong ones for the ids in `wrong`)."""
    by_id = {i["id"]: i for items in QUIZZES.values() for i in items}
    out = []
    for item_id in item_ids:
        right = sorted(by_id[item_id]["answer"])
        out.append({"item_id": item_id, "chosen": [x for x in (0, 1, 2) if x not in right][:1]
                    if item_id in wrong else right})
    return out


def complete_lessons(h, *lesson_ids):
    for lid in lesson_ids:
        r = h.post("/v1/progress", {"statement_id": str(uuid.uuid4()), "verb": "completed", "lesson_id": lid})
        assert r.status_code == 201, r.text


def pass_module_quiz(h, module_id):
    attempt = h.post(f"/v1/modules/{module_id}/quiz/attempts").json()
    return h.post(f"/v1/quiz/attempts/{attempt['attempt_id']}/submit",
                  {"responses": answers([i["id"] for i in attempt["items"]])}).json()


def test_contract_copy_is_the_root_file():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256


def test_every_contract_path_is_served():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(learn_router.router)
    app.include_router(learn_router.public_router)
    served = {p for p in app.openapi()["paths"]}
    assert {f"/learn{p}" for p in DOC["paths"]} == served


# ── route class (ADR 0022 decision 6) ───────────────────────────────────────────────────────────────────────────

def test_me_creates_the_learner_with_the_directory_name(h):
    h.store.staff[(WORKFORCE, SUB)]["display_name"] = "Directory Name"
    r = h.get("/v1/me", name="Typed By The Client")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    conforms(r.json(), "Me")
    assert r.json()["display_name"] == "Directory Name" and r.json()["preview"] is False


def test_no_or_bad_token_is_401(h):
    r = h.client.get("/learn/v1/me")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    conforms(r.json(), "Problem")
    assert "Bearer" in r.headers["www-authenticate"]
    r = h.client.get("/learn/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


@pytest.mark.parametrize("client_id", [identity_config.DEV_CIS_ADMIN_CLIENT_ID,
                                       identity_config.DEV_BREAKGLASS_CLIENT_ID, "7hhcrmstaff0000000000000aa"])
def test_another_workforce_client_is_403(h, client_id):
    r = h.get("/v1/me", client_id=client_id)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_partner_pool_token_is_partner_not_open(h):
    from tests.unit.test_identity_verifier import LocalIssuer
    token = LocalIssuer(partner_identity.DEV_PARTNER_ISSUER).token(client_id=partner_identity.DEV_PARTNER_WEB_CLIENT_ID)
    r = h.client.get("/learn/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["error"] == "partner_not_open"


def test_m2m_token_is_403(h):
    from tests.unit.test_identity_verifier import LocalIssuer
    token = LocalIssuer(DEV_M2M_ISSUER).token(client_id="7m2mclient000000000000000a")
    assert h.client.get("/learn/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_unknown_or_inactive_staff_is_not_a_learner(h):
    r = h.get("/v1/me", sub="1105aaaa-0000-4000-8000-0000000000ff")
    assert r.status_code == 403 and r.json()["error"] == "not_a_learner"


def test_e2e_identity_is_a_test_learner_on_dev_only(h, monkeypatch):
    h.store.staff[(WORKFORCE, SUB)]["email"] = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert h.get("/v1/me").json()["is_test"] is True
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert h.get("/v1/me").status_code == 403


def test_academy_client_is_accepted_nowhere_else():
    staff_clients = {c for cfg in identity_config.cis_staff_issuers() for c in cfg.client_ids}
    assert ACADEMY not in staff_clients
    assert learn_identity.academy_issuer_config().client_ids == frozenset({ACADEMY})


def test_academy_client_equal_to_a_staff_client_fails_closed(monkeypatch):
    monkeypatch.setenv(learn_identity.ENV_ACADEMY_WEB_CLIENT_ID, identity_config.DEV_CIS_ADMIN_CLIENT_ID)
    with pytest.raises(ValueError):
        learn_identity.academy_issuer_config()


# ── paths, modules, lessons ──────────────────────────────────────────────────────────────────────────────────────

def test_paths_hide_drafts_and_partner_paths(h):
    r = h.get("/v1/paths")
    assert r.status_code == 200
    conforms(r.json(), "PathList")
    paths = {p["id"]: p for p in r.json()["paths"]}
    assert set(paths) == {"sample-path", "solo-path"} and r.json()["bundle_sha"] == "f" * 40
    assert [m["id"] for m in paths["sample-path"]["modules"]] == ["Z9"]           # draft D5 hidden
    assert paths["sample-path"]["planned"] == ["Z7"] and paths["sample-path"]["final_quiz"] is True


def test_preview_role_sees_drafts_marked(h):
    paths = {p["id"]: p for p in h.get("/v1/paths", **{"cognito:groups": ["cis-admin"]}).json()["paths"]}
    assert [(m["id"], m["status"]) for m in paths["sample-path"]["modules"]] == [("Z9", "active"), ("D5", "draft")]
    module = h.get("/v1/modules/D5", **{"cognito:groups": ["cis-content_lead"]}).json()
    assert module["draft"] is True
    assert h.get("/v1/modules/D5").status_code == 404


def test_enroll_is_idempotent(h):
    r = h.post("/v1/paths/sample-path/enroll")
    assert r.status_code == 201
    conforms(r.json(), "Enrollment")
    assert h.post("/v1/paths/sample-path/enroll").status_code == 200
    assert h.post("/v1/paths/agency-path/enroll").status_code == 404
    assert h.post("/v1/paths/Bad_Path/enroll").json()["error"] == "not_found"
    assert h.get("/v1/paths").json()["paths"][0]["enrolled"] is True


def test_module_view(h):
    r = h.get("/v1/modules/Z9")
    assert r.status_code == 200
    conforms(r.json(), "Module")
    body = r.json()
    assert body["prerequisites"] == [{"id": "Z7", "state": "planned"}]
    assert body["quiz"]["items_per_attempt"] == 3 and body["quiz"]["pass_mark_pct"] == 80   # one item suspended
    assert body["lab"] == {"present": False, "state": "none"}
    assert h.get("/v1/modules/Z8").json()["lab"] == {"present": True, "state": "closed"}
    assert h.get("/v1/modules/Q1").status_code == 404


def test_lesson_in_both_languages(h):
    r = h.get("/v1/lessons/Z9-01?lang=vi")
    assert r.status_code == 200
    conforms(r.json(), "Lesson")
    assert r.json()["lang"] == "vi" and r.json()["languages"] == ["en", "vi"]
    assert r.json()["sources"][0]["body_md"].startswith("## Sample rule")
    en = h.get("/v1/lessons/Z9-02?lang=vi").json()
    assert en["lang"] == "en" and en["languages"] == ["en"]
    assert h.get("/v1/lessons/Z9-01?lang=fr").status_code == 422


def test_lesson_video_is_presigned(h, monkeypatch):
    calls = []

    async def presign(key, ttl, *, check=False):
        calls.append((key, ttl, check))
        return f"https://bucket.example.invalid/{key}?X-Amz-Expires={ttl}"
    monkeypatch.setattr(lb, "presign", presign)
    r = h.get("/v1/lessons/Z9-01/video")
    assert r.status_code == 200
    conforms(r.json(), "VideoUrls")
    assert calls[0] == ("learn-media/Z9/01-sample-lesson.mp4", 300, True)
    assert r.json()["captions"][0]["lang"] == "en"
    assert h.get("/v1/lessons/Z9-02/video").status_code == 404


def test_no_bundle_is_503(h, monkeypatch):
    async def missing():
        raise lb.LearnUnavailable("no learn bundle is published yet")
    monkeypatch.setattr(lb, "load", missing)
    r = h.get("/v1/paths")
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


# ── progress ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_progress_is_idempotent_on_statement_id(h):
    body = {"statement_id": FIXED_ID, "verb": "opened", "lesson_id": "Z9-01", "lang": "en"}
    r = h.post("/v1/progress", body)
    assert r.status_code == 201
    conforms(r.json(), "ProgressAccepted")
    assert h.post("/v1/progress", body).status_code == 200
    assert h.post("/v1/progress", {**body, "verb": "completed"}).json()["error"] == "conflict"
    assert h.post("/v1/progress", {**body, "extra": 1}).status_code == 422
    assert h.post("/v1/progress", {**body, "lesson_id": "Q9-01",
                                   "statement_id": "11050000-0000-4000-8000-000000000002"}).status_code == 404


# ── quizzes ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_attempt_never_carries_answers_and_skips_suspended_items(h):
    r = h.post("/v1/modules/Z9/quiz/attempts")
    assert r.status_code == 201
    conforms(r.json(), "QuizAttempt")
    ids = {i["id"] for i in r.json()["items"]}
    assert ids == {"Z9-Q1", "Z9-Q2", "Z9-Q3"}
    assert "answer" not in r.text and "explain" not in r.text and "suspended" not in r.text
    assert {i["kind"] for i in r.json()["items"]} == {"single", "multiple"}


def test_failed_quiz_waits_for_retake_and_makes_review_cards(h):
    attempt = h.post("/v1/modules/Z9/quiz/attempts").json()
    ids = [i["id"] for i in attempt["items"]]
    r = h.post(f"/v1/quiz/attempts/{attempt['attempt_id']}/submit", {"responses": answers(ids, wrong=ids[:2])})
    assert r.status_code == 200
    conforms(r.json(), "QuizResult")
    assert r.json()["passed"] is False and r.json()["retake_at"] is not None
    again = h.post("/v1/modules/Z9/quiz/attempts")
    assert again.status_code == 409 and again.json()["error"] == "retake_wait" and again.json()["retry_at"]
    closed = h.post(f"/v1/quiz/attempts/{attempt['attempt_id']}/submit", {"responses": []})
    assert closed.json()["error"] == "attempt_closed"
    due = h.get("/v1/review/due")
    conforms(due.json(), "ReviewDue")
    assert {c["item"]["id"] for c in due.json()["cards"]} == set(ids[:2])
    assert due.json()["daily_cap"] == 20 and due.json()["target_retention"] == 0.9


def test_submission_is_checked_against_the_bound_items(h):
    attempt = h.post("/v1/modules/Z9/quiz/attempts").json()
    url = f"/v1/quiz/attempts/{attempt['attempt_id']}/submit"
    assert h.post(url, {"responses": [{"item_id": "Z9-Q4", "chosen": [2]}]}).status_code == 422
    twice = [{"item_id": attempt["items"][0]["id"], "chosen": [0]}] * 2
    assert h.post(url, {"responses": twice}).status_code == 422
    assert h.post("/v1/quiz/attempts/11050000-0000-4000-8000-0000000000aa/submit",
                  {"responses": []}).status_code == 404


def test_final_quiz_issues_a_verifiable_certificate(h):
    assert h.post("/v1/paths/sample-path/final-quiz/attempts").json()["error"] == "prerequisites_open"
    complete_lessons(h, "Z9-01", "Z9-02")
    result = pass_module_quiz(h, "Z9")
    assert result["passed"] is True and result["module_complete"] is True and result["certificate"] is None
    final = h.post("/v1/paths/sample-path/final-quiz/attempts")
    assert final.status_code == 201 and final.json()["scope"] == "final"
    graded = h.post(f"/v1/quiz/attempts/{final.json()['attempt_id']}/submit",
                    {"responses": answers([i["id"] for i in final.json()["items"]])}).json()
    conforms(graded, "QuizResult")
    code = graded["certificate"]["code"]
    assert graded["certificate"]["verify_path"] == f"/verify/{code}"
    certs = h.get("/v1/certificates").json()
    conforms(certs, "CertificateList")
    assert certs["certificates"][0]["status"] == "valid" and certs["certificates"][0]["expires_at"] is None
    assert certs["certificates"][0]["module_versions"] == [{"module_id": "Z9", "version": "1.0.0"}]
    view = h.client.get(f"/learn/public/v1/certificates/{code}")
    assert view.status_code == 200
    conforms(view.json(), "CertificateVerify")
    assert view.json()["holder_display_name"] == "Sample Learner"
    paths = {p["id"]: p for p in h.get("/v1/paths").json()["paths"]}
    assert paths["sample-path"]["completed"] is True and paths["sample-path"]["certificate_status"] == "valid"
    # a new code: the old one stops working
    new = h.post(f"/v1/certificates/{graded['certificate']['certificate_id']}/code")
    conforms(new.json(), "CertificateCode")
    assert h.client.get(f"/learn/public/v1/certificates/{code}").status_code == 404
    assert h.client.get(f"/learn/public/v1/certificates/{new.json()['code']}").status_code == 200


def pass_final_quiz(h, path_id):
    final = h.post(f"/v1/paths/{path_id}/final-quiz/attempts")
    assert final.status_code == 201, final.text
    return h.post(f"/v1/quiz/attempts/{final.json()['attempt_id']}/submit",
                  {"responses": answers([i["id"] for i in final.json()["items"]])}).json()


def test_final_quiz_passed_again_keeps_the_valid_certificate(h):
    complete_lessons(h, "Z9-01", "Z9-02")
    pass_module_quiz(h, "Z9")
    first = pass_final_quiz(h, "sample-path")["certificate"]
    stored = dict(next(iter(h.store.certs.values())))
    again = pass_final_quiz(h, "sample-path")
    conforms(again, "QuizResult")
    assert again["passed"] is True and again["certificate"] is None
    after = next(iter(h.store.certs.values()))
    assert after["code_sha256"] == stored["code_sha256"] and after["issued_at"] == stored["issued_at"]
    assert after["expires_at"] == stored["expires_at"] and after["renewal_reason"] is None
    assert h.client.get(f"/learn/public/v1/certificates/{first['code']}").status_code == 200


def test_final_quiz_renews_after_a_major_version(h):
    complete_lessons(h, "Z9-01", "Z9-02")
    pass_module_quiz(h, "Z9")
    first = pass_final_quiz(h, "sample-path")["certificate"]
    h.bundle.modules["Z9"]["version"] = "2.0.0"
    assert pass_module_quiz(h, "Z9")["certificate"] is None    # the new major needs its module quiz first
    renewed = pass_final_quiz(h, "sample-path")["certificate"]
    assert renewed is not None and renewed["certificate_id"] == first["certificate_id"]
    assert next(iter(h.store.certs.values()))["renewal_reason"] == "major_version"
    assert h.client.get(f"/learn/public/v1/certificates/{first['code']}").status_code == 404
    assert h.client.get(f"/learn/public/v1/certificates/{renewed['code']}").status_code == 200


def test_path_without_final_quiz_certifies_on_its_last_module(h):
    complete_lessons(h, "Z8-01")
    result = pass_module_quiz(h, "Z8")
    assert result["certificate"]["path_id"] == "solo-path"


def test_major_version_asks_for_renewal(h):
    complete_lessons(h, "Z8-01")
    pass_module_quiz(h, "Z8")
    h.bundle.modules["Z8"]["version"] = "2.0.0"
    certs = h.get("/v1/certificates").json()["certificates"]
    assert certs[0]["status"] == "renewal_due"
    assert h.post(f"/v1/certificates/{certs[0]['certificate_id']}/code").json()["error"] == "conflict"


def test_lab_is_closed(h):
    r = h.post("/v1/modules/Z8/lab/check")
    assert r.status_code == 409 and r.json()["error"] == "lab_closed"
    assert h.post("/v1/modules/Z9/lab/check").status_code == 404


# ── spaced review ────────────────────────────────────────────────────────────────────────────────────────────────

def _miss_one(h):
    attempt = h.post("/v1/modules/Z9/quiz/attempts").json()
    ids = [i["id"] for i in attempt["items"]]
    h.post(f"/v1/quiz/attempts/{attempt['attempt_id']}/submit", {"responses": answers(ids, wrong=ids[:1])})
    return ids[0]


def test_review_result_stores_a_forward_card_only(h):
    item_id = _miss_one(h)
    now = g.now_utc().replace(microsecond=0)
    card = {"due": g.iso(now + timedelta(days=1)), "stability": 1.2, "difficulty": 5.0, "elapsed_days": 0,
            "scheduled_days": 1, "learning_steps": 0, "reps": 1, "lapses": 0, "state": 1, "last_review": g.iso(now)}
    right = sorted(next(i for i in QUIZZES["Z9"] if i["id"] == item_id)["answer"])
    bad = h.post(f"/v1/review/{item_id}/result", {"chosen": right, "reviewed_at": g.iso(now),
                                                  "card": {**card, "due": g.iso(now - timedelta(days=1))}})
    assert bad.status_code == 422
    skew = h.post(f"/v1/review/{item_id}/result", {"chosen": right, "reviewed_at": g.iso(now),
                                                   "card": {**card, "last_review": g.iso(now - timedelta(hours=1))}})
    assert skew.status_code == 422
    ok = h.post(f"/v1/review/{item_id}/result", {"chosen": right, "reviewed_at": g.iso(now), "card": card})
    assert ok.status_code == 200
    conforms(ok.json(), "ReviewResult")
    assert ok.json()["correct"] is True
    assert h.get("/v1/review/due").json()["cards"] == []            # due tomorrow now
    assert h.post("/v1/review/Z9-Q9/result", {"chosen": [], "reviewed_at": g.iso(now), "card": card}).status_code \
        == 404


# ── public verify and its rate limit ────────────────────────────────────────────────────────────────────────────

def test_verify_unknown_or_malformed_code_is_404(h):
    r = h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA")
    assert r.status_code == 404
    conforms(r.json(), "Problem")
    assert h.client.get("/learn/public/v1/certificates/short").status_code == 404


def test_verify_is_rate_limited_per_client_address(h):
    edge = {"X-Forwarded-For": "34.120.0.7, 52.8.0.9, 10.0.3.4"}     # caller-written, API GW source, ALB peer
    for _ in range(30):
        assert h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA", headers=edge).status_code == 404
    r = h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA", headers=edge)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited" and int(r.headers["retry-after"]) <= 60
    conforms(r.json(), "Problem")
    # a forged left-most entry does not move the caller to another bucket
    forged = {"X-Forwarded-For": "35.1.2.3, 52.8.0.9, 10.0.3.4"}
    assert h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA", headers=forged).status_code == 429
    other = {"X-Forwarded-For": "52.8.0.10, 10.0.3.4"}
    assert h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA", headers=other).status_code == 404


def test_verify_limit_is_the_setting(h, monkeypatch):
    from shared import settings
    real = settings.aget

    async def aget(key, tenant_id=None):
        return 2 if key == "cis.learn.verify_requests_per_minute" else await real(key, tenant_id)
    monkeypatch.setattr(settings, "aget", aget)
    codes = [h.client.get("/learn/public/v1/certificates/AAAAAAAAAAAAAAAAAAAAAA").status_code for _ in range(3)]
    assert codes == [404, 404, 429]
