"""장비 적용성 판정. 'analogous/comparison/unknown'을 'applicable'로 취급하지 않는다."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.jsonutil import as_json
from app.services.conditions import evaluate

SCOPE_PRIORITY = ("analogous", "comparison", "unknown")


def assess(rows: list[dict[str, Any]], facts: dict[str, Any], asset_config_id: UUID | None) -> dict[str, Any]:
    """한 콘텐츠(절/절차/수치)에 대한 검토 완료 적용성 행들을 평가한다.

    반환 scope: applicable | analogous | comparison | unknown | excluded(조건 미충족)
    """
    if asset_config_id is None:
        return _result("unknown", "not_evaluated", [], [], "asset_config_unresolved")
    if not rows:
        return _result("unknown", "not_evaluated", [], [], "no_reviewed_applicability")
    applicable = [r for r in rows if r["reference_scope"] == "applicable"]
    others = [r for r in rows if r["reference_scope"] != "applicable"]
    evaluations = [(r, evaluate(as_json(r["conditions"]), facts)) for r in applicable]
    true_rows = [r for r, e in evaluations if e.result is True]
    unknown = [(r, e) for r, e in evaluations if e.result is None]
    false_rows = [r for r, e in evaluations if e.result is False]
    errors = [err for _, e in evaluations for err in e.errors]

    if true_rows and not others:
        return _result("applicable", "true", [r["id"] for r in true_rows], [], "reviewed_applicable", errors)
    if true_rows and others:
        scope = _conservative(others)
        return _result(scope, "true", [r["id"] for r in rows], [], "conflicting_applicability", errors)
    if unknown:
        fields = sorted({f for _, e in unknown for f in e.unknown_fields})
        return _result("unknown", "unknown", [r["id"] for r, _ in unknown], fields,
                       "conditions_unknown" if fields else "conditions_invalid", errors)
    if others:
        return _result(_conservative(others), "not_evaluated", [r["id"] for r in others], [], "reviewed_reference_only", errors)
    if false_rows:
        return _result("excluded", "false", [r["id"] for r in false_rows], [], "conditions_not_met", errors)
    return _result("unknown", "not_evaluated", [], [], "no_reviewed_applicability", errors)


def _conservative(rows: list[dict[str, Any]]) -> str:
    scopes = {r["reference_scope"] for r in rows}
    for scope in SCOPE_PRIORITY:
        if scope in scopes:
            return scope
    return "unknown"


def _result(scope: str, condition: str, ids: list[Any], unknown_fields: list[str], reason: str,
            errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "scope": scope,
        "condition_result": condition,
        "applicability_ids": [str(i) for i in ids],
        "unknown_fields": unknown_fields,
        "reason": reason,
        "condition_errors": errors or [],
    }


def citation_scope(assessment: dict[str, Any]) -> str:
    scope = assessment["scope"]
    return scope if scope in ("applicable", "analogous", "comparison") else "unknown"
