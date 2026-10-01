"""배포 전 도메인 검사 (SCHEMA.md 5장 '배포 검사 최소 항목' 중 SQL로 판정 가능한 부분).

SHACL(shapes.ttl)이 RDF 구조(하위 타입 행, 관계 끝 종류, 근거 존재)를 검사하고, 여기서는 DB 기준의
검토 상태·장비 구성 검증·절차 단계 적용성을 검사한다. failure는 배포를 막고 warning은 보고만 한다.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sqlutil import fetch_all

CHECKS: list[tuple[str, str, str, str]] = [
    (
        "no_draft_content", "failure",
        "런타임에 노출되는 개념·관계·적용성은 검토 완료(reviewed) 또는 반려(rejected) 상태여야 한다.",
        """
        SELECT 'entity_versions' AS source, entity_id::text AS id FROM rsw.entity_versions
         WHERE release_id = :rid AND review_status = 'draft'
        UNION ALL SELECT 'knowledge_relations', id::text FROM rsw.knowledge_relations
         WHERE release_id = :rid AND review_status = 'draft'
        UNION ALL SELECT 'applicability', id::text FROM rsw.applicability
         WHERE release_id = :rid AND review_status = 'draft'
        """,
    ),
    (
        "applicable_requires_verified_asset_config", "failure",
        "applicable 적용성은 검증된(verified) 장비 구성에 대해서만 배포한다.",
        """
        SELECT 'applicability' AS source, a.id::text AS id FROM rsw.applicability a
        JOIN rsw.asset_configs c ON c.id = a.asset_config_id
        WHERE a.release_id = :rid AND a.review_status = 'reviewed' AND a.reference_scope = 'applicable'
          AND c.verification_status <> 'verified'
        """,
    ),
    (
        "procedure_steps_applicable", "failure",
        "applicable 절차의 모든 단계 근거 절도 같은 장비 구성에서 applicable로 검토되어야 한다.",
        """
        SELECT 'procedure_steps' AS source, s.id::text AS id
        FROM rsw.applicability pa
        JOIN rsw.procedure_steps s ON s.release_id = pa.release_id AND s.procedure_id = pa.content_entity_id
        WHERE pa.release_id = :rid AND pa.content_kind = 'procedure' AND pa.review_status = 'reviewed'
          AND pa.reference_scope = 'applicable'
          AND NOT EXISTS (
              SELECT 1 FROM rsw.applicability sa
              WHERE sa.release_id = pa.release_id AND sa.asset_config_id = pa.asset_config_id
                AND sa.content_entity_id = s.source_section_id AND sa.review_status = 'reviewed'
                AND sa.reference_scope = 'applicable')
        """,
    ),
    (
        "verified_binding_requires_checked_feature", "failure",
        "검증된 feature binding은 출처 확인 이상의 feature 정의를 가리켜야 한다.",
        """
        SELECT 'feature_bindings' AS source, b.id::text AS id FROM rsw.feature_bindings b
        JOIN rsw.feature_definitions f ON f.release_id = b.release_id AND f.entity_id = b.feature_id
        WHERE b.release_id = :rid AND b.verification_status = 'verified' AND f.definition_status = 'unverified'
        """,
    ),
    (
        "unresolved_conflicts", "warning",
        "미해결 충돌은 숨기지 않는다. 런타임은 해당 내용으로 단일 확정값/절차를 만들지 않는다.",
        "SELECT 'knowledge_conflicts' AS source, id::text AS id FROM rsw.knowledge_conflicts WHERE release_id = :rid AND status = 'unresolved'",
    ),
    (
        "documented_section_without_chunk", "warning",
        "documented_in 대상 원문 절에 검색 청크가 없으면 인용 후보로 검색되지 않는다.",
        """
        SELECT DISTINCT 'document_sections' AS source, r.object_id::text AS id FROM rsw.knowledge_relations r
        WHERE r.release_id = :rid AND r.predicate = 'documented_in' AND r.review_status = 'reviewed'
          AND NOT EXISTS (SELECT 1 FROM rsw.release_chunks rc WHERE rc.release_id = r.release_id AND rc.section_id = r.object_id)
        """,
    ),
]


async def run_domain_checks(session: AsyncSession, release_id: UUID) -> list[dict[str, Any]]:
    results = []
    for name, severity, description, sql in CHECKS:
        rows = await fetch_all(session, sql, rid=release_id)
        results.append({
            "check": name, "severity": severity, "description": description,
            "passed": not rows, "violation_count": len(rows),
            "violations": [f"{r['source']}:{r['id']}" for r in rows[:50]],
        })
    return results


def domain_checks_passed(results: list[dict[str, Any]]) -> bool:
    return all(r["passed"] for r in results if r["severity"] == "failure")
