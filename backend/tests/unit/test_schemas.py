"""요청 DTO가 output/api 계약(examples.json, validate_contract.py 부정 사례)과 일치하는지 검사한다."""
import copy
import json

import pytest
from pydantic import ValidationError

from app.schemas.alerts import AlertAccepted, AlertDetails, AlertInput
from app.schemas.answers import (AnswerDetails, Citation, FeedbackCreated, FeedbackInput, QuestionAccepted,
                                 QuestionInput)
from app.schemas.common import Problem
from tests.contract import assert_contract, contract_errors, examples

INPUTS = {
    "alert_request": AlertInput, "question_request": QuestionInput,
    "followup_request": QuestionInput, "feedback_request": FeedbackInput,
}
OUTPUTS = {
    "alert_created": AlertAccepted, "alert_duplicate": AlertAccepted, "alert_details": AlertDetails,
    "question_accepted": QuestionAccepted, "answer_pending": AnswerDetails, "answer_needs_context": AnswerDetails,
    "answer_answered": AnswerDetails, "answer_failed": AnswerDetails, "citation_example": Citation,
    "feedback_created": FeedbackCreated, "conflict_problem": Problem,
}


def parse(model, value):
    return model.model_validate_json(json.dumps(value), strict=True)


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_contract_input_examples_are_accepted(name):
    parse(INPUTS[name], examples()[name])


@pytest.mark.parametrize("name", sorted(OUTPUTS))
def test_contract_output_examples_round_trip_through_response_models(name):
    model = OUTPUTS[name]
    dumped = model.model_validate(examples()[name]).model_dump(mode="json")
    assert dumped == examples()[name]


def mutated(name, fn):
    value = copy.deepcopy(examples()[name])
    fn(value)
    return value


# validate_contract.py의 부정 사례 중 요청 DTO에 해당하는 것 + 추가 경계 사례
NEGATIVE = [
    ("missing producer", "alert_request", AlertInput, lambda v: v.pop("producer_key")),
    ("unexpected source authority", "alert_request", AlertInput, lambda v: v.update(release_id="forbidden")),
    ("probability > 1", "alert_request", AlertInput, lambda v: v["ml_assessments"][0]["score"].update(value=1.1)),
    ("classification without target", "alert_request", AlertInput, lambda v: v["ml_assessments"][0].pop("target")),
    ("calibration without reference", "alert_request", AlertInput,
     lambda v: v["ml_assessments"][0]["score"].update(calibration_status="calibrated")),
    ("valid observation without value", "alert_request", AlertInput, lambda v: v["observations"][0].pop("value")),
    ("naive timestamp", "alert_request", AlertInput, lambda v: v.update(occurred_at="2026-09-26T01:00:00")),
    ("followup missing parent", "followup_request", QuestionInput, lambda v: v.pop("reply_to_answer_id")),
    ("question bad UUID", "question_request", QuestionInput, lambda v: v.update(context_id="event-id")),
    ("acceptable with errors", "feedback_request", FeedbackInput, lambda v: v.update(verdict="acceptable")),
    ("other without comment", "feedback_request", FeedbackInput, lambda v: v.update(error_categories=["other"], comment=None)),
    ("client reviewer injection", "feedback_request", FeedbackInput, lambda v: v.update(reviewer_id="forbidden")),
    # 추가 경계 사례
    ("schema version", "alert_request", AlertInput, lambda v: v.update(schema_version="2.0")),
    ("window reversed", "alert_request", AlertInput,
     lambda v: v.update(observation_window={"start": "2026-09-26T01:00:00Z", "end": "2026-09-26T00:00:00Z"})),
    ("server-only _integration key", "alert_request", AlertInput, lambda v: v["operating_context"].update(_integration={})),
    ("null cycle_state", "alert_request", AlertInput, lambda v: v["operating_context"].update(cycle_state=None)),
    ("null operating_context", "alert_request", AlertInput, lambda v: v.update(operating_context=None)),
    ("bool is not a number", "alert_request", AlertInput, lambda v: v["ml_assessments"][0]["score"].update(value=True)),
    ("number as string", "alert_request", AlertInput, lambda v: v["ml_assessments"][0]["score"].update(value="0.5")),
    ("summary min > max", "alert_request", AlertInput,
     lambda v: v["observations"][0].update(value=None, summary={"min": 2, "max": 1})),
    ("summary mean outside", "alert_request", AlertInput,
     lambda v: v["observations"][0].update(value=None, summary={"min": 1, "max": 2, "mean": 3})),
    ("empty summary", "alert_request", AlertInput, lambda v: v["observations"][0].update(summary={})),
    ("too many codes", "alert_request", AlertInput,
     lambda v: v.update(observed_codes=[{"namespace": "n", "code": "c"}] * 65)),
    ("calibrated logit", "alert_request", AlertInput,
     lambda v: v["ml_assessments"][0]["score"].update(kind="logit", value=3.0, calibration_status="calibrated", calibration_ref="ref")),
    ("blank question", "question_request", QuestionInput, lambda v: v.update(question="   ")),
    ("question too long", "question_request", QuestionInput, lambda v: v.update(question="가" * 2001)),
    ("duplicate question keys", "followup_request", QuestionInput,
     lambda v: v.update(context_answers=v["context_answers"] * 2)),
    ("errors without category", "feedback_request", FeedbackInput, lambda v: v.update(error_categories=[])),
    ("duplicate category", "feedback_request", FeedbackInput, lambda v: v.update(error_categories=["other", "other"], comment="x")),
    ("unknown category", "feedback_request", FeedbackInput, lambda v: v.update(error_categories=["typo"])),
]


