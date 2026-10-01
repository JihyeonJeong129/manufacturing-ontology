"""질문·답변·피드백 DTO. docs/design/api/openapi.json 과 일치시킨다."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StrictStr, field_validator, model_validator

from app.schemas.common import Notice, Output, Scalar, StrictInput, UtcDatetime, WorkError, finite

FEEDBACK_CATEGORIES = (
    "wrong_equipment", "wrong_code_mapping", "wrong_source", "unsupported_claim",
    "missing_context", "incorrect_procedure", "incorrect_citation", "other",
)


class ContextAnswer(StrictInput):
    question_key: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    value: Scalar
    unit: Annotated[StrictStr, Field(max_length=64)] | None = None

    @model_validator(mode="after")
    def _finite(self) -> "ContextAnswer":
        if isinstance(self.value, float):
            finite(self.value, "value")
        return self


class QuestionInput(StrictInput):
    context_id: UUID
    question: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    reply_to_answer_id: UUID | None = None
    context_answers: Annotated[list[ContextAnswer], Field(max_length=20)] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("공백만 있는 질문은 허용되지 않습니다.")
        return value

    @model_validator(mode="after")
    def _reply_required(self) -> "QuestionInput":
        if self.context_answers and self.reply_to_answer_id is None:
            raise ValueError("context_answers가 있으면 reply_to_answer_id가 필요합니다.")
        keys = [a.question_key for a in self.context_answers]
        if len(keys) != len(set(keys)):
            raise ValueError("context_answers의 question_key가 중복되었습니다.")
        return self


class QuestionAccepted(Output):
    answer_id: UUID
    alert_id: UUID
    context_id: UUID
    knowledge_release_id: UUID
    status: Literal["pending"]
    status_url: str
    created_at: UtcDatetime


class FollowUpOption(Output):
    value: Any
    label: str


class FollowUpQuestion(Output):
    question_key: str
    field: str
    prompt: str
    value_type: Literal["string", "number", "boolean"]
    required: bool
    options: list[FollowUpOption]
    unit: str | None


class Locator(Output):
    kind: Literal["pdf_page", "logical_block"]
    page_number: Annotated[int, Field(ge=1)]
    printed_page_label: str | None
    char_start: Annotated[int, Field(ge=0)] | None
    char_end: Annotated[int, Field(ge=1)] | None
    bbox: Annotated[list[Annotated[float, Field(ge=0, le=1)]], Field(min_length=4, max_length=4)] | None


class Citation(Output):
    citation_id: UUID
    claim_key: str
    section_id: UUID
    section_title: str
    section_number: str | None
    span_id: UUID
    document_revision_id: UUID
    document_title: str
    document_url: str | None
    quote: str
    reference_scope: Literal["applicable", "analogous", "comparison", "unknown"]
    locator: Locator


class EventEvidenceRef(Output):
    kind: Literal["observed_code", "observation", "ml_assessment"]
    id: UUID


class Claim(Output):
    claim_key: str
    type: Literal["observation", "hypothesis", "manual_fact", "procedure_guidance", "limitation"]
    text: str
    citation_ids: list[UUID]
    event_evidence: list[EventEvidenceRef]


class ProcedureStep(Output):
    step_number: Annotated[int, Field(ge=1)]
    claim_key: str
    instruction: str
    expected_result: str | None
    condition_summary: str | None
    condition_state: Literal["unconditional", "satisfied"]
    citation_ids: Annotated[list[UUID], Field(min_length=1)]


class ProcedureRecommendation(Output):
    procedure_id: UUID
    title: str
    required_role: str
    preconditions: list[str]
    applicability_ids: Annotated[list[UUID], Field(min_length=1)]
    steps: Annotated[list[ProcedureStep], Field(min_length=1)]


class AnswerResult(Output):
    summary: str
    claims: list[Claim]
    procedures: list[ProcedureRecommendation]
    follow_up_questions: list[FollowUpQuestion]
    citations: list[Citation]
    notices: list[Notice]


class AnswerDetails(Output):
    answer_id: UUID
    alert_id: UUID
    context_id: UUID
    knowledge_release_id: UUID
    reply_to_answer_id: UUID | None
    status: Literal["pending", "running", "answered", "needs_context", "insufficient_evidence", "failed"]
    question: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    result: AnswerResult | None
    error: WorkError | None


class FeedbackInput(StrictInput):
    verdict: Literal["acceptable", "needs_revision", "incorrect"]
    error_categories: Annotated[list[Literal[FEEDBACK_CATEGORIES]], Field(max_length=8)]  # type: ignore[valid-type]
    comment: Annotated[StrictStr, Field(max_length=2000)] | None = None
    supersedes_id: UUID | None = None

    @model_validator(mode="after")
    def _rules(self) -> "FeedbackInput":
        if len(set(self.error_categories)) != len(self.error_categories):
            raise ValueError("error_categories에 중복 항목이 있습니다.")
        if self.verdict == "acceptable" and self.error_categories:
            raise ValueError("verdict='acceptable'이면 error_categories는 빈 배열이어야 합니다.")
        if self.verdict != "acceptable" and not self.error_categories:
            raise ValueError("verdict가 acceptable이 아니면 오류 유형이 최소 1개 필요합니다.")
        if "other" in self.error_categories and (self.comment is None or not self.comment.strip()):
            raise ValueError("error_categories에 'other'가 있으면 공백이 아닌 comment가 필요합니다.")
        return self


class FeedbackCreated(Output):
    feedback_id: UUID
    answer_id: UUID
    reviewer_id: UUID
    verdict: Literal["acceptable", "needs_revision", "incorrect"]
    error_categories: list[str]
    comment: str | None
    supersedes_id: UUID | None
    created_at: UtcDatetime
