"""AA-1105 — request and response models of the AA Academy learning API (contract learn.http.v1, a byte copy in
docs/contracts/learn.http.v1.openapi.yaml). Requests are validated with them (extra fields refused, answered 422
``invalid``); responses use them for the OpenAPI document only (AA-899: a public route declares its model), the
contract test validates the real bodies against the contract itself.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Lang = Literal["en", "vi"]
CertStatus = Literal["valid", "expired", "renewal_due", "revoked"]
LessonStatus = Literal["not_started", "opened", "completed"]
LessonType = Literal["reading", "walkthrough", "worked-example"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── requests ───────────────────────────────────────────────────────────────────────────────────────────────────────

class ProgressStatementIn(_In):
    statement_id: uuid.UUID
    verb: Literal["opened", "check_answered", "completed"]
    lesson_id: str = Field(pattern=r"^[A-Z][0-9]{1,2}-[0-9]{2}$")
    lang: Optional[Lang] = None


class ItemResponseIn(_In):
    item_id: str = Field(max_length=20)
    chosen: list[int] = Field(max_length=20)


class QuizSubmissionIn(_In):
    responses: list[ItemResponseIn] = Field(max_length=200)


class FsrsCard(_In):
    due: str = Field(max_length=40)
    stability: float = Field(ge=0)
    difficulty: float = Field(ge=0)
    elapsed_days: float = Field(ge=0)
    scheduled_days: float = Field(ge=0)
    learning_steps: Optional[int] = Field(default=None, ge=0)
    reps: int = Field(ge=0)
    lapses: int = Field(ge=0)
    state: int = Field(ge=0, le=3)
    last_review: Optional[str] = Field(max_length=40)


class ReviewSubmissionIn(_In):
    chosen: list[int] = Field(max_length=20)
    reviewed_at: str = Field(max_length=40)
    card: FsrsCard


# ── responses (OpenAPI document) ──────────────────────────────────────────────────────────────────────────────────

class Me(BaseModel):
    learner_id: str
    kind: Literal["staff", "partner"]
    display_name: Optional[str]
    preview: bool
    is_test: bool


class PathModule(BaseModel):
    id: str
    title: Optional[str]
    required: bool
    status: Literal["active", "draft", "planned"]
    complete: bool


class PathSummary(BaseModel):
    id: str
    title: str
    tier: Literal["staff", "partner"]
    enrolled: bool
    completed: bool
    final_quiz: bool
    certificate_status: Optional[CertStatus]
    modules: list[PathModule]
    planned: list[str] = []


class PathList(BaseModel):
    bundle_sha: Optional[str]
    paths: list[PathSummary]


class Enrollment(BaseModel):
    path_id: str
    status: Literal["active", "completed", "withdrawn"]
    enrolled_at: str
    completed_at: Optional[str] = None


class Prerequisite(BaseModel):
    id: str
    state: Literal["complete", "open", "planned"]


class LessonSummary(BaseModel):
    id: str
    title: str
    type: LessonType
    minutes: int
    stale: bool
    has_video: bool
    status: LessonStatus


class QuizState(BaseModel):
    available: bool
    items_per_attempt: int
    pass_mark_pct: int
    best_score_pct: Optional[float]
    passed: bool
    retake_at: Optional[str]


class LabState(BaseModel):
    present: bool
    state: Literal["none", "closed", "open", "passed"]


class Module(BaseModel):
    id: str
    title: str
    version: str
    status: Literal["active", "draft"]
    draft: bool
    stale: bool
    objectives: list[str]
    prerequisites: list[Prerequisite]
    complete: bool
    lessons: list[LessonSummary]
    quiz: QuizState
    lab: LabState


class CitedSection(BaseModel):
    ref: str
    kind: Literal["doc", "adr", "skill", "steering", "context"]
    stale: bool
    available: bool
    body_md: Optional[str]


class Lesson(BaseModel):
    id: str
    module_id: str
    module_version: str
    title: str
    type: LessonType
    minutes: int
    objective: str
    lang: Lang
    languages: list[Lang]
    body_md: str
    stale: bool
    draft: bool
    has_video: bool
    status: LessonStatus
    sources: list[CitedSection]


class Caption(BaseModel):
    lang: Lang
    url: str


class VideoUrls(BaseModel):
    url: str
    expires_at: str
    captions: list[Caption]


class ProgressAccepted(BaseModel):
    statement_id: str
    lesson_id: str
    status: Literal["opened", "completed"]
    module_complete: bool


class QuizItem(BaseModel):
    id: str
    kind: Literal["single", "multiple"]
    prompt: str
    choices: list[str]


class QuizAttempt(BaseModel):
    attempt_id: str
    scope: Literal["module", "final"]
    target_id: str
    started_at: str
    pass_mark_pct: int
    items: list[QuizItem]


class GradedItem(BaseModel):
    item_id: str
    correct: bool
    chosen: list[int]
    correct_choices: list[int]
    explain: str
    source_ref: str


class CertificateCode(BaseModel):
    certificate_id: str
    path_id: str
    code: str
    verify_path: str


class QuizResult(BaseModel):
    attempt_id: str
    scope: Literal["module", "final"]
    target_id: str
    score_pct: float
    pass_mark_pct: int
    passed: bool
    retake_at: Optional[str]
    module_complete: bool
    items: list[GradedItem]
    certificate: Optional[CertificateCode]


class ReviewCard(BaseModel):
    module_id: str
    item: QuizItem
    card: FsrsCard


class ReviewDue(BaseModel):
    daily_cap: int
    target_retention: float
    cards: list[ReviewCard]


class ReviewResult(BaseModel):
    item_id: str
    correct: bool
    correct_choices: list[int]
    explain: str
    card: FsrsCard


class LabResult(BaseModel):
    module_id: str
    status: Literal["passed", "failed"]


class PinnedModule(BaseModel):
    module_id: str
    version: str


class Certificate(BaseModel):
    certificate_id: str
    path_id: str
    path_title: str
    issued_at: str
    expires_at: Optional[str]
    status: CertStatus
    module_versions: list[PinnedModule]


class CertificateList(BaseModel):
    certificates: list[Certificate]


class CertificateVerify(BaseModel):
    path_id: str
    path_title: str
    holder_display_name: Optional[str]
    issued_at: str
    expires_at: Optional[str]
    status: CertStatus
    module_versions: list[PinnedModule]


class LearnProblem(BaseModel):
    error: str
    message: str
    retry_at: Optional[str] = None
