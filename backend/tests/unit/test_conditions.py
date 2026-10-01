import pytest

from app.services import conditions as c

FACTS = {
    "operating_context.cycle_state": "non_welding",
    "operating_context.shift": None,
    "operating_context.workpiece_type": "unknown",
    "asset_config.drive_type": "servo_pneumatic",
    "asset_config.controller_firmware": None,
    "asset_config.model": "M1",
}


def leaf(field, op, value=None):
    node = {"field": field, "op": op}
    if value is not None:
        node["value"] = value
    return node


def test_empty_condition_means_no_condition():
    assert c.evaluate({}, FACTS).result is True
    assert c.evaluate(None, FACTS).result is True


@pytest.mark.parametrize("node, expected", [
    (leaf("operating_context.cycle_state", "eq", "non_welding"), True),
    (leaf("operating_context.cycle_state", "eq", "welding"), False),
    (leaf("operating_context.cycle_state", "ne", "welding"), True),
    (leaf("asset_config.drive_type", "in", ["servo_pneumatic", "pneumatic"]), True),
    (leaf("asset_config.drive_type", "not_in", ["servo_pneumatic"]), False),
    (leaf("asset_config.model", "exists"), True),
    (leaf("asset_config.controller_firmware", "exists"), False),
])
def test_leaf_operators(node, expected):
    assert c.evaluate(node, FACTS).result is expected


def test_unknown_values_are_not_treated_as_false():
    for field in ("operating_context.shift", "operating_context.workpiece_type", "asset_config.controller_firmware"):
        evaluation = c.evaluate(leaf(field, "eq", "x"), FACTS)
        assert evaluation.result is None
        assert evaluation.unknown_fields == {field}


def test_three_valued_all_any_not():
    known_true = leaf("operating_context.cycle_state", "eq", "non_welding")
    known_false = leaf("operating_context.cycle_state", "eq", "welding")
    unknown = leaf("operating_context.shift", "eq", "night")
    assert c.evaluate({"all": [known_true, unknown]}, FACTS).result is None
    assert c.evaluate({"all": [known_false, unknown]}, FACTS).result is False
    assert c.evaluate({"any": [known_true, unknown]}, FACTS).result is True
    assert c.evaluate({"any": [known_false, unknown]}, FACTS).result is None
    assert c.evaluate({"not": known_true}, FACTS).result is False
    assert c.evaluate({"not": unknown}, FACTS).result is None


def test_decided_result_does_not_request_irrelevant_fields():
    evaluation = c.evaluate({"all": [leaf("operating_context.cycle_state", "eq", "welding"),
                                     leaf("operating_context.shift", "eq", "night")]}, FACTS)
    assert evaluation.result is False and evaluation.unknown_fields == set()


@pytest.mark.parametrize("node", [
    leaf("operator.free_text", "eq", "x"),                       # 허용되지 않은 필드
    leaf("operating_context.cycle_state", "regex", ".*"),        # 허용되지 않은 연산자
    {"field": "operating_context.cycle_state", "op": "eq", "value": "x", "code": "__import__('os')"},
    {"all": []},
    "operating_context.cycle_state == 'x'",
    leaf("asset_config.drive_type", "gt", 3),                   # 문자열 값에 수치 비교
    leaf("asset_config.drive_type", "in", "servo_pneumatic"),   # in 값이 배열이 아님
])
def test_invalid_expressions_become_unknown_with_errors(node):
    evaluation = c.evaluate(node, FACTS)
    assert evaluation.result is None
    assert evaluation.errors


def test_no_implicit_type_coercion():
    facts = {"asset_config.model": 1}
    assert c.evaluate(leaf("asset_config.model", "eq", True), facts).result is False
    assert c.evaluate(leaf("asset_config.model", "eq", "1"), facts).result is False
    assert c.evaluate(leaf("asset_config.model", "eq", 1.0), facts).result is True
    assert c.evaluate(leaf("asset_config.model", "ge", 1), facts).result is True


def test_describe_summaries():
    assert c.describe({}) is None
    text = c.describe({"all": [leaf("operating_context.cycle_state", "eq", "non_welding")]})
    assert text == "운전 상태 = 비용접 상태"
    assert "또는" in c.describe({"any": [leaf("asset_config.model", "eq", "A"), leaf("asset_config.model", "eq", "B")]})
