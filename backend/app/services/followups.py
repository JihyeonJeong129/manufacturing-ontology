"""추가 확인 질문 카탈로그 (서버 계약 허용 목록).

작업자가 답할 수 있는 필드는 여기 등록된 운전 맥락뿐이다. 장비 구성(asset_config.*)이나
적용성 승인은 작업자 답변으로 바꿀 수 없다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FollowUpDefinition:
    question_key: str
    field: str
    prompt: str
    value_type: str
    options: tuple[tuple[Any, str], ...] = ()
    unit: str | None = None

    def to_question(self, *, required: bool = True, prompt: str | None = None) -> dict[str, Any]:
        return {
            "question_key": self.question_key,
            "field": self.field,
            "prompt": prompt or self.prompt,
            "value_type": self.value_type,
            "required": required,
            "options": [{"value": value, "label": label} for value, label in self.options],
            "unit": self.unit,
        }


CATALOG: dict[str, FollowUpDefinition] = {
    d.field: d
    for d in (
        FollowUpDefinition(
            "cycle_state", "operating_context.cycle_state", "경보 발생 당시 용접 중이었나요?", "string",
            (("welding", "용접 중"), ("non_welding", "비용접 상태")),
        ),
        FollowUpDefinition("workpiece_type", "operating_context.workpiece_type", "경보 발생 당시 작업물 종류는 무엇이었나요?", "string"),
        FollowUpDefinition("shift", "operating_context.shift", "경보가 발생한 근무조는 언제였나요?", "string"),
    )
}
BY_KEY = {d.question_key: d for d in CATALOG.values()}


def required_context_fields(required_context: Any) -> list[tuple[str, str | None]]:
    """scenarios.required_context 항목을 (field, prompt override) 목록으로 해석한다.

    항목은 "operating_context.cycle_state" 같은 필드 문자열이거나
    {"field": ..., "prompt": ...} 객체다. 카탈로그에 없는 필드는 무시하지 않고 그대로 돌려준다.
    """
    result: list[tuple[str, str | None]] = []
    if not isinstance(required_context, list):
        return result
    for item in required_context:
        if isinstance(item, str):
            result.append((item, None))
        elif isinstance(item, dict) and isinstance(item.get("field"), str):
            prompt = item.get("prompt") if isinstance(item.get("prompt"), str) else None
            result.append((item["field"], prompt))
    return result


def check_answer_value(question: dict[str, Any], value: Any, unit: Any) -> str | None:
    """이전 답변의 follow_up_question 계약에 맞는지 검사한다. 오류 메시지 또는 None."""
    value_type = question.get("value_type")
    if value_type == "string" and not isinstance(value, str):
        return "문자열 값이어야 합니다."
    if value_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return "숫자 값이어야 합니다."
    if value_type == "boolean" and not isinstance(value, bool):
        return "boolean 값이어야 합니다."
    options = question.get("options") or []
    if options:
        allowed = [o.get("value") for o in options]
        if not any(type(v) is type(value) and v == value for v in allowed):
            return f"허용된 선택지가 아닙니다: {allowed}"
    expected_unit = question.get("unit")
    if expected_unit is None and unit is not None:
        return "이 질문은 단위를 받지 않습니다."
    if expected_unit is not None and unit != expected_unit:
        return f"단위는 {expected_unit!r}이어야 합니다."
    return None
