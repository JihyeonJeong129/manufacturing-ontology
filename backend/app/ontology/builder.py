"""PostgreSQL 지식 배포본 → RDF (builder_version = rsw-rdf-builder-1).

- 빌드는 두 단계다: load_snapshot()(DB 읽기) → build_graph()(순수 함수). 빌더는 테스트에서 DB 없이 검증한다.
- 모든 노드에 IRI를 부여하고 blank node를 쓰지 않는다. 정렬된 N-Triples로 직렬화하면
  같은 배포본에서 항상 같은 바이트가 나오므로 graph_sha256을 재현할 수 있다.
- 관계는 rsw:Relation 노드로 재구체화하며 조건(JSON)·장비 범위·경험적/검토 상태를 보존한다.
- 추론 규칙은 실행하지 않는다. 명시된 사실만 표현한다.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import as_json, dumps
from app.db.sqlutil import fetch_all, fetch_one

BUILDER_VERSION = "rsw-rdf-builder-1"
ONTOLOGY_DIR = Path(__file__).parent
ONTOLOGY_FILE = ONTOLOGY_DIR / "rsw.ttl"
SHAPES_FILE = ONTOLOGY_DIR / "shapes.ttl"
ONTOLOGY_VERSION = "rsw-ontology-1.0"
SHAPES_VERSION = "rsw-shapes-1.0"

RSW = Namespace("urn:rsw:ontology#")
JSON_DT = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON")

KIND_CLASS = {
    "component_type": RSW.ComponentType, "scenario": RSW.Scenario, "fault_type": RSW.FaultType,
    "alarm_code": RSW.AlarmCode, "diagnostic_rule": RSW.DiagnosticRule, "hypothesis": RSW.Hypothesis,
    "evidence_pattern": RSW.EvidencePattern, "feature": RSW.Feature, "document_section": RSW.DocumentSection,
    "procedure": RSW.Procedure, "numeric_spec": RSW.NumericSpec,
}
PREDICATE_IRI = {
    "related_fault": RSW.relatedFault, "raises_hypothesis": RSW.raisesHypothesis, "has_pattern": RSW.hasPattern,
    "suggests": RSW.suggests, "may_contribute_to": RSW.mayContributeTo, "maps_to": RSW.mapsTo,
    "triggers": RSW.triggers, "has_procedure": RSW.hasProcedure, "checks_hypothesis": RSW.checksHypothesis,
    "uses_spec": RSW.usesSpec, "concerns_component": RSW.concernsComponent, "documented_in": RSW.documentedIn,
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ontology_sha256() -> str:
    return file_sha256(ONTOLOGY_FILE)


def shapes_sha256() -> str:
    return file_sha256(SHAPES_FILE)


def iri(kind: str, value: Any) -> URIRef:
    return URIRef(f"urn:rsw:{kind}:{value}")


@dataclass
class ReleaseSnapshot:
    release: dict[str, Any]
    entities: list[dict[str, Any]] = field(default_factory=list)
    aliases: list[dict[str, Any]] = field(default_factory=list)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    patterns: list[dict[str, Any]] = field(default_factory=list)
    procedures: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    numeric_specs: list[dict[str, Any]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    applicability: list[dict[str, Any]] = field(default_factory=list)
    applicability_evidence: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    asset_configs: list[dict[str, Any]] = field(default_factory=list)


async def load_snapshot(session: AsyncSession, release_id: UUID) -> ReleaseSnapshot:
    release = await fetch_one(session, "SELECT * FROM rsw.knowledge_releases WHERE id = :id", id=release_id)
    if release is None:
        raise LookupError(f"release {release_id} not found")
    q = {"rid": release_id}
    snap = ReleaseSnapshot(release=release)
    snap.entities = await fetch_all(session, """
        SELECT ev.entity_id, ev.entity_kind, ev.label_ko, ev.label_en, ev.description, ev.metadata, ev.review_status,
               ke.namespace, ke.local_code
        FROM rsw.entity_versions ev JOIN rsw.knowledge_entities ke ON ke.id = ev.entity_id
        WHERE ev.release_id = :rid ORDER BY ev.entity_id""", **q)
    snap.aliases = await fetch_all(session, "SELECT entity_id, language, alias FROM rsw.entity_aliases WHERE release_id = :rid ORDER BY 1,2,3", **q)
    snap.scenarios = await fetch_all(session, "SELECT * FROM rsw.scenarios WHERE release_id = :rid ORDER BY entity_id", **q)
    snap.features = await fetch_all(session, "SELECT * FROM rsw.feature_definitions WHERE release_id = :rid ORDER BY entity_id", **q)
    snap.patterns = await fetch_all(session, "SELECT * FROM rsw.evidence_patterns WHERE release_id = :rid ORDER BY entity_id", **q)
    snap.procedures = await fetch_all(session, "SELECT * FROM rsw.procedures WHERE release_id = :rid ORDER BY entity_id", **q)
    snap.steps = await fetch_all(session, "SELECT * FROM rsw.procedure_steps WHERE release_id = :rid ORDER BY procedure_id, step_number", **q)
    snap.numeric_specs = await fetch_all(session, "SELECT * FROM rsw.numeric_specs WHERE release_id = :rid ORDER BY entity_id", **q)
    snap.sections = await fetch_all(session, """
        SELECT ds.id, ds.document_revision_id, ds.section_key, ds.section_number, ds.title, ds.text_sha256
        FROM rsw.document_sections ds JOIN rsw.entity_versions ev ON ev.entity_id = ds.id AND ev.release_id = :rid
        ORDER BY ds.id""", **q)
    revision_ids = sorted({s["document_revision_id"] for s in snap.sections}, key=str)
    if revision_ids:
        snap.revisions = await fetch_all(session, """
            SELECT id, document_key, revision_key, source_kind, title, source_sha256, sealed_at
            FROM rsw.document_revisions WHERE id = ANY(:ids) ORDER BY id""", ids=revision_ids)
    snap.relations = await fetch_all(session, "SELECT * FROM rsw.knowledge_relations WHERE release_id = :rid ORDER BY id", **q)
    snap.evidence = await fetch_all(session, "SELECT * FROM rsw.relation_evidence WHERE release_id = :rid ORDER BY id", **q)
    snap.applicability = await fetch_all(session, "SELECT * FROM rsw.applicability WHERE release_id = :rid ORDER BY id", **q)
    snap.applicability_evidence = await fetch_all(session, "SELECT * FROM rsw.applicability_evidence WHERE release_id = :rid ORDER BY 2,3", **q)
    snap.conflicts = await fetch_all(session, "SELECT * FROM rsw.knowledge_conflicts WHERE release_id = :rid ORDER BY id", **q)
    config_ids = sorted({a["asset_config_id"] for a in snap.applicability}
                        | {r["scope_asset_config_id"] for r in snap.relations if r["scope_asset_config_id"]}, key=str)
    if config_ids:
        snap.asset_configs = await fetch_all(session, "SELECT id, asset_id, revision, verification_status FROM rsw.asset_configs WHERE id = ANY(:ids) ORDER BY id", ids=config_ids)
    return snap


def _lit(value: Any) -> Literal:
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value, datatype=XSD.integer)
    # RDF 1.1의 단순 문자열(plain literal). sh:in / SPARQL 패턴의 문자열 값과 같은 term이 된다.
    return Literal(str(value))


def _json(value: Any) -> Literal:
    return Literal(dumps(as_json(value) if isinstance(value, (str, bytes)) else value), datatype=JSON_DT)


def build_graph(snap: ReleaseSnapshot) -> Graph:
    g = Graph()
    g.bind("rsw", RSW)
    rel = snap.release
    release = iri("release", rel["id"])
    g.add((release, RDF.type, RSW.KnowledgeRelease))
    for prop, key in (("version", "version"), ("ontologyVersion", "ontology_version"), ("shapesVersion", "shapes_version"),
                      ("builderVersion", "builder_version"), ("validationContractVersion", "validation_contract_version"),
                      ("releaseStatus", "status")):
        g.add((release, RSW[prop], _lit(rel[key])))

    for e in snap.entities:
        node = iri("entity", e["entity_id"])
        g.add((node, RDF.type, RSW.KnowledgeEntity))
        g.add((node, RDF.type, KIND_CLASS[e["entity_kind"]]))
        g.add((node, RSW.inRelease, release))
        g.add((node, RSW.kind, _lit(e["entity_kind"])))
        g.add((node, RSW.namespace, _lit(e["namespace"])))
        g.add((node, RSW.localCode, _lit(e["local_code"])))
        g.add((node, RDFS.label, Literal(e["label_ko"], lang="ko")))
        if e["label_en"]:
            g.add((node, RDFS.label, Literal(e["label_en"], lang="en")))
        g.add((node, RSW.description, _lit(e["description"])))
        g.add((node, RSW.reviewStatus, _lit(e["review_status"])))
    for a in snap.aliases:
        g.add((iri("entity", a["entity_id"]), RSW.alias, Literal(a["alias"], lang=a["language"] or None)))

    for s in snap.scenarios:
        node = iri("entity", s["entity_id"])
        g.add((node, RSW.scenarioKind, _lit(s["scenario_kind"])))
        g.add((node, RSW.questionExamples, _json(s["question_examples"])))
        g.add((node, RSW.requiredContext, _json(s["required_context"])))
    for f in snap.features:
        node = iri("entity", f["entity_id"])
        g.add((node, RSW.featureKind, _lit(f["feature_kind"])))
        g.add((node, RSW.valueType, _lit(f["value_type"])))
        g.add((node, RSW.unitStatus, _lit(f["unit_status"])))
        g.add((node, RSW.definitionStatus, _lit(f["definition_status"])))
        if f["unit"]:
            g.add((node, RSW.unit, _lit(f["unit"])))
    for p in snap.patterns:
        node = iri("entity", p["entity_id"])
        g.add((node, RSW.patternKind, _lit(p["pattern_kind"])))
        g.add((node, RSW.conditions, _json(p["conditions"])))
        g.add((node, RSW.empiricalStatus, _lit(p["empirical_status"])))
    for p in snap.procedures:
        node = iri("entity", p["entity_id"])
        g.add((node, RSW.procedureKind, _lit(p["procedure_kind"])))
        g.add((node, RSW.preconditions, _json(p["preconditions"])))
        g.add((node, RSW.requiredRole, _lit(p["required_role"])))
        g.add((node, RSW.sourceSection, iri("entity", p["source_section_id"])))
    for st in snap.steps:
        node = iri("procedure-step", st["id"])
        g.add((iri("entity", st["procedure_id"]), RSW.hasStep, node))
        g.add((node, RDF.type, RSW.ProcedureStep))
        g.add((node, RSW.stepNumber, _lit(int(st["step_number"]))))
        g.add((node, RSW.instruction, _lit(st["instruction_ko"])))
        g.add((node, RSW.sourceSection, iri("entity", st["source_section_id"])))
        if st["expected_result"]:
            g.add((node, RSW.expectedResult, _lit(st["expected_result"])))
        if st["branch_condition"] is not None:
            g.add((node, RSW.branchCondition, _json(st["branch_condition"])))
        if st["source_span_id"]:
            g.add((node, RSW.evidenceSpan, _lit(str(st["source_span_id"]))))
    for n in snap.numeric_specs:
        node = iri("entity", n["entity_id"])
        g.add((node, RSW.quantity, _lit(n["quantity"])))
        g.add((node, RSW.comparison, _lit(n["comparison"])))
        g.add((node, RSW.originalValueText, _lit(n["original_value_text"])))
        g.add((node, RSW.specKind, _lit(n["spec_kind"])))
        g.add((node, RSW.interpretationStatus, _lit(n["interpretation_status"])))
        g.add((node, RSW.conditions, _json(n["conditions"])))
        g.add((node, RSW.sourceSection, iri("entity", n["source_section_id"])))
        for key, prop in (("value_low", "valueLow"), ("value_high", "valueHigh")):
            if n[key] is not None:
                g.add((node, RSW[prop], Literal(str(n[key]), datatype=XSD.decimal)))
        if n["unit"]:
            g.add((node, RSW.unit, _lit(n["unit"])))

    for d in snap.revisions:
        node = iri("document-revision", d["id"])
        g.add((node, RDF.type, RSW.DocumentRevision))
        g.add((node, RSW.title, _lit(d["title"])))
        g.add((node, RSW.sourceSha256, _lit(d["source_sha256"])))
        g.add((node, RSW.sealed, _lit(d["sealed_at"] is not None)))
    for s in snap.sections:
        node = iri("entity", s["id"])
        g.add((node, RSW.documentRevision, iri("document-revision", s["document_revision_id"])))
        g.add((node, RSW.title, _lit(s["title"])))
        g.add((node, RSW.sectionKey, _lit(s["section_key"])))
        g.add((node, RSW.textSha256, _lit(s["text_sha256"])))
        if s["section_number"]:
            g.add((node, RSW.sectionNumber, _lit(s["section_number"])))

    for c in snap.asset_configs:
        node = iri("asset-config", c["id"])
        g.add((node, RDF.type, RSW.AssetConfig))
        g.add((node, RSW.verificationStatus, _lit(c["verification_status"])))

    for r in snap.relations:
        node = iri("relation", r["id"])
        g.add((node, RDF.type, RSW.Relation))
        g.add((node, RSW.inRelease, release))
        g.add((node, RSW.subject, iri("entity", r["subject_id"])))
        g.add((node, RSW.predicate, PREDICATE_IRI[r["predicate"]]))
        g.add((node, RSW.object, iri("entity", r["object_id"])))
        g.add((node, RSW.conditions, _json(r["conditions"])))
        g.add((node, RSW.evidenceKind, _lit(r["evidence_kind"])))
        g.add((node, RSW.empiricalStatus, _lit(r["empirical_status"])))
        g.add((node, RSW.reviewStatus, _lit(r["review_status"])))
        if r["scope_asset_config_id"]:
            g.add((node, RSW.scopeAssetConfig, iri("asset-config", r["scope_asset_config_id"])))
    for ev in snap.evidence:
        node = iri("relation-evidence", ev["id"])
        g.add((iri("relation", ev["relation_id"]), RSW.hasEvidence, node))
        g.add((node, RDF.type, RSW.RelationEvidence))
        g.add((node, RSW.evidenceSection, iri("entity", ev["section_id"])))
        g.add((node, RSW.evidenceRole, _lit(ev["evidence_role"])))
        g.add((node, RSW.explanation, _lit(ev["explanation"])))
        if ev["span_id"]:
            g.add((node, RSW.evidenceSpan, _lit(str(ev["span_id"]))))

    for a in snap.applicability:
        node = iri("applicability", a["id"])
        g.add((node, RDF.type, RSW.Applicability))
        g.add((node, RSW.inRelease, release))
        g.add((node, RSW.content, iri("entity", a["content_entity_id"])))
        g.add((node, RSW.assetConfig, iri("asset-config", a["asset_config_id"])))
        g.add((node, RSW.referenceScope, _lit(a["reference_scope"])))
        g.add((node, RSW.conditions, _json(a["conditions"])))
        g.add((node, RSW.reviewStatus, _lit(a["review_status"])))
        if a["component_scope"]:
            g.add((node, RSW.componentScope, _lit(a["component_scope"])))
        if a["review_basis"]:
            g.add((node, RSW.reviewBasis, _lit(a["review_basis"])))
    for ae in snap.applicability_evidence:
        g.add((iri("applicability", ae["applicability_id"]), RSW.evidenceSection, iri("entity", ae["section_id"])))

    for c in snap.conflicts:
        node = iri("conflict", c["id"])
        g.add((node, RDF.type, RSW.KnowledgeConflict))
        g.add((node, RSW.affectedEntity, iri("entity", c["affected_entity_id"])))
        g.add((node, RSW.leftSection, iri("entity", c["left_section_id"])))
        g.add((node, RSW.rightSection, iri("entity", c["right_section_id"])))
        g.add((node, RSW.conflictStatus, _lit(c["status"])))
        g.add((node, RSW.description, _lit(c["description"])))
    return g


def canonical_ntriples(graph: Graph) -> bytes:
    """blank node가 없는 그래프를 정렬된 N-Triples 바이트로 직렬화한다."""
    text = graph.serialize(format="nt", encoding="utf-8")
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    lines = sorted({line for line in text.splitlines() if line.strip()})
    return ("\n".join(lines) + "\n").encode("utf-8")
