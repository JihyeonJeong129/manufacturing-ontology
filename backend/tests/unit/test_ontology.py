"""RDF 빌더 + SHACL shapes 단위 테스트 (DB 없이 메모리 스냅샷 사용, 실제 pySHACL 실행)."""
import copy
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from rdflib import Literal, URIRef

from app.ontology import builder
from app.ontology.builder import RSW, ReleaseSnapshot, build_graph, canonical_ntriples
from app.ontology.publisher import validate_graph

U = lambda: uuid.uuid4()  # noqa: E731
H = "a" * 64


def snapshot() -> ReleaseSnapshot:
    rel, doc, cfg = U(), U(), U()
    alarm, fault, scen, proc, sec = U(), U(), U(), U(), U()
    r_map, r_doc, r_proc = U(), U(), U()
    app_id = U()

    def ent(eid, kind, code, label):
        return {"entity_id": eid, "entity_kind": kind, "label_ko": label, "label_en": None, "description": "합성",
                "metadata": {}, "review_status": "reviewed", "namespace": "test", "local_code": code}

    def relation(rid, s, sk, p, o, ok):
        return {"id": rid, "release_id": rel, "subject_id": s, "subject_kind": sk, "predicate": p, "object_id": o,
                "object_kind": ok, "scope_asset_config_id": None, "conditions": "{}", "evidence_kind": "manual",
                "empirical_status": "source_statement", "review_status": "reviewed"}

    def evidence(rid):
        return {"id": U(), "release_id": rel, "relation_id": rid, "section_id": sec, "span_id": None,
                "evidence_role": "supports", "explanation": "합성 근거"}

    return ReleaseSnapshot(
        release={"id": rel, "version": "t1", "ontology_version": "o", "shapes_version": "s", "builder_version": "b",
                 "validation_contract_version": "v", "status": "frozen"},
        entities=[ent(alarm, "alarm_code", "E1", "경보"), ent(fault, "fault_type", "F1", "고장"),
                  ent(scen, "scenario", "S1", "시나리오"), ent(proc, "procedure", "P1", "절차"),
                  ent(sec, "document_section", "sec-1", "절")],
        scenarios=[{"entity_id": scen, "scenario_kind": "fault_response", "question_examples": "[]",
                    "required_context": '["operating_context.cycle_state"]'}],
        procedures=[{"entity_id": proc, "procedure_kind": "inspection", "preconditions": "[]",
                     "required_role": "technician", "source_section_id": sec}],
        steps=[{"id": U(), "procedure_id": proc, "step_number": 1, "instruction_ko": "점검", "expected_result": None,
                "branch_condition": None, "source_section_id": sec, "source_span_id": None}],
        sections=[{"id": sec, "document_revision_id": doc, "section_key": "sec-1", "section_number": "1",
                   "title": "절", "text_sha256": H}],
        revisions=[{"id": doc, "document_key": "d", "revision_key": "r1", "source_kind": "manual", "title": "문서",
                    "source_sha256": H, "sealed_at": datetime.now(timezone.utc)}],
        relations=[relation(r_map, alarm, "alarm_code", "maps_to", fault, "fault_type"),
                   relation(r_doc, alarm, "alarm_code", "documented_in", sec, "document_section"),
                   relation(r_proc, scen, "scenario", "has_procedure", proc, "procedure")],
        evidence=[evidence(r_map), evidence(r_doc), evidence(r_proc)],
        applicability=[{"id": app_id, "content_entity_id": proc, "asset_config_id": cfg, "reference_scope": "applicable",
                        "conditions": "{}", "review_status": "reviewed", "component_scope": None, "review_basis": "합성"}],
        applicability_evidence=[{"release_id": rel, "applicability_id": app_id, "section_id": sec}],
        asset_configs=[{"id": cfg, "asset_id": U(), "revision": 1, "verification_status": "verified"}],
    )


def test_valid_snapshot_conforms():
    conforms, text, violations = validate_graph(build_graph(snapshot()))
    assert conforms, text
    assert violations == 0


def test_relations_are_reified_not_direct_triples():
    snap = snapshot()
    graph = build_graph(snap)
    for predicate in builder.PREDICATE_IRI.values():
        assert not list(graph.triples((None, predicate, None))), "subject-predicate-object 직접 삼중항을 만들지 않는다"
    relation_nodes = list(graph.subjects(RSW.predicate, RSW.mapsTo))
    assert len(relation_nodes) == 1
    node = relation_nodes[0]
    assert graph.value(node, RSW.conditions) is not None
    assert str(graph.value(node, RSW.reviewStatus)) == "reviewed"
    assert URIRef("http://www.w3.org/2002/07/owl#sameAs") not in set(graph.predicates())


def test_serialization_is_deterministic():
    snap = snapshot()
    first, second = canonical_ntriples(build_graph(snap)), canonical_ntriples(build_graph(copy.deepcopy(snap)))
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert b"_:" not in first, "blank node가 있으면 해시가 재현되지 않는다"


def _violates(mutate, expected_fragment: str) -> None:
    snap = snapshot()
    mutate(snap)
    conforms, text, _ = validate_graph(build_graph(snap))
    assert not conforms
    assert expected_fragment in text, text


def test_reviewed_relation_without_supporting_evidence():
    _violates(lambda s: s.evidence.pop(0), "지지(supports) 근거")


def test_relation_endpoint_kinds_follow_relation_rules():
    def wrong(s):
        s.relations[0]["predicate"] = "has_procedure"  # alarm_code -has_procedure-> fault_type 는 허용되지 않는다
    _violates(wrong, "relation_rules")


def test_missing_subtype_row():
    _violates(lambda s: s.scenarios.clear(), "scenarios 하위 행")


def test_procedure_requires_steps():
    _violates(lambda s: s.steps.clear(), "최소 1개의 단계")


def test_applicable_requires_evidence():
    _violates(lambda s: s.applicability_evidence.clear(), "applicability_evidence")


def test_unsealed_source_revision():
    def unseal(s):
        s.revisions[0]["sealed_at"] = None
    _violates(unseal, "봉인")


def test_only_frozen_releases_are_built():
    def draft(s):
        s.release["status"] = "draft"
    _violates(draft, "frozen")


def test_korean_label_required():
    def english_only(s):
        s.entities[0]["label_ko"] = "x"
    snap = snapshot()
    english_only(snap)
    graph = build_graph(snap)
    graph.remove((None, None, Literal("x", lang="ko")))
    conforms, text, _ = validate_graph(graph)
    assert not conforms and "한국어 표시 이름" in text


def test_ontology_and_shape_files_are_hashed():
    assert len(builder.ontology_sha256()) == 64 and len(builder.shapes_sha256()) == 64
    assert builder.ontology_sha256() != builder.shapes_sha256()
