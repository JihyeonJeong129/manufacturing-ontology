"""답변 실행 1건을 준비한다. DB 쓰기는 하지 않고 최종 트랜잭션에 넣을 행들을 돌려준다.

기본 생성기는 결정적 템플릿(builtin/grounded-template-v1)이다. 검색된 원문 span, 이벤트 행,
검토된 절차만 사용해 주장을 만들고, 모든 인용문은 서버가 span에서 그대로 추출한다.
LLM 생성기를 붙일 때도 같은 근거 묶음(EvidenceBundle)과 check_answer() 계약을 사용한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.jsonutil import as_json
from app.db.sqlutil import fetch_all, fetch_one
from app.schemas.common import notice
from app.services import conditions
from app.services.answering import applicability as appl
from app.services.answering import fusion
from app.services.answering.embedding import resolve_embedder
from app.services.answering.invariants import check_answer
from app.services.conditions import FIELD_LABELS, UNKNOWN_VALUES, VALUE_LABELS
from app.services.followups import CATALOG, required_context_fields

SNAPSHOT_VERSION = 1
MAX_EVENT_CLAIMS = 10
QUESTION_SCENARIO_WINDOW = 3   # 질문 기반 시나리오 연결에 쓰는 상위 선택 절 수
SCOPE_RANK = {"applicable": 0, "analogous": 1, "comparison": 2, "unknown": 3, "excluded": 4}
DISTANCE_OPERATORS = {"cosine": "<=>", "l2": "<->", "inner_product": "<#>"}

SYSTEM_RULES = (
    "당신은 RSW 용접건 정비 안내 보조입니다. 제공된 근거 원문 발췌와 이벤트 데이터만 사용합니다. "
    "ML 결과나 관련 시나리오를 확정 원인으로 표현하지 않습니다. 적용성이 applicable로 검토되고 조건이 "
    "충족된 절차만 수행 안내로 제시하며, 유사/비교/미확인 자료는 참고 설명으로만 표시합니다. "
    "필요한 정보가 없으면 추가 확인 질문을 하고, 근거가 없으면 근거 부족으로 답합니다. 장비 제어 명령은 생성하지 않습니다."
)


class RunNotAnswerable(Exception):
    """재시도해도 해결되지 않는 입력 문제."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PreparedAnswer:
    status: str
    result: dict[str, Any]
    input_snapshot: dict[str, Any]
    candidates: list[dict[str, Any]]
    citations: list[dict[str, Any]]


@dataclass
class RunContext:
    run: dict[str, Any]
    run_id: UUID
    release_id: UUID
    context_id: UUID
    asset_config_id: UUID | None
    context_status: str
    operating_context: dict[str, Any]
    integration: dict[str, Any]
    warnings: list[dict[str, Any]]
    parameters: dict[str, Any]
    codes: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    ml: list[dict[str, Any]]
    asset_config: dict[str, Any] | None
    query_vector: list[float] | None = None

    @property
    def question(self) -> str:
        return self.run["question"]

    @property
    def event_ids(self) -> set[str]:
        return {str(r["id"]) for r in self.codes + self.observations + self.ml}


@dataclass
class Graph:
    code_entities: list[UUID] = field(default_factory=list)
    faults: list[UUID] = field(default_factory=list)
    hypotheses: list[UUID] = field(default_factory=list)
    scenarios: list[UUID] = field(default_factory=list)
    procedures: list[UUID] = field(default_factory=list)
    code_sections: list[UUID] = field(default_factory=list)
    graph_sections: list[UUID] = field(default_factory=list)
    entity_sections: dict[UUID, list[UUID]] = field(default_factory=dict)
    relations: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    unknown_fields: set[str] = field(default_factory=set)
    labels: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    scenario_evidence: dict[UUID, list[dict[str, str]]] = field(default_factory=dict)
    required_context: dict[UUID, Any] = field(default_factory=dict)
    # 질문 내용(검색된 원문 절)으로 연결한 시나리오: scenario_id -> {section_id, relation_id, rank}
    question_links: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    # 그래프 관계의 근거 span (절 -> span 목록). 인용할 때 이 span을 우선한다.
    evidence_spans: dict[UUID, list[UUID]] = field(default_factory=dict)


# ---------------------------------------------------------------- load