@pytest.mark.parametrize("label, name, model, fn", NEGATIVE, ids=[n[0] for n in NEGATIVE])
def test_negative_fixtures_are_rejected(label, name, model, fn):
    with pytest.raises(ValidationError):
        parse(model, mutated(name, fn))


DESIGN_NEGATIVE = [n for n in NEGATIVE if n[0] in {
    "missing producer", "unexpected source authority", "probability > 1", "classification without target",
    "calibration without reference", "valid observation without value", "followup missing parent",
    "acceptable with errors", "other without comment", "client reviewer injection"}]


@pytest.mark.parametrize("label, name, model, fn", DESIGN_NEGATIVE, ids=[n[0] for n in DESIGN_NEGATIVE])
def test_design_contract_also_rejects_them(label, name, model, fn):
    """같은 사례를 원본 openapi.json 스키마도 거절한다(DTO와 계약의 일치 확인)."""
    schema = {"alert_request": "AlertInput", "question_request": "QuestionInput",
              "followup_request": "QuestionInput", "feedback_request": "FeedbackInput"}[name]
    assert contract_errors(mutated(name, fn), schema)


def test_boundaries_that_must_be_accepted():
    alert = copy.deepcopy(examples()["alert_request"])
    alert["occurred_at"] = "2026-09-26T10:00:00+09:00"
    alert["observed_codes"] = []
    alert["observations"] = [{"feature_key": "k", "quality": "missing"},
                             {"feature_key": "k2", "quality": "valid", "summary": {"min": 1, "max": 1, "mean": 1}}]
    alert["ml_assessments"] = [{"kind": "anomaly", "model": {"name": "m", "version": "v"},
                                "score": {"kind": "anomaly_score", "value": 12, "calibration_status": "unknown"}}]
    alert.pop("operating_context")
    alert.pop("observation_window")
    parse(AlertInput, alert)
    assert_contract(alert, "AlertInput")
    parse(FeedbackInput, {"verdict": "acceptable", "error_categories": []})
    parse(QuestionInput, {"context_id": "00000000-0000-4000-8000-000000000002", "question": "질문", "context_answers": []})
