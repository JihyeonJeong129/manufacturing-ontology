import copy
import uuid

import pytest

from app.services.answering.invariants import AnswerInvariantError, check_answer
from app.services.answering.pipeline import build_facts
from app.services.normalizer import decide_context_status, value_matches_type
from tests.contract import examples

EVENT_ID = "00000000-0000-4000-8000-000000000013"


def needs_context_result():
    return copy.deepcopy(examples()["answer_needs_context"]["result"])


def answered_result():
    return copy.deepcopy(examples()["answer_answered"]["result"])


def with_procedure(scope="applicable"):
    result = answered_result()
    cid = str(uuid.uuid4())
    result["claims"].append({"claim_key": "p1-s1", "type": "procedure_guidance", "text": "점검", "citation_ids": [cid], "event_evidence": []})
    citation = copy.deepcopy(result["citations"][0])
    citation.update(citation_id=cid, claim_key="p1-s1", reference_scope=scope)
    result["citations"].append(citation)
    result["procedures"] = [{
        "procedure_id": str(uuid.uuid4()), "title": "t", "required_role": "r", "preconditions": [],
        "applicability_ids": [str(uuid.uuid4())],
        "steps": [{"step_number": 1, "claim_key": "p1-s1", "instruction": "점검", "expected_result": None,
                   "condition_summary": None, "condition_state": "unconditional", "citation_ids": [cid]}],
    }]
    return result


def test_contract_examples_satisfy_invariants():
    check_answer("needs_context", needs_context_result(), {EVENT_ID})
    check_answer("answered", answered_result(), set())
    check_answer("answered", with_procedure(), set())


@pytest.mark.parametrize("status, mutate", [
    ("needs_context", lambda r: r.update(follow_up_questions=[])),
    ("needs_context", lambda r: r["claims"][0]["event_evidence"][0].update(id=str(uuid.uuid4()))),  # 다른 context의 이벤트
    ("answered", lambda r: r["claims"][0].update(citation_ids=[])),                                 # 근거 없는 manual_fact
    ("answered", lambda r: r["citations"][0].update(claim_key="missing")),
    ("answered", lambda r: r["claims"].append(dict(r["claims"][0]))),                              # 중복 claim_key
    ("answered", lambda r: r["claims"][0].update(citation_ids=[str(uuid.uuid4())])),
    ("insufficient_evidence", lambda r: r.update(**with_procedure())),
    ("answered", lambda r: r.update(**with_procedure(scope="analogous"))),                          # 유사 자료로 절차 안내
    ("answered", lambda r: r.update(**{**with_procedure(), "procedures": [
        {**with_procedure()["procedures"][0], "steps": [{**with_procedure()["procedures"][0]["steps"][0], "claim_key": "nope"}]}]})),
    ("failed", lambda r: None),
])
def test_invariant_violations(status, mutate):
    result = needs_context_result() if status == "needs_context" else answered_result()
    mutate(result)
    with pytest.raises((AnswerInvariantError, ValueError)):
        check_answer(status, result, {EVENT_ID})


def test_hypothesis_claim_needs_some_evidence():
    result = answered_result()
    result["claims"].append({"claim_key": "h", "type": "hypothesis", "text": "원인 후보", "citation_ids": [], "event_evidence": []})
    with pytest.raises(AnswerInvariantError):
        check_answer("answered", result, set())


@pytest.mark.parametrize("kwargs, expected", [
    (dict(invalid=True, asset_found=True, config_verified=True, code_statuses=["mapped"], observation_statuses=[], ml_mapped=[]), "invalid"),
    (dict(invalid=False, asset_found=True, config_verified=True, code_statuses=["mapped"], observation_statuses=["mapped"], ml_mapped=[True]), "mapped"),
    (dict(invalid=False, asset_found=True, config_verified=False, code_statuses=["mapped"], observation_statuses=[], ml_mapped=[]), "partial"),
    (dict(invalid=False, asset_found=True, config_verified=True, code_statuses=["mapped", "unmapped"], observation_statuses=[], ml_mapped=[]), "partial"),
    # API 명세 예시: 장비는 식별됐지만 구성·코드·관측·ML이 모두 미매핑 → partial
    (dict(invalid=False, asset_found=True, config_verified=False, code_statuses=["unmapped"], observation_statuses=["unmapped"], ml_mapped=[False]), "partial"),
    (dict(invalid=False, asset_found=False, config_verified=False, code_statuses=["unmapped"], observation_statuses=[], ml_mapped=[False]), "unmapped"),
    (dict(invalid=False, asset_found=False, config_verified=False, code_statuses=["mapped"], observation_statuses=[], ml_mapped=[]), "partial"),
])
def test_context_status_rules(kwargs, expected):
    assert decide_context_status(**kwargs) == expected


@pytest.mark.parametrize("value, value_type, ok", [
    (1.5, "number", True), (2, "number", True), (True, "number", False), ("1", "number", False),
    (True, "boolean", True), (0, "boolean", False), ("x", "string", True), (None, "number", True), (3, "unknown", True),
])
def test_observation_type_check(value, value_type, ok):
    assert value_matches_type(value, value_type) is ok


def test_operator_inputs_fill_unknown_but_never_override_known_values():
    config = {"drive_type": "servo_pneumatic", "manufacturer": "M", "model": "X", "controller_manufacturer": None,
              "controller_model": "C", "controller_firmware": None, "verification_status": "verified"}
    operator = {
        "operating_context.cycle_state": {"value": "non_welding"},
        "operating_context.shift": {"value": "night"},
        "asset_config.model": {"value": "Y"},  # 카탈로그 밖: 장비 구성은 작업자 답변으로 바꿀 수 없다
    }
    facts, provenance, conflicts = build_facts({"cycle_state": "unknown", "shift": "day"}, config, operator)
    assert facts["operating_context.cycle_state"] == "non_welding"
    assert provenance["operating_context.cycle_state"] == "operator_reported"
    assert facts["operating_context.shift"] is None and provenance["operating_context.shift"] == "conflict"
    assert conflicts == [{"field": "operating_context.shift", "event_value": "day", "operator_value": "night"}]
    assert facts["asset_config.model"] == "X"
