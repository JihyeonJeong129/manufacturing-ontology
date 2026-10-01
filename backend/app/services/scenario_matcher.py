"""관련 시나리오 후보 매칭 (matcher_version = rsw-matcher-v1).

해당 context의 지식 배포본에서 검토 완료(reviewed)이고 반박(refuted)되지 않은 관계만 사용한다.
관계가 장비 구성으로 한정되어 있으면 context의 장비 구성과 같을 때만 사용한다.
결과는 원인 확정이나 확률이 아니라 "관련 시나리오 후보"다.

- observed_code:     alarm_code -maps_to-> fault_type <-related_fault- scenario
- ml_classification: ML 분류 target(fault_type) <-related_fault- scenario
- rule_trigger:      ML 규칙 target(diagnostic_rule) -triggers-> scenario
(pattern, operating_context 매칭은 feature 의미 검증이 필요하여 v1에서 수행하지 않는다.)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sqlutil import fetch_all

MAX_ITEMS = 10

_USABLE = """
  {a}.review_status = 'reviewed' AND {a}.empirical_status <> 'refuted'
  AND ({a}.scope_asset_config_id IS NULL OR {a}.scope_asset_config_id = CAST(:acid AS uuid))
"""

METHOD_TEXT = {
    "observed_code": "관측된 경보 코드가 매핑된 고장 분류와 연결된 시나리오",
    "ml_classification": "ML 분류 결과의 고장 분류와 연결된 시나리오",
    "rule_trigger": "규칙 결과가 트리거하는 시나리오",
}


async def match_scenarios(
    session: AsyncSession,
    release_id: UUID,
    asset_config_id: UUID | None,
    code_entity_ids: list[UUID],
    fault_type_ids: list[UUID],
    rule_ids: list[UUID],
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if code_entity_ids:
        hits += await fetch_all(
            session,
            f"""
            SELECT s.subject_id AS scenario_id, 'observed_code' AS method, ARRAY[m.id, s.id] AS relation_ids
            FROM rsw.knowledge_relations m
            JOIN rsw.knowledge_relations s
              ON s.release_id = m.release_id AND s.predicate = 'related_fault' AND s.object_id = m.object_id
            WHERE m.release_id = :rid AND m.predicate = 'maps_to' AND m.subject_id = ANY(:ids)
              AND {_USABLE.format(a='m')} AND {_USABLE.format(a='s')}
            """,
            rid=release_id, ids=code_entity_ids, acid=asset_config_id,
        )
    if fault_type_ids:
        hits += await fetch_all(
            session,
            f"""
            SELECT s.subject_id AS scenario_id, 'ml_classification' AS method, ARRAY[s.id] AS relation_ids
            FROM rsw.knowledge_relations s
            WHERE s.release_id = :rid AND s.predicate = 'related_fault' AND s.object_id = ANY(:ids)
              AND {_USABLE.format(a='s')}
            """,
            rid=release_id, ids=fault_type_ids, acid=asset_config_id,
        )
    if rule_ids:
        hits += await fetch_all(
            session,
            f"""
            SELECT t.object_id AS scenario_id, 'rule_trigger' AS method, ARRAY[t.id] AS relation_ids
            FROM rsw.knowledge_relations t
            WHERE t.release_id = :rid AND t.predicate = 'triggers' AND t.subject_id = ANY(:ids)
              AND {_USABLE.format(a='t')}
            """,
            rid=release_id, ids=rule_ids, acid=asset_config_id,
        )
    if not hits:
        return []

    methods: dict[UUID, list[str]] = defaultdict(list)
    relations: dict[UUID, list[UUID]] = defaultdict(list)
    for hit in hits:
        sid = hit["scenario_id"]
        if hit["method"] not in methods[sid]:
            methods[sid].append(hit["method"])
        for rel in hit["relation_ids"]:
            if rel not in relations[sid]:
                relations[sid].append(rel)

    rows = await fetch_all(
        session,
        """
        SELECT ke.id, ke.namespace, ke.local_code, ev.label_ko
        FROM rsw.knowledge_entities ke
        JOIN rsw.entity_versions ev ON ev.entity_id = ke.id AND ev.release_id = :rid
        JOIN rsw.scenarios sc ON sc.entity_id = ke.id AND sc.release_id = :rid
        WHERE ke.id = ANY(:ids) AND ev.review_status = 'reviewed'
        """,
        rid=release_id, ids=list(methods),
    )
    return rank_matches([
        {
            "scenario_id": row["id"],
            "namespace": row["namespace"],
            "code": row["local_code"],
            "label": row["label_ko"],
            "match_methods": methods[row["id"]],
            "relation_ids": relations[row["id"]],
            "explanation": explain(methods[row["id"]]),
        }
        for row in rows
    ])


def rank_matches(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """매칭 근거 수가 많은 순, 같으면 namespace/code 순. 최대 10개."""
    ordered = sorted(items, key=lambda x: (-len(x["match_methods"]), x["namespace"], x["code"], str(x["scenario_id"])))
    return ordered[:MAX_ITEMS]


def explain(methods: list[str]) -> str:
    parts = [METHOD_TEXT.get(m, m) for m in methods]
    return "; ".join(parts) + "입니다. 원인 확정이나 발생 확률을 뜻하지 않습니다."
