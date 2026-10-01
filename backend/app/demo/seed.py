"""합성 데모 데이터 적재 (python -m app.cli seed-demo).

모든 코드·장비·문서·수치는 형식 확인용 합성 데이터다. 실제 Festo/현장 장비 사실이나 정비 기준이 아니다.
원문 등록 → 봉인 → 지식 draft 작성 → 전체 검토 승인 → 동결 → RDF/SHACL 검사 후 발행 → pilot 활성화
순서를 실제 서비스 계약 그대로 수행한다.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import token_sha256
from app.core.config import Settings
from app.core.jsonutil import dumps
from app.db.sqlutil import execute, fetch_one
from app.demo.pdf import make_text_pdf
from app.ontology import publisher

DEMO_RELEASE = "demo-kb-v1"
DEPLOYMENT_ACTOR = "demo:developer"
DOC_KEY, DOC_REVISION, EXTRACTION = "demo-manual", "r1", "demo-extractor-v1"
DOC_RELATIVE = "documents/demo-manual-r1.pdf"
SECTION_NS = "demo.manual.r1"

PAGES = [
    [
        "SYNTHETIC DEMO MANUAL - NOT A REAL EQUIPMENT MANUAL",
        "2.1 Alarm DEMO-E01",
        "Alarm DEMO-E01 indicates that the weld cycle exceeded the configured duration.",
        "3.2 Electrode tip wear",
        "Worn electrode tips can extend the weld cycle duration.",
    ],
    [
        "4.1 Electrode tip inspection",
        "Step 1: Stop the cell and apply lockout before inspection.",
        "Step 2: Inspect both electrode tips for wear and deformation.",
        "Step 3: In the non-welding state only, measure the actuator return time.",
        "5.1 Note on a similar gun family",
        "A similar servo-pneumatic gun family uses a different stroke sensor.",
    ],
]
# (key, number, title, page, span lines, search terms, label)
SECTIONS = [
    ("sec-2.1", "2.1", "Alarm DEMO-E01", 1, [PAGES[0][2]], "DEMO-E01 경보 알람 사이클 시간 초과 의미"),
    ("sec-3.2", "3.2", "Electrode tip wear", 1, [PAGES[0][4]], "전극 팁 마모 원인 사이클 지연"),
    ("sec-4.1", "4.1", "Electrode tip inspection", 2, PAGES[1][1:4], "전극 팁 점검 절차 잠금 액추에이터 복귀 시간"),
    ("sec-5.1", "5.1", "Note on a similar gun family", 2, [PAGES[1][5]], "유사 건 스트로크 센서 참고"),
]


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


@dataclass
class DemoIds:
    release_id: UUID
    developer: UUID
    reviewer: UUID
    operator: UUID
    service: UUID
    producer: UUID
    asset_config: UUID
    document_revision: UUID
    entities: dict[str, UUID]


async def _actor(session: AsyncSession, key: str, name: str, role: str) -> UUID:
    await execute(session, """
        INSERT INTO rsw.actors (external_key, display_name, role) VALUES (:k, :n, :r)
        ON CONFLICT (external_key) DO NOTHING""", k=key, n=name, r=role)
    row = await fetch_one(session, "SELECT id FROM rsw.actors WHERE external_key = :k", k=key)
    return row["id"]


async def _credential(session: AsyncSession, actor: UUID, token: str, label: str, producer: UUID | None = None) -> None:
    await execute(session, """
        INSERT INTO rsw.api_credentials (actor_id, token_sha256, producer_id, label) VALUES (:a, :t, :p, :l)
        ON CONFLICT (token_sha256) DO NOTHING""", a=actor, t=token_sha256(token), p=producer, l=label)


async def _entity(session: AsyncSession, kind: str, ns: str, code: str) -> UUID:
    await execute(session, """
        INSERT INTO rsw.knowledge_entities (kind, namespace, local_code) VALUES (:k, :ns, :c)
        ON CONFLICT (kind, namespace, local_code) DO NOTHING""", k=kind, ns=ns, c=code)
    row = await fetch_one(session, "SELECT id FROM rsw.knowledge_entities WHERE kind = :k AND namespace = :ns AND local_code = :c",
                          k=kind, ns=ns, c=code)
    return row["id"]


def write_demo_pdf(storage_root: Path) -> tuple[str, str]:
    data = make_text_pdf(PAGES)
    target = storage_root / DOC_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)
    return f"storage:{DOC_RELATIVE}", sha(data)


async def seed_demo(session: AsyncSession, cfg: Settings, *, publish_release: bool = True) -> dict[str, Any]:
    """한 트랜잭션 안에서 호출한다. 이미 적재되어 있으면 토큰·파일·활성 포인터만 보정한다."""
    developer = await _actor(session, "demo:developer", "데모 개발자", "developer")
    reviewer = await _actor(session, "demo:reviewer", "데모 도메인 검토자", "domain_reviewer")
    operator = await _actor(session, "demo:operator", "데모 작업자", "operator")
    service = await _actor(session, "demo:ml-service", "데모 ML 서비스", "service")
    await execute(session, """
        INSERT INTO rsw.event_producers (producer_key, description) VALUES ('demo-ml', '합성 데모 ML 생산자')
        ON CONFLICT (producer_key) DO NOTHING""")
    producer = (await fetch_one(session, "SELECT id FROM rsw.event_producers WHERE producer_key = 'demo-ml'"))["id"]
    await _credential(session, service, cfg.DEMO_ML_TOKEN, "demo ML producer token", producer)
    await _credential(session, operator, cfg.DEMO_OPERATOR_TOKEN, "demo operator token")
    await _credential(session, reviewer, cfg.DEMO_REVIEWER_TOKEN, "demo reviewer token")
    source_uri, source_sha = write_demo_pdf(cfg.STORAGE_ROOT)

    existing = await fetch_one(session, "SELECT id, status FROM rsw.knowledge_releases WHERE version = :v", v=DEMO_RELEASE)
    if existing is not None:
        if existing["status"] == "published":
            await publisher.activate(session, existing["id"], cfg.KNOWLEDGE_DEPLOYMENT, developer)
        return {"created": False, "release_id": str(existing["id"]), "release_status": existing["status"]}

    # ----- 장비와 구성 (합성) -----
    for code, name in (("RSW-DEMO-001", "합성 데모 용접기 1"), ("RSW-DEMO-002", "합성 데모 용접기 2")):
        await execute(session, "INSERT INTO rsw.assets (asset_code, display_name, location) VALUES (:c, :n, 'DEMO-LINE') ON CONFLICT (asset_code) DO NOTHING",
                      c=code, n=name)
    asset1 = (await fetch_one(session, "SELECT id FROM rsw.assets WHERE asset_code = 'RSW-DEMO-001'"))["id"]
    asset2 = (await fetch_one(session, "SELECT id FROM rsw.assets WHERE asset_code = 'RSW-DEMO-002'"))["id"]
    effective = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await execute(session, """
        INSERT INTO rsw.asset_configs
          (asset_id, revision, manufacturer, model, controller_manufacturer, controller_model, drive_type,
           components, verification_status, verified_by, verified_at, verification_basis, effective_from)
        VALUES (:a, 1, 'DEMO-MFR', 'DEMO-RSW', 'DEMO-CTRL-MFR', 'DEMO-CTRL', 'servo_pneumatic',
                '{"gun": "DEMO-GUN"}'::jsonb, 'verified', :r, now(), '합성 데모 데이터 (실제 장비 확인 아님)', :eff)
        ON CONFLICT (asset_id, revision) DO NOTHING""", a=asset1, r=reviewer, eff=effective)
    await execute(session, """
        INSERT INTO rsw.asset_configs (asset_id, revision, drive_type, effective_from)
        VALUES (:a, 1, 'unknown', :eff) ON CONFLICT (asset_id, revision) DO NOTHING""", a=asset2, eff=effective)
    config1 = (await fetch_one(session, "SELECT id FROM rsw.asset_configs WHERE asset_id = :a AND revision = 1", a=asset1))["id"]

    # ----- 원문 판본: 등록 → 페이지/절/span → 봉인 -----
    doc = await fetch_one(session, """
        INSERT INTO rsw.document_revisions
          (document_key, revision_key, source_kind, title, publisher, language, source_uri, source_sha256,
           extraction_version, page_count, created_by)
        VALUES (:dk, :rk, 'manual', 'Synthetic RSW demo manual (not a real manual)', 'DEMO', 'en', :uri, :sha, :ev, :pc, :by)
        RETURNING id""", dk=DOC_KEY, rk=DOC_REVISION, uri=source_uri, sha=source_sha, ev=EXTRACTION,
        pc=len(PAGES), by=developer)
    doc_id = doc["id"]
    page_texts = ["\n".join(lines) for lines in PAGES]
    for number, text in enumerate(page_texts, start=1):
        await execute(session, """
            INSERT INTO rsw.document_pages (document_revision_id, page_number, locator_kind, printed_page_label, page_text, text_sha256, extraction_method)
            VALUES (:d, :n, 'pdf_page', :label, :t, :s, 'native')""", d=doc_id, n=number, label=str(number), t=text, s=sha(text))
    ids: dict[str, UUID] = {}
    spans: dict[str, list[UUID]] = {}
    for key, number, title, page, lines, _terms in SECTIONS:
        sid = await _entity(session, "document_section", SECTION_NS, key)
        ids[key] = sid
        original = "\n".join(lines)
        await execute(session, """
            INSERT INTO rsw.document_sections (id, document_revision_id, section_key, section_number, title, original_text, text_sha256)
            VALUES (:id, :d, :k, :n, :t, :o, :s)""", id=sid, d=doc_id, k=key, n=number, t=title, o=original, s=sha(original))
        spans[key] = []
        for ordinal, line in enumerate(lines, start=1):
            start = page_texts[page - 1].index(line)
            span = await fetch_one(session, """
                INSERT INTO rsw.section_spans (section_id, document_revision_id, page_number, ordinal, excerpt, char_start, char_end)
                VALUES (:s, :d, :p, :o, :e, :cs, :ce) RETURNING id""",
                s=sid, d=doc_id, p=page, o=ordinal, e=line, cs=start, ce=start + len(line))
            spans[key].append(span["id"])
    await execute(session, "UPDATE rsw.document_revisions SET sealed_at = now() WHERE id = :id", id=doc_id)

    chunks: dict[str, UUID] = {}
    for key, _number, title, page, lines, terms in SECTIONS:
        content = "\n".join(lines)
        chunk = await fetch_one(session, """
            INSERT INTO rsw.retrieval_chunks (section_id, document_revision_id, chunking_version, ordinal, language, content,
                                              heading_path, search_terms, content_sha256, page_start, page_end)
            VALUES (:s, :d, 'demo-chunk-v1', 1, 'en', :c, :h, :t, :sha, :p, :p) RETURNING id""",
            s=ids[key], d=doc_id, c=content, h=title, t=terms, sha=sha(content), p=page)
        chunks[key] = chunk["id"]

    # ----- 지식 배포본 draft -----
    release_id = await publisher.create_draft(session, version=DEMO_RELEASE, created_by=developer,
                                              validation_contract_version=cfg.VALIDATION_CONTRACT_VERSION)
    ids["E01"] = await _entity(session, "alarm_code", "demo.dataset.raw_alarm", "DEMO-E01")
    ids["F01"] = await _entity(session, "fault_type", "demo.dataset.fault_class", "DEMO-F01")
    ids["S01"] = await _entity(session, "scenario", "demo.scenario", "S01")
    ids["H01"] = await _entity(session, "hypothesis", "demo.hypothesis", "H01")
    ids["P01"] = await _entity(session, "procedure", "demo.procedure", "P01")
    ids["FEAT"] = await _entity(session, "feature", "demo.feature", "cycle_duration")
    labels = {
        "E01": ("alarm_code", "사이클 시간 초과 경보 (데모)", "합성 데모: 용접 사이클이 설정 시간을 초과했음을 나타내는 예시 경보 코드."),
        "F01": ("fault_type", "용접 사이클 지연 (데모)", "합성 데모 고장 분류."),
        "S01": ("scenario", "사이클 지연 경보 대응 (데모)", "합성 데모: 사이클 지연 경보가 발생했을 때의 대응 상황."),
        "H01": ("hypothesis", "전극 팁 마모 (데모 원인 후보)", "합성 데모 원인 후보. 확정 원인이 아니다."),
        "P01": ("procedure", "전극 팁 점검 절차 (데모)", "합성 데모 점검 절차."),
        "FEAT": ("feature", "사이클 시간 (데모)", "합성 데모 측정값: 한 용접 사이클의 소요 시간."),
    }
    for key, number, title, *_ in SECTIONS:
        labels[key] = ("document_section", f"{number} {title}", f"합성 데모 매뉴얼 절 {number}.")
    for key, (kind, label, description) in labels.items():
        await execute(session, """
            INSERT INTO rsw.entity_versions (release_id, entity_id, entity_kind, label_ko, description, review_status)
            VALUES (:r, :e, :k, :l, :d, 'reviewed')""", r=release_id, e=ids[key], k=kind, l=label, d=description)
    await execute(session, "INSERT INTO rsw.entity_aliases (release_id, entity_id, language, alias) VALUES (:r, :e, 'ko', '사이클 초과 경보')",
                  r=release_id, e=ids["E01"])
    await execute(session, """
        INSERT INTO rsw.scenarios (release_id, entity_id, scenario_kind, question_examples, required_context)
        VALUES (:r, :e, 'fault_response', CAST(:q AS jsonb), CAST(:rc AS jsonb))""",
        r=release_id, e=ids["S01"], q=dumps(["이 경보가 의미하는 것과 먼저 확인할 정보를 알려주세요."]),
        rc=dumps(["operating_context.cycle_state"]))
    await execute(session, """
        INSERT INTO rsw.feature_definitions (release_id, entity_id, feature_kind, value_type, unit, unit_status, definition_status)
        VALUES (:r, :e, 'measurement', 'number', 's', 'verified', 'source_checked')""", r=release_id, e=ids["FEAT"])
    await execute(session, """
        INSERT INTO rsw.feature_bindings (release_id, asset_config_id, source_tag, feature_id, source_unit, verification_status, verification_basis)
        VALUES (:r, :c, 'demo.cycle_duration', :f, 's', 'verified', '합성 데모 매핑')""", r=release_id, c=config1, f=ids["FEAT"])
    await execute(session, """
        INSERT INTO rsw.procedures (release_id, entity_id, procedure_kind, preconditions, required_role, source_section_id)
        VALUES (:r, :e, 'inspection', CAST(:pre AS jsonb), 'maintenance_technician', :s)""",
        r=release_id, e=ids["P01"], pre=dumps(["설비를 정지하고 잠금 조치를 할 수 있는 권한이 있어야 합니다."]), s=ids["sec-4.1"])
    steps = [
        (1, "셀을 정지하고 점검 전에 잠금(lockout) 조치를 적용합니다.", "설비 정지 및 잠금 표지 부착", None),
        (2, "양쪽 전극 팁의 마모와 변형을 점검합니다.", "마모/변형 여부 기록", None),
        (3, "비용접 상태에서만 액추에이터 복귀 시간을 측정합니다.", "복귀 시간 측정값 기록",
         {"all": [{"field": "operating_context.cycle_state", "op": "eq", "value": "non_welding"}]}),
    ]
    for number, text, expected, branch in steps:
        await execute(session, """
            INSERT INTO rsw.procedure_steps (release_id, procedure_id, step_number, instruction_ko, expected_result,
                                             branch_condition, source_section_id, source_span_id)
            VALUES (:r, :p, :n, :t, :x, CAST(:b AS jsonb), :s, :sp)""",
            r=release_id, p=ids["P01"], n=number, t=text, x=expected, b=None if branch is None else dumps(branch),
            s=ids["sec-4.1"], sp=spans["sec-4.1"][number - 1])

    relations = [
        ("E01", "alarm_code", "maps_to", "F01", "fault_type", "manual", "source_statement", "sec-2.1"),
        ("S01", "scenario", "related_fault", "F01", "fault_type", "team_analysis", "source_statement", "sec-2.1"),
        ("S01", "scenario", "raises_hypothesis", "H01", "hypothesis", "manual", "hypothesis", "sec-3.2"),
        ("S01", "scenario", "has_procedure", "P01", "procedure", "manual", "source_statement", "sec-4.1"),
        ("P01", "procedure", "checks_hypothesis", "H01", "hypothesis", "manual", "source_statement", "sec-4.1"),
        ("E01", "alarm_code", "documented_in", "sec-2.1", "document_section", "manual", "source_statement", "sec-2.1"),
        ("H01", "hypothesis", "documented_in", "sec-3.2", "document_section", "manual", "source_statement", "sec-3.2"),
        ("P01", "procedure", "documented_in", "sec-4.1", "document_section", "manual", "source_statement", "sec-4.1"),
        ("S01", "scenario", "documented_in", "sec-5.1", "document_section", "manual", "source_statement", "sec-5.1"),
    ]
    for subject, skind, predicate, obj, okind, evidence_kind, empirical, section in relations:
        rel = await fetch_one(session, """
            INSERT INTO rsw.knowledge_relations (release_id, subject_id, subject_kind, predicate, object_id, object_kind,
                                                 evidence_kind, empirical_status, review_status)
            VALUES (:r, :s, :sk, :p, :o, :ok, :ek, :es, 'reviewed') RETURNING id""",
            r=release_id, s=ids[subject], sk=skind, p=predicate, o=ids[obj], ok=okind, ek=evidence_kind, es=empirical)
        await execute(session, """
            INSERT INTO rsw.relation_evidence (release_id, relation_id, section_id, span_id, evidence_role, explanation)
            VALUES (:r, :rel, :s, :sp, 'supports', :x)""",
            r=release_id, rel=rel["id"], s=ids[section], sp=spans[section][0], x=f"합성 데모 근거: {predicate}")

    applicability = [
        ("sec-2.1", "document_section", "applicable", "sec-2.1"),
        ("sec-3.2", "document_section", "applicable", "sec-3.2"),
        ("sec-4.1", "document_section", "applicable", "sec-4.1"),
        ("P01", "procedure", "applicable", "sec-4.1"),
        ("sec-5.1", "document_section", "analogous", "sec-5.1"),
    ]
    for content, kind, scope, evidence in applicability:
        row = await fetch_one(session, """
            INSERT INTO rsw.applicability (release_id, content_entity_id, content_kind, asset_config_id, reference_scope,
                                           conditions, review_status, reviewed_by, review_basis)
            VALUES (:r, :c, :k, :ac, :scope, '{}'::jsonb, 'reviewed', :by, '합성 데모 검토 (실제 장비 검토 아님)')
            RETURNING id""", r=release_id, c=ids[content], k=kind, ac=config1, scope=scope, by=reviewer)
        await execute(session, "INSERT INTO rsw.applicability_evidence (release_id, applicability_id, section_id) VALUES (:r, :a, :s)",
                      r=release_id, a=row["id"], s=ids[evidence])
    for key, *_ in SECTIONS:
        await execute(session, "INSERT INTO rsw.release_chunks (release_id, chunk_id, section_id) VALUES (:r, :c, :s)",
                      r=release_id, c=chunks[key], s=ids[key])

    await publisher.approve(session, release_id, reviewer)
    await publisher.freeze(session, release_id)
    report: dict[str, Any] | None = None
    if publish_release:
        report = await publisher.publish(session, release_id, cfg.STORAGE_ROOT)
        if report["result"] != "published":
            raise RuntimeError(f"demo release failed validation: {report['shacl']['report_text'][:2000]} {report['domain_checks']}")
        await publisher.activate(session, release_id, cfg.KNOWLEDGE_DEPLOYMENT, developer)
    return {
        "created": True, "release_id": str(release_id), "release_status": "published" if publish_release else "frozen",
        "document_revision_id": str(doc_id), "asset_config_id": str(config1),
        "entities": {k: str(v) for k, v in ids.items()}, "report": report,
    }


def sample_alert(event_id: str | None = None, *, cycle_state: str = "unknown", asset_key: str = "RSW-DEMO-001") -> dict[str, Any]:
    """API_SPEC.md의 경보 예시와 같은 형태의 합성 요청."""
    return {
        "schema_version": "1.0",
        "producer_schema_version": "demo-ml-v1",
        "event_id": event_id or f"demo-event-{uuid.uuid4().hex[:12]}",
        "producer_key": "demo-ml",
        "asset_key": asset_key,
        "occurred_at": "2026-09-26T01:00:00Z",
        "observation_window": {"start": "2026-09-26T00:59:50Z", "end": "2026-09-26T01:00:00Z"},
        "observed_codes": [{"namespace": "demo.dataset.raw_alarm", "code": "DEMO-E01", "observed_at": "2026-09-26T01:00:00Z"}],
        "observations": [{"feature_key": "demo.cycle_duration", "value": 1.92, "unit": "s", "quality": "valid",
                          "observed_at": "2026-09-26T01:00:00Z"}],
        "ml_assessments": [{
            "kind": "classification", "model": {"name": "demo-classifier", "version": "demo-v1"},
            "target": {"namespace": "demo.dataset.fault_class", "code": "DEMO-F01"},
            "score": {"kind": "probability", "value": 0.82, "calibration_status": "uncalibrated"},
        }],
        "operating_context": {"cycle_state": cycle_state},
    }
