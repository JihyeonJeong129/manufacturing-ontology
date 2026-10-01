"""답변 결과 계약 검사. 커밋 전에 실행해 근거 없는 주장·잘못된 인용 연결을 막는다 (API_SPEC.md 6장)."""
from __future__ import annotations

from typing import Any

from app.schemas.answers import AnswerResult

COMPLETED = ("answered", "needs_context", "insufficient_evidence")


class AnswerInvariantError(ValueError):
    pass


def check_answer(status: str, result: dict[str, Any], allowed_event_ids: set[str]) -> None:
    if status not in COMPLETED:
        raise AnswerInvariantError(f"unexpected status {status}")
    AnswerResult.model_validate(result)  # 형식 계약

    claims = result["claims"]
    citations = {c["citation_id"]: c for c in (dict(x, citation_id=str(x["citation_id"])) for x in result["citations"])}
    keys = [c["claim_key"] for c in claims]
    if len(keys) != len(set(keys)):
        raise AnswerInvariantError("claim_key must be unique within an answer")
    by_key = {c["claim_key"]: c for c in claims}

    for citation in citations.values():
        if citation["claim_key"] not in by_key:
            raise AnswerInvariantError(f"citation {citation['citation_id']} points to unknown claim {citation['claim_key']}")
    for claim in claims:
        for cid in map(str, claim["citation_ids"]):
            if cid not in citations:
                raise AnswerInvariantError(f"claim {claim['claim_key']} cites unknown citation {cid}")
            if citations[cid]["claim_key"] != claim["claim_key"]:
                raise AnswerInvariantError(f"citation {cid} belongs to another claim")
        for ref in claim["event_evidence"]:
            if str(ref["id"]) not in allowed_event_ids:
                raise AnswerInvariantError(f"claim {claim['claim_key']} references event data outside the context")
        if claim["type"] in ("manual_fact", "procedure_guidance") and not claim["citation_ids"]:
            raise AnswerInvariantError(f"{claim['type']} claim {claim['claim_key']} requires a source citation")
        if claim["type"] == "hypothesis" and not (claim["citation_ids"] or claim["event_evidence"]):
            raise AnswerInvariantError(f"hypothesis claim {claim['claim_key']} has no evidence")

    for procedure in result["procedures"]:
        for step in procedure["steps"]:
            claim = by_key.get(step["claim_key"])
            if claim is None or claim["type"] != "procedure_guidance":
                raise AnswerInvariantError("procedure step must link to a procedure_guidance claim")
            for cid in map(str, step["citation_ids"]):
                if cid not in citations:
                    raise AnswerInvariantError("procedure step cites unknown citation")
                if citations[cid]["reference_scope"] != "applicable":
                    raise AnswerInvariantError("procedure steps may cite only applicable sources")
            if step["condition_state"] == "satisfied" and not step["condition_summary"]:
                raise AnswerInvariantError("satisfied step requires condition_summary")

    if status in ("needs_context", "insufficient_evidence") and result["procedures"]:
        raise AnswerInvariantError(f"{status} answers must not include procedures")
    if status == "needs_context" and not result["follow_up_questions"]:
        raise AnswerInvariantError("needs_context requires at least one follow-up question")