def _order(rows: list[dict[str, Any]], mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {str(m["id"]): m["input_index"] for m in mappings}
    return sorted(rows, key=lambda r: index.get(str(r["id"]), 1 << 30))


async def load_run_context(session: AsyncSession, run_id: UUID) -> RunContext:
    run = await fetch_one(
        session,
        """
        SELECT r.id, r.context_id, r.release_id, r.question, r.parameters, r.embedding_model_id, r.status,
               r.prompt_version, r.retriever_version, r.model_provider, r.model_name, r.requested_by,
               ac.alert_id, ac.asset_config_id, ac.status AS context_status, ac.operating_context,
               ac.warnings, ac.sealed_at
        FROM rsw.assistant_runs r JOIN rsw.alert_contexts ac ON ac.id = r.context_id
        WHERE r.id = :id
        """,
        id=run_id,
    )
    if run is None:
        raise RunNotAnswerable("RUN_MISSING", "답변 실행을 찾을 수 없습니다.")
    operating_context = dict(as_json(run["operating_context"]) or {})
    integration = operating_context.pop("_integration", {}) or {}
    cid = run["context_id"]
    codes = await fetch_all(session, """
        SELECT id, raw_namespace, raw_code, code_entity_id, mapping_status, observed_at
        FROM rsw.observed_codes WHERE context_id = :cid""", cid=cid)
    observations = await fetch_all(session, """
        SELECT id, raw_feature_key, value, summary, unit, quality, binding_id, observed_at
        FROM rsw.observations WHERE context_id = :cid""", cid=cid)
    ml = await fetch_all(session, """
        SELECT id, assessment_kind, model_name, model_version, raw_target_namespace, raw_target_code,
               target_entity_id, target_kind, score_kind, score_value, calibration_status
        FROM rsw.ml_assessments WHERE context_id = :cid""", cid=cid)
    asset_config = None
    if run["asset_config_id"] is not None:
        asset_config = await fetch_one(session, """
            SELECT id, drive_type, manufacturer, model, controller_manufacturer, controller_model,
                   controller_firmware, verification_status
            FROM rsw.asset_configs WHERE id = :id""", id=run["asset_config_id"])
    for row in observations:
        row["value"] = as_json(row["value"])
    return RunContext(
        run=run, run_id=run["id"], release_id=run["release_id"], context_id=cid,
        asset_config_id=run["asset_config_id"], context_status=run["context_status"],
        operating_context=operating_context, integration=integration,
        warnings=as_json(run["warnings"]) or [], parameters=as_json(run["parameters"]) or {},
        codes=_order(codes, integration.get("code_mappings", [])),
        observations=_order(observations, integration.get("observation_mappings", [])),
        ml=_order(ml, integration.get("ml_mappings", [])),
        asset_config=asset_config,
    )


def build_facts(operating_context: dict[str, Any], asset_config: dict[str, Any] | None,
                operator_inputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """조건 평가용 사실. 작업자 입력은 ML 입력의 unknown만 보완하고, 다르면 충돌로 남긴다."""
    facts: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for key in ("cycle_state", "workpiece_type", "shift"):
        name = f"operating_context.{key}"
        facts[name] = operating_context.get(key)
        if facts[name] not in UNKNOWN_VALUES:
            provenance[name] = "ml_event"
    conflicts: list[dict[str, Any]] = []
    for name, entry in sorted(operator_inputs.items()):
        if name not in CATALOG or not isinstance(entry, dict):
            continue
        value = entry.get("value")
        current = facts.get(name)
        if current in UNKNOWN_VALUES:
            facts[name] = value
            provenance[name] = "operator_reported"
        elif not (type(current) is type(value) and current == value):
            conflicts.append({"field": name, "event_value": current, "operator_value": value})
            facts[name] = None
            provenance[name] = "conflict"
    if asset_config:
        for key in ("drive_type", "manufacturer", "model", "controller_manufacturer", "controller_model",
                    "controller_firmware", "verification_status"):
            facts[f"asset_config.{key}"] = asset_config.get(key)
            provenance[f"asset_config.{key}"] = "asset_config"
    return facts, provenance, conflicts


# ---------------------------------------------------------------- graph

_USABLE = """
  r.review_status = 'reviewed' AND r.empirical_status <> 'refuted'
  AND (r.scope_asset_config_id IS NULL OR r.scope_asset_config_id = CAST(:acid AS uuid))
"""


async def _relations(session: AsyncSession, ctx: RunContext, facts: dict[str, Any], graph: Graph,
                     predicate: str, subjects: list[UUID]) -> list[dict[str, Any]]:
    if not subjects:
        return []
    rows = await fetch_all(
        session,
        f"""
        SELECT r.id, r.subject_id, r.subject_kind, r.predicate, r.object_id, r.object_kind, r.conditions
        FROM rsw.knowledge_relations r
        JOIN rsw.entity_versions obj ON obj.release_id = r.release_id AND obj.entity_id = r.object_id
        WHERE r.release_id = :rid AND r.predicate = :predicate AND r.subject_id = ANY(:subjects)
          AND obj.review_status = 'reviewed' AND {_USABLE}
        ORDER BY r.subject_id, r.object_id, r.id
        """,
        rid=ctx.release_id, predicate=predicate, subjects=subjects, acid=ctx.asset_config_id,
    )
    usable = []
    for row in rows:
        evaluation = conditions.evaluate(as_json(row["conditions"]), facts)
        if evaluation.result is False:
            continue
        if evaluation.result is None:
            graph.unknown_fields |= evaluation.unknown_fields
        row["condition_result"] = evaluation.state
        graph.relations[row["id"]] = row
        usable.append(row)
    return usable


def _add(items: list[UUID], value: UUID) -> None:
    if value not in items:
        items.append(value)


async def expand_graph(session: AsyncSession, ctx: RunContext, facts: dict[str, Any],
                       question_links: list[dict[str, Any]] | None = None) -> Graph:
    graph = Graph()
    for code in ctx.codes:
        if code["code_entity_id"]:
            _add(graph.code_entities, code["code_entity_id"])
    for item in ctx.ml:
        if item["target_kind"] == "fault_type":
            _add(graph.faults, item["target_entity_id"])
        elif item["target_kind"] == "hypothesis":
            _add(graph.hypotheses, item["target_entity_id"])
    related = ctx.integration.get("related_scenarios") or {}
    matched_relation_ids: list[UUID] = []
    for item in related.get("items", []) if related.get("status") == "matched" else []:
        sid = UUID(item["scenario_id"])
        _add(graph.scenarios, sid)
        matched_relation_ids += [UUID(r) for r in item.get("relation_ids", [])]
        graph.scenario_evidence[sid] = _scenario_event_evidence(ctx, item.get("match_methods", []))
    for link in question_links or []:
        sid = link["scenario_id"]
        if sid not in graph.scenarios:
            _add(graph.scenarios, sid)
            graph.question_links[sid] = link
            matched_relation_ids.append(link["relation_id"])

    for rel in await _relations(session, ctx, facts, graph, "maps_to", graph.code_entities):
        _add(graph.faults, rel["object_id"])
    for rel in await _relations(session, ctx, facts, graph, "raises_hypothesis", graph.scenarios):
        _add(graph.hypotheses, rel["object_id"])
    for rel in await _relations(session, ctx, facts, graph, "has_procedure", graph.scenarios):
        _add(graph.procedures, rel["object_id"])

    subjects = graph.code_entities + graph.faults + graph.hypotheses + graph.scenarios + graph.procedures
    for rel in await _relations(session, ctx, facts, graph, "documented_in", subjects):
        target = graph.code_sections if rel["subject_id"] in graph.code_entities else graph.graph_sections
        _add(target, rel["object_id"])
        graph.entity_sections.setdefault(rel["subject_id"], [])
        _add(graph.entity_sections[rel["subject_id"]], rel["object_id"])

    relation_ids = list(graph.relations) + [r for r in matched_relation_ids if r not in graph.relations]
    if relation_ids:
        for row in await fetch_all(
            session,
            """
            SELECT e.section_id, e.span_id, r.predicate, r.object_id FROM rsw.relation_evidence e
            JOIN rsw.knowledge_relations r ON r.release_id = e.release_id AND r.id = e.relation_id
            WHERE e.release_id = :rid AND e.relation_id = ANY(:ids) AND e.evidence_role IN ('supports', 'context')
            ORDER BY e.relation_id, e.id
            """,
            rid=ctx.release_id, ids=relation_ids,
        ):
            if row["section_id"] not in graph.code_sections:
                _add(graph.graph_sections, row["section_id"])
            # documented_in의 근거가 다른 문서의 참조 목록이면 그 절의 대표 인용으로 쓰지 않는다.
            if row["span_id"] and (row["predicate"] != "documented_in" or row["section_id"] == row["object_id"]):
                graph.evidence_spans.setdefault(row["section_id"], [])
                _add(graph.evidence_spans[row["section_id"]], row["span_id"])
    if graph.procedures:
        for row in await fetch_all(
            session,
            "SELECT entity_id, source_section_id FROM rsw.procedures WHERE release_id = :rid AND entity_id = ANY(:ids)",
            rid=ctx.release_id, ids=graph.procedures,
        ):
            if row["source_section_id"] not in graph.code_sections:
                _add(graph.graph_sections, row["source_section_id"])
    if graph.scenarios:
        for row in await fetch_all(
            session,
            "SELECT entity_id, required_context FROM rsw.scenarios WHERE release_id = :rid AND entity_id = ANY(:ids)",
            rid=ctx.release_id, ids=graph.scenarios,
        ):
            graph.required_context[row["entity_id"]] = as_json(row["required_context"])

    ids = subjects
    if ids:
        for row in await fetch_all(
            session,
            """
            SELECT ev.entity_id, ev.label_ko, ev.review_status, ke.kind, ke.namespace, ke.local_code
            FROM rsw.entity_versions ev JOIN rsw.knowledge_entities ke ON ke.id = ev.entity_id
            WHERE ev.release_id = :rid AND ev.entity_id = ANY(:ids)
            """,
            rid=ctx.release_id, ids=ids,
        ):
            graph.labels[row["entity_id"]] = row
    return graph


def _scenario_event_evidence(ctx: RunContext, methods: list[str]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    if "observed_code" in methods:
        refs += [{"kind": "observed_code", "id": str(c["id"])} for c in ctx.codes if c["code_entity_id"]]
    if "ml_classification" in methods or "rule_trigger" in methods:
        refs += [{"kind": "ml_assessment", "id": str(m["id"])} for m in ctx.ml if m["target_entity_id"]]
    return refs


# ---------------------------------------------------------------- retrieval


@dataclass
class Retrieval:
    ranked: dict[str, list[tuple[UUID, float | None]]]
    chunk_section: dict[UUID, UUID]
    section_assessment: dict[UUID, dict[str, Any]]
    fused: list[tuple[UUID, float]]
    selected_chunks: list[UUID]
    selected_sections: list[UUID]
    conflicts: list[dict[str, Any]]
    vector_status: str
    keyword_query: str | None


async def _applicability(session: AsyncSession, ctx: RunContext, content_ids: list[UUID],
                         facts: dict[str, Any]) -> dict[UUID, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if content_ids and ctx.asset_config_id is not None:
        rows = await fetch_all(
            session,
            """
            SELECT id, content_entity_id, reference_scope, conditions FROM rsw.applicability
            WHERE release_id = :rid AND asset_config_id = :acid AND content_entity_id = ANY(:ids)
              AND review_status = 'reviewed'
            ORDER BY id
            """,
            rid=ctx.release_id, acid=ctx.asset_config_id, ids=content_ids,
        )
    by_content: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        by_content.setdefault(row["content_entity_id"], []).append(row)
    return {cid: appl.assess(by_content.get(cid, []), facts, ctx.asset_config_id) for cid in content_ids}


async def retrieve(session: AsyncSession, ctx: RunContext, graph: Graph, facts: dict[str, Any], cfg: Settings) -> Retrieval:
    limit = cfg.RETRIEVAL_CANDIDATE_LIMIT
    ranked: dict[str, list[tuple[UUID, float | None]]] = {}
    chunk_section: dict[UUID, UUID] = {}

    graph_sections = graph.code_sections + [s for s in graph.graph_sections if s not in graph.code_sections]
    if graph_sections:
        # 같은 방식 안에서는 적용성 순(applicable → analogous → comparison → unknown)으로 둔다.
        # 매뉴얼 검색 순서 권장(같은 구동 방식 → 공압식 실제 건 → 공정 제어 → 전동 서보 비교)과 같다.
        pre = await _applicability(session, ctx, graph_sections, facts)
        code_part = sorted(graph.code_sections, key=lambda sid: SCOPE_RANK.get(pre[sid]["scope"], 9))
        rest = sorted([sid for sid in graph_sections if sid not in set(graph.code_sections)],
                      key=lambda sid: SCOPE_RANK.get(pre[sid]["scope"], 9))
        graph_sections = code_part + rest
        rows = await fetch_all(
            session,
            """
            SELECT rc.chunk_id, rc.section_id, c.ordinal FROM rsw.release_chunks rc
            JOIN rsw.retrieval_chunks c ON c.id = rc.chunk_id
            WHERE rc.release_id = :rid AND rc.section_id = ANY(:sids)
            """,
            rid=ctx.release_id, sids=graph_sections,
        )
        order = {sid: i for i, sid in enumerate(graph_sections)}
        rows.sort(key=lambda r: (order[r["section_id"]], r["ordinal"], str(r["chunk_id"])))
        for method, sections in (("code", set(graph.code_sections)), ("graph", set(graph_sections) - set(graph.code_sections))):
            items = [(r["chunk_id"], None) for r in rows if r["section_id"] in sections][:limit]
            if items:
                ranked[method] = items
        chunk_section.update({r["chunk_id"]: r["section_id"] for r in rows})

    query = fusion.keyword_query([ctx.question] + [f"{c['raw_code']}" for c in ctx.codes])
    if query:
        rows = await fetch_all(
            session,
            """
            SELECT rc.chunk_id, rc.section_id, ts_rank_cd(c.search_tsv, q.query) AS score
            FROM rsw.release_chunks rc
            JOIN rsw.retrieval_chunks c ON c.id = rc.chunk_id
            CROSS JOIN (SELECT to_tsquery('simple', :q) AS query) q
            WHERE rc.release_id = :rid AND c.search_tsv @@ q.query
            ORDER BY score DESC, rc.chunk_id
            LIMIT :limit
            """,
            rid=ctx.release_id, q=query, limit=limit,
        )
        if rows:
            ranked["keyword"] = [(r["chunk_id"], float(r["score"])) for r in rows]
            chunk_section.update({r["chunk_id"]: r["section_id"] for r in rows})

    vector_status = "not_configured"
    model_id = ctx.run["embedding_model_id"]
    embedder = await resolve_embedder(session, model_id, cfg)
    if model_id is not None and embedder is None:
        vector_status = "embedder_unavailable"
    elif embedder is not None:
        model = await fetch_one(session, "SELECT dimensions, distance_metric FROM rsw.embedding_models WHERE id = :id", id=model_id)
        if ctx.query_vector is None:
            ctx.query_vector = await embedder.embed(ctx.question)
        vector = ctx.query_vector
        if model is None or len(vector) != model["dimensions"]:
            vector_status = "dimension_mismatch"
        else:
            operator = DISTANCE_OPERATORS[model["distance_metric"]]
            literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
            rows = await fetch_all(
                session,
                f"""
                SELECT rc.chunk_id, rc.section_id, (ce.embedding {operator} CAST(:qv AS public.vector)) AS distance
                FROM rsw.release_chunks rc
                JOIN rsw.chunk_embeddings ce ON ce.chunk_id = rc.chunk_id AND ce.embedding_model_id = :mid
                WHERE rc.release_id = :rid
                ORDER BY distance, rc.chunk_id
                LIMIT :limit
                """,
                rid=ctx.release_id, mid=model_id, qv=literal, limit=limit,
            )
            vector_status = "searched"
            if rows:
                ranked["vector"] = [(r["chunk_id"], float(r["distance"])) for r in rows]
                chunk_section.update({r["chunk_id"]: r["section_id"] for r in rows})

    sections = list(dict.fromkeys(chunk_section.values()))
    section_assessment = await _applicability(session, ctx, sections, facts)
    excluded = {cid for cid, sid in chunk_section.items() if section_assessment[sid]["scope"] == "excluded"}
    fused = fusion.fuse({m: [cid for cid, _ in items] for m, items in ranked.items()}, excluded)

    selected_chunks: list[UUID] = []
    selected_sections: list[UUID] = []
    for chunk_id, _ in fused:
        section_id = chunk_section[chunk_id]
        if section_id in selected_sections:
            continue
        selected_chunks.append(chunk_id)
        selected_sections.append(section_id)
        if len(selected_sections) >= cfg.RETRIEVAL_TOP_K:
            break

    conflict_ids = list(dict.fromkeys(sections + graph.procedures + graph.hypotheses + graph.faults + graph.scenarios))
    conflicts = []
    if conflict_ids:
        conflicts = await fetch_all(
            session,
            """
            SELECT id, affected_entity_id, left_section_id, right_section_id, description
            FROM rsw.knowledge_conflicts
            WHERE release_id = :rid AND status = 'unresolved'
              AND (affected_entity_id = ANY(:ids) OR left_section_id = ANY(:ids) OR right_section_id = ANY(:ids))
            ORDER BY id
            """,
            rid=ctx.release_id, ids=conflict_ids,
        )
    return Retrieval(ranked, chunk_section, section_assessment, fused, selected_chunks, selected_sections,
                     conflicts, vector_status, query)


async def link_question_scenario(session: AsyncSession, ctx: RunContext, retrieval: Retrieval
                                 ) -> tuple[dict[str, Any] | None, list[UUID]]:
    """장비 데이터로 매칭된 시나리오가 없을 때 질문으로 시나리오를 연결한다.

    연결 조건(보수적): 키워드 검색의 '강한' 절과 벡터 검색의 상위 3개 절이 같은 '시나리오 설명 절'(한 시나리오만
    documented_in으로 가리키는 절)을 함께 가리켜야 한다. 키워드의 강한 절은 점수가 1위의 절반 이상이고 최저 점수보다
    높은 절이다(동점 순서나 모두 같은 점수에 흔들리지 않게). 한 방식만 있으면 그 방식의 1위가 시나리오 설명 절이어야 한다.
    연결하지 못해도 상위 절이 가리키는 시나리오들은 후보 목록으로 돌려준다(절차는 제시하지 않는다).
    """
    scores: dict[UUID, float] = {}
    for cid, score in retrieval.ranked.get("keyword", []):
        scores.setdefault(retrieval.chunk_section[cid], float(score or 0.0))
    keyword: list[UUID] = []
    if scores and max(scores.values()) > min(scores.values()):
        top, low = max(scores.values()), min(scores.values())
        keyword = [sid for sid, score in scores.items() if score >= top * 0.5 and score > low]
    vector: list[UUID] = []
    for cid, _ in retrieval.ranked.get("vector", []):
        sid = retrieval.chunk_section[cid]
        if sid not in vector:
            vector.append(sid)
        if len(vector) >= QUESTION_SCENARIO_WINDOW:
            break
    keyword_ran, vector_ran = "keyword" in retrieval.ranked, "vector" in retrieval.ranked
    pool = list(dict.fromkeys(keyword[:QUESTION_SCENARIO_WINDOW] + vector))
    if not pool:
        return None, []
    rows = await fetch_all(
        session,
        f"""
        SELECT r.id, r.subject_id, r.object_id
        FROM rsw.knowledge_relations r
        JOIN rsw.entity_versions ev ON ev.release_id = r.release_id AND ev.entity_id = r.subject_id
        JOIN rsw.scenarios sc ON sc.release_id = r.release_id AND sc.entity_id = r.subject_id
        WHERE r.release_id = :rid AND r.predicate = 'documented_in' AND r.subject_kind = 'scenario'
          AND r.object_id = ANY(:sections) AND ev.review_status = 'reviewed' AND {_USABLE}
        ORDER BY r.object_id, r.subject_id, r.id
        """,
        rid=ctx.release_id, sections=pool, acid=ctx.asset_config_id,
    )
    by_section: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        by_section.setdefault(row["object_id"], []).append(row)
    describing = {sid: rels[0] for sid, rels in by_section.items() if len({r["subject_id"] for r in rels}) == 1}
    candidates = list(dict.fromkeys(describing[sid]["subject_id"] for sid in pool if sid in describing))
    if keyword_ran and vector_ran:
        agreed = [sid for sid in retrieval.selected_sections if sid in keyword and sid in vector]
    else:
        agreed = (keyword or vector)[:1]
    for rank, section_id in enumerate(agreed, start=1):
        if section_id in describing:
            relation = describing[section_id]
            return {"scenario_id": relation["subject_id"], "section_id": section_id, "relation_id": relation["id"],
                    "rank": rank, "agreement": ["keyword", "vector"] if keyword_ran and vector_ran else
                    (["keyword"] if keyword else ["vector"])}, candidates
    return None, candidates


def candidate_rows(ctx: RunContext, retrieval: Retrieval) -> list[dict[str, Any]]:
    per_chunk = {cid: retrieval.section_assessment[sid] for cid, sid in retrieval.chunk_section.items()}
    rows: list[dict[str, Any]] = []
    for method, items in retrieval.ranked.items():
        rows += fusion.as_rows(ctx.run_id, ctx.release_id, method, items, per_chunk)
    rows += fusion.as_rows(ctx.run_id, ctx.release_id, "fusion", [(cid, score) for cid, score in retrieval.fused],
                           per_chunk, selected=set(retrieval.selected_chunks))
    return rows


# ---------------------------------------------------------------- procedures


@dataclass
class ProcedurePlan:
    procedure_id: UUID
    title: str
    required_role: str
    preconditions: list[str]
    applicability_ids: list[str]
    steps: list[dict[str, Any]]
    blockers: list[str]
    unknown_fields: set[str]


async def evaluate_procedures(session: AsyncSession, ctx: RunContext, graph: Graph, facts: dict[str, Any],
                              retrieval: Retrieval) -> list[ProcedurePlan]:
    if not graph.procedures:
        return []
    procedures = await fetch_all(
        session,
        """
        SELECT p.entity_id, p.preconditions, p.required_role, p.source_section_id, ev.label_ko
        FROM rsw.procedures p
        JOIN rsw.entity_versions ev ON ev.release_id = p.release_id AND ev.entity_id = p.entity_id
        WHERE p.release_id = :rid AND p.entity_id = ANY(:ids) AND ev.review_status = 'reviewed'
        ORDER BY ev.label_ko, p.entity_id
        """,
        rid=ctx.release_id, ids=graph.procedures,
    )
    if not procedures:
        return []
    proc_ids = [p["entity_id"] for p in procedures]
    steps = await fetch_all(
        session,
        """
        SELECT id, procedure_id, step_number, instruction_ko, expected_result, branch_condition,
               source_section_id, source_span_id
        FROM rsw.procedure_steps WHERE release_id = :rid AND procedure_id = ANY(:ids)
        ORDER BY procedure_id, step_number
        """,
        rid=ctx.release_id, ids=proc_ids,
    )
    step_sections = list(dict.fromkeys(s["source_section_id"] for s in steps))
    proc_assessment = await _applicability(session, ctx, proc_ids, facts)
    section_assessment = await _applicability(session, ctx, step_sections, facts)
    conflict_targets = {c["affected_entity_id"] for c in retrieval.conflicts} | \
        {c["left_section_id"] for c in retrieval.conflicts} | {c["right_section_id"] for c in retrieval.conflicts}
    extra_conflicts = await fetch_all(
        session,
        """
        SELECT affected_entity_id, left_section_id, right_section_id FROM rsw.knowledge_conflicts
        WHERE release_id = :rid AND status = 'unresolved'
          AND (affected_entity_id = ANY(:ids) OR left_section_id = ANY(:ids) OR right_section_id = ANY(:ids))
        """,
        rid=ctx.release_id, ids=proc_ids + step_sections,
    ) if step_sections or proc_ids else []
    for c in extra_conflicts:
        conflict_targets |= {c["affected_entity_id"], c["left_section_id"], c["right_section_id"]}

    config_verified = bool(ctx.asset_config and ctx.asset_config["verification_status"] == "verified")
    plans = []
    for proc in procedures:
        pid = proc["entity_id"]
        blockers: list[str] = []
        unknown: set[str] = set()
        if not config_verified:
            blockers.append("asset_config_unverified")
        assessment = proc_assessment[pid]
        if assessment["scope"] != "applicable":
            blockers.append(f"procedure_{assessment['reason']}")
            unknown |= set(assessment["unknown_fields"])
        if pid in conflict_targets or proc["source_section_id"] in conflict_targets:
            blockers.append("unresolved_conflict")
        included = []
        for step in (s for s in steps if s["procedure_id"] == pid):
            branch = as_json(step["branch_condition"])
            evaluation = conditions.evaluate(branch, facts)
            if evaluation.result is False:
                continue
            if evaluation.result is None:
                blockers.append(f"step_{step['step_number']}_condition_unknown")
                unknown |= evaluation.unknown_fields
                continue
            step_scope = section_assessment[step["source_section_id"]]
            if step_scope["scope"] != "applicable":
                blockers.append(f"step_{step['step_number']}_source_{step_scope['reason']}")
                unknown |= set(step_scope["unknown_fields"])
            if step["source_section_id"] in conflict_targets:
                blockers.append(f"step_{step['step_number']}_unresolved_conflict")
            included.append({
                **step,
                "condition_state": "unconditional" if conditions.is_empty(branch) else "satisfied",
                "condition_summary": conditions.describe(branch),
            })
        if not included:
            blockers.append("no_applicable_steps")
        preconditions = as_json(proc["preconditions"]) or []
        plans.append(ProcedurePlan(
            procedure_id=pid, title=proc["label_ko"], required_role=proc["required_role"],
            preconditions=[p if isinstance(p, str) else str(p) for p in preconditions],
            applicability_ids=assessment["applicability_ids"], steps=included,
            blockers=list(dict.fromkeys(blockers)), unknown_fields=unknown,
        ))
    return plans


# ---------------------------------------------------------------- compose


async def _procedure_specs(session: AsyncSession, ctx: RunContext, procedure_ids: list[UUID]) -> list[dict[str, Any]]:
    if not procedure_ids:
        return []
    return await fetch_all(
        session,
        f"""
        SELECT r.subject_id AS procedure_id, ns.entity_id, ns.original_value_text, ns.unit,
               ns.source_section_id, ns.source_span_id, ev.label_ko
        FROM rsw.knowledge_relations r
        JOIN rsw.numeric_specs ns ON ns.release_id = r.release_id AND ns.entity_id = r.object_id
        JOIN rsw.entity_versions ev ON ev.release_id = r.release_id AND ev.entity_id = r.object_id
        WHERE r.release_id = :rid AND r.predicate = 'uses_spec' AND r.subject_id = ANY(:ids)
          AND ev.review_status = 'reviewed' AND {_USABLE}
        ORDER BY r.subject_id, ev.label_ko, ns.entity_id
        """,
        rid=ctx.release_id, ids=procedure_ids, acid=ctx.asset_config_id,
    )



async def _spans(session: AsyncSession, section_ids: list[UUID], span_ids: list[UUID]
                 ) -> tuple[dict[UUID, list[dict]], dict[UUID, dict]]:
    rows = []
    if section_ids or span_ids:
        rows = await fetch_all(
            session,
            """
            SELECT sp.id, sp.section_id, sp.page_number, sp.ordinal, sp.excerpt, sp.char_start, sp.char_end, sp.bbox,
                   dp.locator_kind, dp.printed_page_label,
                   ds.title AS section_title, ds.section_number, ds.document_revision_id,
                   dr.title AS document_title
            FROM rsw.section_spans sp
            JOIN rsw.document_pages dp ON dp.document_revision_id = sp.document_revision_id AND dp.page_number = sp.page_number
            JOIN rsw.document_sections ds ON ds.id = sp.section_id
            JOIN rsw.document_revisions dr ON dr.id = ds.document_revision_id
            WHERE sp.section_id = ANY(:sids) OR sp.id = ANY(:spids)
            ORDER BY sp.section_id, sp.ordinal
            """,
            sids=section_ids, spids=span_ids,
        )
    by_section: dict[UUID, list[dict]] = {}
    by_id: dict[UUID, dict] = {}
    for row in rows:
        by_id[row["id"]] = row
        by_section.setdefault(row["section_id"], []).append(row)
    return by_section, by_id


def _bigrams(text: str) -> set[str]:
    import re

    grams: set[str] = set()
    for token in re.findall(r"\w+", text.lower()):
        if len(token) >= 2:
            grams |= {token[i:i + 2] for i in range(len(token) - 1)}
    return grams


def best_span(spans: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    """절의 span 중 질문과 글자 bigram이 가장 많이 겹치는 것. 다른 span 안에 포함된 부분 span은 제외하고,
    동점이면 앞선 span(ordinal)을 고른다. 겹침이 없으면 첫 span."""
    if not spans:
        return None
    candidates = [
        s for s in spans
        if not any(o is not s and o["page_number"] == s["page_number"] and o["char_start"] is not None
                   and s["char_start"] is not None and o["char_start"] <= s["char_start"] and s["char_end"] <= o["char_end"]
                   and (o["char_end"] - o["char_start"]) > (s["char_end"] - s["char_start"]) for o in spans)
    ] or spans
    grams = _bigrams(question)
    scored = [(len(grams & _bigrams(s["excerpt"])), -s["ordinal"], s) for s in candidates]
    top = max(scored, key=lambda x: (x[0], x[1]))
    return top[2] if top[0] > 0 else sorted(candidates, key=lambda s: s["ordinal"])[0]


def _bbox(value: Any) -> list[float] | None:
    value = as_json(value)
    if (isinstance(value, list) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in value)):
        return [float(v) for v in value]
    return None


class Composer:
    """근거 묶음에서 AnswerResult와 인용 행을 만든다 (builtin 결정적 템플릿)."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.claims: list[dict[str, Any]] = []
        self.citations: list[dict[str, Any]] = []
        self.citation_rows: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self._notice_keys: set[tuple[str, str | None, str]] = set()

    def notice(self, code: str, message: str, field_name: str | None = None) -> None:
        key = (code, field_name, message)
        if key not in self._notice_keys:
            self._notice_keys.add(key)
            self.notices.append(notice(code, message, field_name))

    def cite(self, claim_key: str, span: dict[str, Any], scope: str) -> str:
        citation_id = uuid.uuid4()
        is_pdf = span["locator_kind"] == "pdf_page"
        self.citations.append({
            "citation_id": str(citation_id), "claim_key": claim_key, "section_id": str(span["section_id"]),
            "section_title": span["section_title"], "section_number": span["section_number"],
            "span_id": str(span["id"]), "document_revision_id": str(span["document_revision_id"]),
            "document_title": span["document_title"],
            "document_url": f"/api/v1/documents/{span['document_revision_id']}" if is_pdf else None,
            "quote": span["excerpt"], "reference_scope": scope,
            "locator": {
                "kind": span["locator_kind"], "page_number": span["page_number"],
                "printed_page_label": span["printed_page_label"], "char_start": span["char_start"],
                "char_end": span["char_end"], "bbox": _bbox(span["bbox"]),
            },
        })
        self.citation_rows.append({
            "id": citation_id, "run_id": self.ctx.run_id, "release_id": self.ctx.release_id,
            "claim_key": claim_key, "section_id": span["section_id"], "span_id": span["id"],
            "cited_text": span["excerpt"],
        })
        return str(citation_id)

    def claim(self, key: str, type_: str, text: str, citation_ids: list[str] | None = None,
              event_evidence: list[dict[str, str]] | None = None) -> None:
        self.claims.append({"claim_key": key, "type": type_, "text": text,
                            "citation_ids": citation_ids or [], "event_evidence": event_evidence or []})


def _label(graph: Graph, entity_id: UUID | None, fallback: str) -> str:
    row = graph.labels.get(entity_id) if entity_id else None
    return row["label_ko"] if row else fallback


def _fmt_value(value: Any, unit: Any) -> str:
    if value is None:
        return "값 없음"
    text = VALUE_LABELS.get(value, value) if isinstance(value, str) else value
    return f"{text} {unit}" if unit else f"{text}"


async def prepare_answer(session: AsyncSession, run_id: UUID, cfg: Settings) -> PreparedAnswer:
    ctx = await load_run_context(session, run_id)
    if ctx.context_status == "invalid":
        raise RunNotAnswerable("CONTEXT_NOT_USABLE", "invalid context로는 답변을 생성하지 않습니다.")
    operator_inputs = ctx.parameters.get("operator_inputs", {}) or {}
    facts, provenance, fact_conflicts = build_facts(ctx.operating_context, ctx.asset_config, operator_inputs)
    graph = await expand_graph(session, ctx, facts)
    retrieval = await retrieve(session, ctx, graph, facts, cfg)
    question_link, question_candidates = None, []
    if not graph.scenarios and cfg.QUESTION_SCENARIO_LINKING:
        question_link, question_candidates = await link_question_scenario(session, ctx, retrieval)
        if question_link is not None:
            graph = await expand_graph(session, ctx, facts, [question_link])
            retrieval = await retrieve(session, ctx, graph, facts, cfg)
    plans = await evaluate_procedures(session, ctx, graph, facts, retrieval)
    ready_plans = [p for p in plans if not p.blockers]

    step_sections = [s["source_section_id"] for p in ready_plans for s in p.steps]
    step_spans = [s["source_span_id"] for p in ready_plans for s in p.steps if s["source_span_id"]]
    hypothesis_sections = [sid for h in graph.hypotheses for sid in graph.entity_sections.get(h, [])]
    link_sections = [link["section_id"] for link in graph.question_links.values()]
    spec_rows = await _procedure_specs(session, ctx, [p.procedure_id for p in ready_plans])
    spec_sections = [r["source_section_id"] for r in spec_rows]
    spec_spans = [r["source_span_id"] for r in spec_rows if r["source_span_id"]]
    spans_by_section, span_by_id = await _spans(
        session, list(dict.fromkeys(retrieval.selected_sections + step_sections + hypothesis_sections + link_sections
                                    + spec_sections)), step_spans + spec_spans)
    extra_assessment = await _applicability(
        session, ctx, [sid for sid in dict.fromkeys(spec_sections + link_sections + hypothesis_sections)
                       if sid not in retrieval.section_assessment], facts)
    assessments = {**extra_assessment, **retrieval.section_assessment}

    def quote(section_id: UUID) -> dict[str, Any] | None:
        spans = spans_by_section.get(section_id, [])
        preferred = set(graph.evidence_spans.get(section_id, []))
        return best_span([s for s in spans if s["id"] in preferred] or spans, ctx.question)

    c = Composer(ctx)
    conflicted_fields = {x["field"] for x in fact_conflicts}

    # 1) 이벤트 관측 사실 (원인 확정이 아님)
    for i, code in enumerate(ctx.codes[:MAX_EVENT_CLAIMS], start=1):
        mapped = code["code_entity_id"] is not None
        text = f"수신 데이터에 코드 {code['raw_namespace']}/{code['raw_code']}이(가) 포함되어 있습니다."
        text += (f" 지식 배포본의 경보 코드 '{_label(graph, code['code_entity_id'], code['raw_code'])}'에 매핑되었습니다."
                 if mapped else " 검토된 코드 정의에는 매핑되지 않았습니다.")
        c.claim(f"observed-code-{i}", "observation", text, event_evidence=[{"kind": "observed_code", "id": str(code["id"])}])
    for i, obs in enumerate(ctx.observations[:MAX_EVENT_CLAIMS], start=1):
        value = obs["value"] if obs["value"] is not None else ("요약값 제공" if obs["summary"] is not None else None)
        text = f"관측값 {obs['raw_feature_key']} = {_fmt_value(value, obs['unit'])} (품질: {obs['quality']})."
        c.claim(f"observation-{i}", "observation", text, event_evidence=[{"kind": "observation", "id": str(obs["id"])}])
    for i, item in enumerate(ctx.ml[:MAX_EVENT_CLAIMS], start=1):
        target = (f"{item['raw_target_namespace']}/{item['raw_target_code']}" if item["raw_target_code"] else "대상 없음")
        text = f"ML 모델 {item['model_name']} {item['model_version']}의 {item['assessment_kind']} 출력: {target}"
        if item["score_value"] is not None:
            text += f", 점수 {item['score_kind']}={item['score_value']:g}"
            if item["calibration_status"] != "calibrated":
                text += " (보정되지 않은 점수이므로 실제 발생 확률이 아닙니다)"
        c.claim(f"ml-{i}", "observation", text + ". 모델 출력은 원인 확정이 아닙니다.",
                event_evidence=[{"kind": "ml_assessment", "id": str(item["id"])}])
    if max(len(ctx.codes), len(ctx.observations), len(ctx.ml)) > MAX_EVENT_CLAIMS:
        c.notice("EVENT_EVIDENCE_TRUNCATED", f"이벤트 항목이 많아 종류별 앞 {MAX_EVENT_CLAIMS}개만 답변에 표시했습니다.")

    # 2) 관련 시나리오 후보와 원인 후보 (확정 아님)
    scenario_labels = []
    for i, sid in enumerate(graph.scenarios, start=1):
        label = _label(graph, sid, str(sid))
        scenario_labels.append(label)
        evidence = graph.scenario_evidence.get(sid, [])
        link = graph.question_links.get(sid)
        if evidence:
            c.claim(f"scenario-{i}", "hypothesis",
                    f"관련 시나리오 후보: '{label}'. 이벤트 데이터와 검토된 관계로 연결된 후보이며 원인 확정이 아닙니다.",
                    event_evidence=evidence)
        elif link is not None:
            span = quote(link["section_id"])
            if span is not None:
                scope = appl.citation_scope(assessments.get(link["section_id"]) or {"scope": "unknown"})
                cid = c.cite(f"scenario-{i}", span, scope)
                c.claim(f"scenario-{i}", "hypothesis",
                        f"질문 내용과 가장 가까운 시나리오 후보: '{label}'. 장비 데이터로 확인된 결과가 아니라 질문과 "
                        "원문 설명의 유사도로 찾은 후보이며 원인 확정이 아닙니다.", citation_ids=[cid])
                c.notice("QUESTION_LINKED_SCENARIO",
                         f"장비 데이터로 매칭된 시나리오가 없어 질문 내용으로 '{label}' 시나리오를 찾았습니다. 현장 상황과 맞는지 확인하세요.")
    for i, hid in enumerate(graph.hypotheses, start=1):
        key = f"hypothesis-{i}"
        cites = []
        for sid in graph.entity_sections.get(hid, []):
            span = quote(sid)
            if span and assessments.get(sid, {"scope": "unknown"})["scope"] != "excluded":
                scope = appl.citation_scope(assessments.get(sid) or {"scope": "unknown"})
                cites.append(c.cite(key, span, scope))
        ml_refs = [{"kind": "ml_assessment", "id": str(m["id"])} for m in ctx.ml if m["target_entity_id"] == hid]
        if cites or ml_refs:
            c.claim(key, "hypothesis",
                    f"'{_label(graph, hid, str(hid))}'은(는) 검토된 지식에 등록된 원인 후보입니다. 점검으로 확인되기 전에는 확정 원인이 아닙니다.",
                    citation_ids=cites, event_evidence=ml_refs)

    if question_link is None and question_candidates:
        rows = await fetch_all(session, """
            SELECT entity_id, label_ko FROM rsw.entity_versions WHERE release_id = :rid AND entity_id = ANY(:ids)""",
            rid=ctx.release_id, ids=question_candidates)
        labels = {r["entity_id"]: r["label_ko"] for r in rows}
        names = ", ".join(labels[i] for i in question_candidates if i in labels)
        c.notice("QUESTION_SCENARIO_UNCERTAIN",
                 f"질문만으로는 시나리오를 확정하지 못했습니다(관련 후보: {names}). 키워드 검색과 벡터 검색이 같은 "
                 "시나리오를 가리키지 않아 절차는 제시하지 않았습니다. 경보 코드나 현장 상황(정지 중인지, 압력·소음 등)을 "
                 "더 구체적으로 알려 주세요.")

    # 3) 원문 근거 (선택된 절)
    manual_count = 0
    for sid in retrieval.selected_sections:
        span = quote(sid)
        if span is None:
            continue
        manual_count += 1
        key = f"source-{manual_count}"
        scope = appl.citation_scope(retrieval.section_assessment[sid])
        cid = c.cite(key, span, scope)
        c.claim(key, "manual_fact", span["excerpt"], citation_ids=[cid])
    scopes = {x["reference_scope"] for x in c.citations}
    if "analogous" in scopes:
        c.notice("ANALOGOUS_REFERENCE", "유사 장비 자료는 참고 설명용이며 정비 절차 수행의 근거로 사용하지 않습니다.")
    if "comparison" in scopes:
        c.notice("COMPARISON_REFERENCE", "비교용 자료는 이 장비에 그대로 적용되지 않습니다.")
    if "unknown" in scopes:
        c.notice("APPLICABILITY_UNKNOWN", "일부 근거는 이 장비 구성에 대한 적용성이 확인되지 않았습니다.")
    for conflict in retrieval.conflicts:
        c.notice("CONFLICT_UNRESOLVED", f"근거 사이의 미해결 충돌: {conflict['description']}")

    # 4) 추가 확인이 필요한 정보
    needed: list[tuple[str, str | None, bool]] = []  # (field, prompt override, scenario_required)
    for sid in graph.scenarios:
        for name, prompt in required_context_fields(graph.required_context.get(sid)):
            if facts.get(name) in UNKNOWN_VALUES and name not in conflicted_fields:
                needed.append((name, prompt, True))
    for plan in plans:
        for name in sorted(plan.unknown_fields):
            if name not in conflicted_fields:
                needed.append((name, None, False))
    for name in sorted(graph.unknown_fields):
        if name not in conflicted_fields:
            needed.append((name, None, False))
    follow_ups: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    scenario_requires_context = False
    for name, prompt, required in needed:
        if name in seen_fields:
            continue
        seen_fields.add(name)
        definition = CATALOG.get(name)
        if definition is None:
            c.notice("CONTEXT_FIELD_UNAVAILABLE",
                     f"{FIELD_LABELS.get(name, name)} 정보가 없어 일부 조건을 판단할 수 없습니다. 이 정보는 작업자 답변으로 바꿀 수 없습니다.", name)
            continue
        scenario_requires_context |= required
        follow_ups.append(definition.to_question(prompt=prompt))
    for conflict in fact_conflicts:
        c.notice("OPERATOR_INPUT_CONFLICT",
                 f"{FIELD_LABELS.get(conflict['field'], conflict['field'])}: 장비 데이터 값({conflict['event_value']})과 "
                 f"작업자 입력({conflict['operator_value']})이 다릅니다. 두 값을 모두 보존하고 판단에 사용하지 않았습니다.",
                 conflict["field"])
    operator_used = [name for name, source in provenance.items() if source == "operator_reported"]
    if operator_used:
        parts = ", ".join(f"{FIELD_LABELS.get(n, n)}={VALUE_LABELS.get(facts[n], facts[n])}" for n in operator_used)
        c.notice("OPERATOR_REPORTED_CONTEXT", f"작업자 제공 정보({parts})를 반영했습니다. 봉인된 장비 데이터는 변경하지 않았습니다.")

    # 5) 절차
    procedures: list[dict[str, Any]] = []
    if not scenario_requires_context:
        for p_index, plan in enumerate(ready_plans, start=1):
            steps_out = []
            for step in plan.steps:
                key = f"procedure-{p_index}-step-{step['step_number']}"
                span = (span_by_id.get(step["source_span_id"]) if step["source_span_id"]
                        else (spans_by_section.get(step["source_section_id"]) or [None])[0])
                if span is None:
                    steps_out = []
                    plan.blockers.append(f"step_{step['step_number']}_without_span")
                    break
                cid = c.cite(key, span, "applicable")
                c.claim(key, "procedure_guidance", step["instruction_ko"], citation_ids=[cid])
                steps_out.append({
                    "step_number": step["step_number"], "claim_key": key, "instruction": step["instruction_ko"],
                    "expected_result": step["expected_result"], "condition_summary": step["condition_summary"],
                    "condition_state": step["condition_state"], "citation_ids": [cid],
                })
            if steps_out:
                procedures.append({
                    "procedure_id": str(plan.procedure_id), "title": plan.title, "required_role": plan.required_role,
                    "preconditions": plan.preconditions, "applicability_ids": plan.applicability_ids, "steps": steps_out,
                })
        # 인용 없이 끝난 절차 단계 claim 제거 (span 누락으로 중단된 경우)
        used = {s["claim_key"] for p in procedures for s in p["steps"]}
        c.claims = [cl for cl in c.claims if cl["type"] != "procedure_guidance" or cl["claim_key"] in used]
        c.citations = [ci for ci in c.citations if not ci["claim_key"].startswith("procedure-") or ci["claim_key"] in used]
        c.citation_rows = [r for r in c.citation_rows if not r["claim_key"].startswith("procedure-") or r["claim_key"] in used]
        # 제시한 절차가 쓰는 수치 기준 (uses_spec). 원문 표기와 출처 span을 그대로 인용한다.
        presented = {UUID(p["procedure_id"]) for p in procedures}
        spec_assessment = await _applicability(session, ctx, list({r["entity_id"] for r in spec_rows}), facts)
        seen_specs: set[UUID] = set()
        for row in spec_rows:
            if row["procedure_id"] not in presented or row["entity_id"] in seen_specs:
                continue
            if spec_assessment[row["entity_id"]]["scope"] == "excluded":
                continue
            span = span_by_id.get(row["source_span_id"]) if row["source_span_id"] else quote(row["source_section_id"])
            if span is None:
                continue
            seen_specs.add(row["entity_id"])
            key = f"spec-{len(seen_specs)}"
            scope = appl.citation_scope(assessments.get(row["source_section_id"]) or {"scope": "unknown"})
            cid = c.cite(key, span, scope)
            c.claim(key, "manual_fact", f"수치 기준: {row['label_ko']} (원문 표기: {row['original_value_text']}).",
                    citation_ids=[cid])
    for plan in plans:
        if str(plan.procedure_id) not in {p["procedure_id"] for p in procedures}:
            reason = "추가 정보 확인 전" if scenario_requires_context else ", ".join(plan.blockers) or "조건 미충족"
            c.notice("PROCEDURE_WITHHELD", f"절차 '{plan.title}'은(는) 제시하지 않았습니다: {reason}.")

    # 6) 한계
    asset = ctx.integration.get("asset") or {}
    if asset.get("verification_status") == "unresolved":
        c.claim("limitation-asset", "limitation", "장비 구성을 확정하지 못해 장비별 적용 절차를 판단할 수 없습니다.")
    elif asset.get("verification_status") == "unverified":
        c.claim("limitation-asset", "limitation", "장비 구성이 검증되지 않아 적용 절차를 제시하지 않습니다.")
    for warning in ctx.warnings:
        if warning.get("code", "").startswith("ASSET_") or warning.get("code") == "ML_SCORE_UNCALIBRATED":
            c.notice(warning["code"], warning["message"], warning.get("field"))

    # 7) 상태와 요약
    evidence_claims = [cl for cl in c.claims if cl["type"] in ("manual_fact", "hypothesis") and cl["citation_ids"]]
    if scenario_requires_context and follow_ups:
        status = "needs_context"
    elif procedures:
        status = "answered"
    elif follow_ups:
        status = "needs_context"
    elif evidence_claims:
        status = "answered"
    else:
        status = "insufficient_evidence"
    if status != "answered":
        procedures = []
    scenario_text = f" 관련 시나리오 후보: {', '.join(scenario_labels)}." if scenario_labels else ""
    if graph.question_links:
        scenario_text = f" 질문 내용으로 찾은 시나리오 후보: {', '.join(scenario_labels)}."
    if status == "answered" and procedures:
        summary = (f"검토된 적용 절차 {len(procedures)}건을 근거와 함께 제시합니다.{scenario_text} "
                   "각 단계의 조건과 인용 원문을 확인한 뒤 수행하세요. 장비 제어는 자동으로 수행되지 않습니다.")
    elif status == "answered":
        summary = (f"관련 근거 {len(evidence_claims)}건을 찾았습니다.{scenario_text} "
                   "현재 정보로 이 장비에 적용이 확인된 정비 절차는 없으므로 참고 설명으로만 사용하세요.")
    elif status == "needs_context":
        fields = ", ".join(FIELD_LABELS.get(q["field"], q["field"]) for q in follow_ups)
        summary = f"현재 정보로는 장비에 맞는 정비 절차를 확정할 수 없습니다.{scenario_text} 추가로 확인할 정보: {fields}."
    else:
        summary = "현재 지식 배포본과 장비 적용성 정보로는 이 질문에 답할 근거를 확보하지 못했습니다."

    result = {"summary": summary, "claims": c.claims, "procedures": procedures,
              "follow_up_questions": follow_ups, "citations": c.citations, "notices": c.notices}
    check_answer(status, result, ctx.event_ids)

    evidence = [{"claim_key": x["claim_key"], "citation_id": x["citation_id"], "section_id": x["section_id"],
                 "span_id": x["span_id"], "quote": x["quote"], "reference_scope": x["reference_scope"]}
                for x in c.citations]
    user_message = {
        "question": ctx.question, "facts": facts, "fact_provenance": provenance,
        "scenarios": [str(s) for s in graph.scenarios], "evidence": evidence,
        "procedures": [{"procedure_id": str(p.procedure_id), "blockers": p.blockers} for p in plans],
        "follow_up_fields": [q["field"] for q in follow_ups],
    }
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "run": {"run_id": str(ctx.run_id), "question": ctx.question, "parameters": ctx.parameters},
        "context": {"context_id": str(ctx.context_id), "release_id": str(ctx.release_id),
                    "asset_config_id": str(ctx.asset_config_id) if ctx.asset_config_id else None,
                    "status": ctx.context_status, "operating_context": ctx.operating_context,
                    "warnings": ctx.warnings, "integration": ctx.integration},
        "facts": facts, "fact_provenance": provenance, "fact_conflicts": fact_conflicts,
        "graph": {"code_entities": [str(x) for x in graph.code_entities], "faults": [str(x) for x in graph.faults],
                  "hypotheses": [str(x) for x in graph.hypotheses], "scenarios": [str(x) for x in graph.scenarios],
                  "procedures": [str(x) for x in graph.procedures],
                  "question_links": {str(k): {"section_id": str(v["section_id"]), "relation_id": str(v["relation_id"]),
                                              "rank": v["rank"], "agreement": v["agreement"]}
                                     for k, v in graph.question_links.items()},
                  "question_candidates": [str(x) for x in question_candidates],
                  "relations": {str(k): v["condition_result"] for k, v in graph.relations.items()},
                  "unknown_fields": sorted(graph.unknown_fields)},
        "retrieval": {"keyword_query": retrieval.keyword_query, "vector_status": retrieval.vector_status,
                      "selected_chunks": [str(x) for x in retrieval.selected_chunks],
                      "section_applicability": {str(k): v for k, v in retrieval.section_assessment.items()},
                      "conflicts": [str(x["id"]) for x in retrieval.conflicts]},
        "procedures": [{"procedure_id": str(p.procedure_id), "blockers": p.blockers,
                        "unknown_fields": sorted(p.unknown_fields),
                        "steps": [{"step_id": str(s["id"]), "condition_state": s["condition_state"]} for s in p.steps]}
                       for p in plans],
        "generator": {
            "provider": ctx.run["model_provider"], "model": ctx.run["model_name"],
            "prompt_version": ctx.run["prompt_version"], "retriever_version": ctx.run["retriever_version"],
            "mode": "deterministic_template",
            "messages": [{"role": "system", "content": SYSTEM_RULES}, {"role": "user", "content": user_message}],
        },
    }
    return PreparedAnswer(status, result, snapshot, candidate_rows(ctx, retrieval), c.citation_rows)
