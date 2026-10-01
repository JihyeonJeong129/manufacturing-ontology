"""조건식 평가기 (validation_contract_version = rsw-conditions-v1).

SCHEMA.md 6장 계약:
- 조건식은 허용된 field/operator/value 로만 구성한다. 임의 코드 실행은 없다.
- 결과는 3값 논리: True(충족) / False(미충족) / None(정보 부족·모름).
- 모르는 필드·연산자·형식은 오류가 아니라 unknown(None)으로 취급하고 이유를 남긴다.
- {} 는 "조건 없음"(항상 충족)이다. 아직 작성하지 못한 조건은 review_status로 차단한다.

문법:
  {"all": [cond, ...]} | {"any": [cond, ...]} | {"not": cond}
  {"field": "<허용 필드>", "op": "eq|ne|in|not_in|lt|le|gt|ge|exists", "value": <값>}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ALLOWED_FIELDS = frozenset({
    "operating_context.cycle_state",
    "operating_context.workpiece_type",
    "operating_context.shift",
    "asset_config.drive_type",
    "asset_config.manufacturer",
    "asset_config.model",
    "asset_config.controller_manufacturer",
    "asset_config.controller_model",
    "asset_config.controller_firmware",
    "asset_config.verification_status",
})
ALLOWED_OPS = frozenset({"eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "exists"})
# 이 값들은 "모름"과 같다. false/0으로 추정하지 않는다.
UNKNOWN_VALUES = (None, "unknown")

FIELD_LABELS = {
    "operating_context.cycle_state": "운전 상태",
    "operating_context.workpiece_type": "작업물 종류",
    "operating_context.shift": "근무조",
    "asset_config.drive_type": "구동 방식",
    "asset_config.manufacturer": "장비 제조사",
    "asset_config.model": "장비 모델",
    "asset_config.controller_manufacturer": "제어기 제조사",
    "asset_config.controller_model": "제어기 모델",
    "asset_config.controller_firmware": "제어기 firmware",
    "asset_config.verification_status": "장비 구성 확인 상태",
}
VALUE_LABELS = {"welding": "용접 중", "non_welding": "비용접 상태"}
OP_LABELS = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}


@dataclass
class Evaluation:
    result: bool | None
    unknown_fields: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        return {True: "true", False: "false", None: "unknown"}[self.result]


def is_empty(condition: Any) -> bool:
    return condition is None or condition == {}


def evaluate(condition: Any, facts: dict[str, Any]) -> Evaluation:
    """조건식을 facts에 대해 평가한다. facts 값이 None/'unknown'이면 모름으로 본다."""
    if is_empty(condition):
        return Evaluation(True)
    return _eval(condition, facts)


def _eval(node: Any, facts: dict[str, Any]) -> Evaluation:
    if not isinstance(node, dict):
        return Evaluation(None, errors=["조건식 노드는 객체여야 합니다."])
    keys = set(node)
    if keys == {"all"} or keys == {"any"}:
        items = node.get("all", node.get("any"))
        if not isinstance(items, list) or not items:
            return Evaluation(None, errors=["all/any는 비어 있지 않은 배열이어야 합니다."])
        parts = [_eval(item, facts) for item in items]
        merged = Evaluation(None)
        for part in parts:
            merged.unknown_fields |= part.unknown_fields
            merged.errors += part.errors
        results = [p.result for p in parts]
        if "all" in node:
            merged.result = False if False in results else (None if None in results else True)
        else:
            merged.result = True if True in results else (None if None in results else False)
        if merged.result is not None:
            # 결론이 났으면 결론에 영향 없는 미확인 필드는 질문하지 않는다.
            merged.unknown_fields = set()
        return merged
    if keys == {"not"}:
        inner = _eval(node["not"], facts)
        inner.result = None if inner.result is None else not inner.result
        return inner
    return _leaf(node, facts)


def _leaf(node: dict[str, Any], facts: dict[str, Any]) -> Evaluation:
    field_name = node.get("field")
    op = node.get("op")
    if set(node) - {"field", "op", "value"}:
        return Evaluation(None, errors=[f"알 수 없는 조건 키: {sorted(set(node) - {'field', 'op', 'value'})}"])
    if field_name not in ALLOWED_FIELDS:
        return Evaluation(None, errors=[f"허용되지 않은 필드: {field_name!r}"])
    if op not in ALLOWED_OPS:
        return Evaluation(None, errors=[f"허용되지 않은 연산자: {op!r}"])
    actual = facts.get(field_name)
    if op == "exists":
        return Evaluation(actual not in UNKNOWN_VALUES)
    if actual in UNKNOWN_VALUES:
        return Evaluation(None, unknown_fields={field_name})
    if "value" not in node:
        return Evaluation(None, errors=[f"{op} 연산에는 value가 필요합니다."])
    expected = node["value"]
    try:
        if op == "eq":
            return Evaluation(_same(actual, expected))
        if op == "ne":
            return Evaluation(not _same(actual, expected))
        if op in ("in", "not_in"):
            if not isinstance(expected, list):
                return Evaluation(None, errors=[f"{op} 연산의 value는 배열이어야 합니다."])
            found = any(_same(actual, item) for item in expected)
            return Evaluation(found if op == "in" else not found)
        if not _is_number(actual) or not _is_number(expected):
            return Evaluation(None, errors=[f"{op} 연산은 수치에만 적용됩니다."])
        return Evaluation({"lt": actual < expected, "le": actual <= expected,
                           "gt": actual > expected, "ge": actual >= expected}[op])
    except TypeError:
        return Evaluation(None, errors=[f"{field_name} 값과 비교 값의 타입이 맞지 않습니다."])


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(a: Any, b: Any) -> bool:
    # true == 1 같은 암묵 변환을 허용하지 않는다.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    return type(a) is type(b) and a == b


def describe(condition: Any) -> str | None:
    """분기 조건의 한국어 요약 (condition_summary)."""
    if is_empty(condition):
        return None
    return _describe(condition)


def _describe(node: Any) -> str:
    if not isinstance(node, dict):
        return "조건"
    if "all" in node and isinstance(node["all"], list):
        return " 그리고 ".join(_describe(x) for x in node["all"])
    if "any" in node and isinstance(node["any"], list):
        return "(" + " 또는 ".join(_describe(x) for x in node["any"]) + ")"
    if "not" in node:
        return f"아님({_describe(node['not'])})"
    label = FIELD_LABELS.get(str(node.get("field")), str(node.get("field")))
    op = node.get("op")
    value = node.get("value")
    if op == "exists":
        return f"{label} 확인됨"
    if op in ("in", "not_in") and isinstance(value, list):
        values = ", ".join(str(VALUE_LABELS.get(v, v)) for v in value)
        return f"{label} {'∈' if op == 'in' else '∉'} {{{values}}}"
    return f"{label} {OP_LABELS.get(op, op)} {VALUE_LABELS.get(value, value)}"
