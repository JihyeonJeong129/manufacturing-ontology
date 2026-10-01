from uuid import UUID, uuid4

from app.services.answering import applicability as appl
from app.services.answering import fusion
from app.services.followups import CATALOG, check_answer_value, required_context_fields

CONFIG = uuid4()
COND_WELD = {"all": [{"field": "operating_context.cycle_state", "op": "eq", "value": "welding"}]}


def row(scope, conditions=None):
    return {"id": uuid4(), "reference_scope": scope, "conditions": conditions or {}}


def test_no_asset_config_or_rows_is_unknown():
    assert appl.assess([row("applicable")], {}, None)["scope"] == "unknown"
    assert appl.assess([], {}, CONFIG)["reason"] == "no_reviewed_applicability"


def test_applicable_requires_conditions_true():
    facts = {"operating_context.cycle_state": "welding"}
    assert appl.assess([row("applicable", COND_WELD)], facts, CONFIG)["scope"] == "applicable"
    excluded = appl.assess([row("applicable", COND_WELD)], {"operating_context.cycle_state": "non_welding"}, CONFIG)
    assert excluded["scope"] == "excluded" and excluded["condition_result"] == "false"
    unknown = appl.assess([row("applicable", COND_WELD)], {"operating_context.cycle_state": "unknown"}, CONFIG)
    assert unknown["scope"] == "unknown" and unknown["unknown_fields"] == ["operating_context.cycle_state"]


def test_analogous_is_never_promoted_to_applicable():
    assert appl.assess([row("analogous")], {}, CONFIG)["scope"] == "analogous"
    conflicting = appl.assess([row("applicable"), row("comparison")], {}, CONFIG)
    assert conflicting["scope"] == "comparison" and conflicting["reason"] == "conflicting_applicability"
    assert appl.citation_scope({"scope": "excluded"}) == "unknown"


def test_fusion_prefers_code_and_graph_and_drops_excluded():
    a, b, c, d = (UUID(int=i) for i in range(1, 5))
    fused = fusion.fuse({"keyword": [c, b], "code": [a], "graph": [b]}, excluded={d})
    assert [cid for cid, _ in fused][:2] == [b, a]  # b: graph+keyword, a: code
    assert all(cid != d for cid, _ in fusion.fuse({"keyword": [d, a]}, excluded={d}))


def test_keyword_query_only_uses_word_characters():
    query = fusion.keyword_query(["경보가 뭐야? DEMO-E01'); DROP TABLE x; --", "a"])
    assert query == "경보가:* | 뭐야:* | demo:* | e01:* | drop:* | table:*"
    assert fusion.keyword_query(["!", ""]) is None


def test_follow_up_catalog_and_answer_checks():
    question = CATALOG["operating_context.cycle_state"].to_question()
    assert question["question_key"] == "cycle_state" and question["required"] is True
    assert check_answer_value(question, "non_welding", None) is None
    assert check_answer_value(question, "idle", None)
    assert check_answer_value(question, 1, None)
    assert check_answer_value(question, "welding", "s")
    number_q = {"value_type": "number", "options": [], "unit": "s"}
    assert check_answer_value(number_q, True, "s") == "숫자 값이어야 합니다."
    assert check_answer_value(number_q, 1.5, "ms")
    assert check_answer_value(number_q, 2, "s") is None


def test_required_context_formats():
    assert required_context_fields(["operating_context.cycle_state", {"field": "operating_context.shift", "prompt": "근무조?"}, 3]) == [
        ("operating_context.cycle_state", None), ("operating_context.shift", "근무조?")]
    assert required_context_fields({"not": "a list"}) == []
