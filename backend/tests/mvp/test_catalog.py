"""카탈로그 자체 검사 (DB 없음). 정의서의 10개 상황, ML 핸드오프 ID, 관계 계약, 인용 위치를 확인한다."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.mvp import build
from app.mvp import catalog_knowledge as K
from app.mvp.catalog_sections import BY_ID as SECTIONS
from app.mvp.catalog_sections import DEFAULT_SCOPE
from app.mvp.sources import SOURCES

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "app" / "db" / "sql" / "rsw_schema.sql"

# 정의서 '오류 상황 10개 요약' 표: (종류, 고장, 에러 코드, ML 규칙)
DEFINITION_TABLE = {
    "S01": ("fault_response", "E01", "E012", {"P1"}),
    "S02": ("fault_response", "E02", "E016", {"P5", "P6"}),
    "S03": ("fault_response", "E03", "E028", {"P5"}),
    "S04": ("fault_response", "E04", "E029", {"P4"}),
    "S05": ("cause_exploration", None, None, {"P2"}),
    "S06": ("cause_exploration", None, None, {"P3"}),
    "S07": ("cause_exploration", None, None, {"P6"}),
    "S08": ("cause_exploration", None, None, {"P5"}),
    "S09": ("cause_exploration", None, None, {"P9"}),
    "S10": ("operational_context", None, None, {"P7", "P8"}),
}


def _relations(predicate: str) -> list[K.RelationDef]:
    return [r for r in K.RELATIONS if r.predicate == predicate]


def _kind(ref: str) -> str:
    if ref.startswith("sec:"):
        return "document_section"
    return {e.ref: e.kind for e in K.ENTITIES}[ref]


def test_ten_situations_follow_definition_table():
    scenarios = {s.entity.code: s for s in K.SCENARIOS}
    assert sorted(scenarios) == [f"S{i:02d}" for i in range(1, 11)]
    for sid, (kind, fault, code, rules) in DEFINITION_TABLE.items():
        assert scenarios[sid].scenario_kind == kind
        assert scenarios[sid].question_examples, sid
        related = {r.object for r in _relations("related_fault") if r.subject == f"scenario:{sid}"}
        assert related == ({f"fault:{fault}"} if fault else set()), sid
        if code:
            assert ("code:" + code, f"fault:{fault}") in {(r.subject, r.object) for r in _relations("maps_to")}
        triggers = {r.subject.split(":")[1] for r in _relations("triggers") if r.object == f"scenario:{sid}"}
        assert triggers == rules, sid
        procedures = [r for r in _relations("has_procedure") if r.subject == f"scenario:{sid}"]
        assert len(procedures) == 1, sid
        causes = [r for r in _relations("raises_hypothesis") if r.subject == f"scenario:{sid}"]
        assert len(causes) >= 2, sid
        documented = {r.object for r in _relations("documented_in") if r.subject == f"scenario:{sid}"}
        assert f"sec:D-{sid}" in documented


def test_ids_are_the_ml_handoff_ids():
    by_ns: dict[str, set[str]] = {}
    for entity in K.ENTITIES:
        by_ns.setdefault(entity.namespace, set()).add(entity.code)
    assert by_ns[K.NS_FAULT] == {"E01", "E02", "E03", "E04"}
    assert by_ns[K.NS_RULE] == {f"P{i}" for i in range(1, 10)}
    assert by_ns[K.NS_PATTERN] == {f"P{i}" for i in range(1, 10)}
    assert by_ns[K.NS_FEATURE] == {f"c{i}" for i in range(1, 19)}
    assert {"E012", "E016", "E028", "E029"} <= by_ns[K.NS_ERROR]
    # 데이터셋 코드와 Festo 제어기 오류 번호는 다른 namespace이며 서로 매핑하지 않는다.
    festo_refs = {e.ref for e in K.FESTO_CODES}
    assert not [r for r in _relations("maps_to") if r.subject in festo_refs]


def test_relation_endpoints_follow_relation_rules():
    block = SCHEMA_SQL.read_text(encoding="utf-8").split("INSERT INTO relation_rules VALUES", 1)[1].split("CREATE TABLE", 1)[0]
    allowed = set(re.findall(r"\('(\w+)','(\w+)','(\w+)'", block))
    assert len(allowed) == 18
    for relation in K.RELATIONS:
        assert (relation.predicate, _kind(relation.subject), _kind(relation.object)) in allowed, relation


def test_every_relation_has_resolvable_evidence():
    names = {sid: {s.name for s in d.spans + d.anchors} for sid, d in SECTIONS.items()}
    assert all(r.evidence for r in K.RELATIONS)
    for relation in K.RELATIONS:
        if relation.object.startswith("sec:"):
            assert relation.object[4:] in SECTIONS, relation
        for ref in relation.evidence:
            sid, _, name = ref.partition("#")
            assert sid in SECTIONS, ref
            assert not name or name in names[sid], ref
    # 관계마다 근거 수준이 정의서 표현과 맞는지: 데이터로 본 것만 supported
    supported = {(r.subject, r.object) for r in K.RELATIONS if r.empirical_status == "supported"}
    assert supported == {("code:E012", "fault:E01"), ("code:E016", "fault:E02"), ("code:E028", "fault:E03"),
                         ("code:E029", "fault:E04")}
    assert {r.empirical_status for r in _relations("may_contribute_to")} == {"hypothesis"}


def test_procedure_steps_cite_applicable_sources():
    assert len(K.PROCEDURES) == 10
    for procedure in K.PROCEDURES:
        assert procedure.steps
        assert DEFAULT_SCOPE[SECTIONS[procedure.source_section].doc] == "applicable"
        for step in procedure.steps:
            sid, _, name = step.source.partition("#")
            definition = SECTIONS[sid]
            assert DEFAULT_SCOPE[definition.doc] == "applicable", step.source
            assert name in {s.name for s in definition.spans + definition.anchors}, step.source


def test_numeric_specs_are_well_formed():
    for spec in K.SPECS:
        assert spec.unit and spec.comparison != "unresolved"
        if spec.comparison == "range":
            assert spec.value_low is not None and spec.value_high is not None and spec.value_low <= spec.value_high
        else:
            assert spec.value_low is not None and spec.value_high is None
        sid, _, name = spec.source.partition("#")
        assert name in {s.name for s in SECTIONS[sid].spans}, spec.source


def test_scope_tiers_follow_rag_guide():
    assert DEFAULT_SCOPE == {
        "rsw-mvp-situations": "applicable", "rsw-manual-rag-guide": "applicable",
        "festo-cpx-sb-lt-weld-sa": "applicable", "milco-modular-weld-guns": "analogous",
        "yaskawa-yrc1000-air-gun": "analogous", "abb-robotware6-spot-options": "analogous",
        "fanuc-r30ib-plus-servo-gun": "comparison"}
    assert {s.drive_type for s in SOURCES.values() if s.manual_id} == {
        "servo_pneumatic", "pneumatic", "control_software", "servo_electric"}


def test_source_notes_record_reference_corrections():
    text = " ".join(K.SOURCE_NOTES)
    assert "p.225~226" in text and "SVGN-261" in text and "PDF p.310" in text
    fanuc = {r.object for r in _relations("documented_in") if r.subject in ("scenario:S03", "scenario:S06")}
    assert "sec:N-SVGN-261" in fanuc


# ---------------------------------------------------------------- 원본 문서가 있어야 하는 검사


def test_sources_match_registered_hashes(pages):
    assert set(pages) == set(SOURCES)
    assert len(pages["festo-cpx-sb-lt-weld-sa"]) == 101
    assert len(pages["fanuc-r30ib-plus-servo-gun"]) == 450
    assert len(pages["rsw-mvp-situations"]) == 21


def test_all_citation_anchors_resolve_to_source_text(pages, resolved):
    assert len(resolved) == len(SECTIONS)
    for section in resolved.values():
        texts = {p.number: p.text for p in pages[section.definition.doc]}
        for span in section.spans:
            assert texts[span.page][span.char_start:span.char_end] == span.excerpt, (section.sid, span.name)


def test_situation_blocks_are_the_definition_document_text(resolved):
    s04 = resolved["D-S04"]
    assert s04.span("definition").excerpt == "- 정의: 잠긴 실린더가 멈추지 않고 분당 5 mm 넘게 움직임."
    assert s04.span("step1").excerpt == "1) 잠금 기능 진단 실행"
    assert resolved["F-10.1-12"].span("row").excerpt.startswith("12 Timeout equalizer")
    assert "Check supply pressure at least\n5 bar." in resolved["F-10.1-12"].span("row").excerpt
    assert resolved["M-9-1"].span("inlet").excerpt.endswith("(18-30 °C).")   # Symbol 글꼴 문자 정규화


def test_chunk_plan(resolved):
    plans = build.plan_chunks(resolved)
    by_section: dict[str, list[build.ChunkPlan]] = {}
    for plan in plans:
        by_section.setdefault(plan.sid, []).append(plan)
    assert "D-QTYPES" not in by_section                     # 메타 절은 검색 색인에서 제외
    documented = {r.object[4:] for r in _relations("documented_in")}
    assert documented <= set(by_section)                     # 근거 절은 모두 검색 가능
    for sid in (f"D-S{i:02d}" for i in range(1, 11)):
        assert len(by_section[sid]) == 2                     # 고장·신호 / 원인·점검
        assert "작업자 질문" not in by_section[sid][0].content
        scenario = next(s for s in K.SCENARIOS if s.entity.code == sid[2:])
        assert scenario.question_examples[0] in by_section[sid][0].search_terms
    assert all(len(p.content) <= build.CHUNK_MAX_CHARS or len(p.content.split("\n")) == 1 for p in plans)
