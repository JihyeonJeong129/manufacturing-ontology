"""RSW MVP 실제 지식 적재 (python -m app.cli load-mvp).

한 트랜잭션 안에서 스키마 계약 순서를 그대로 따른다:
원문 판본 등록(쪽·절·span) → 봉인 → 검색 청크·임베딩 → 지식 배포본 draft 작성 → 도메인 검토 승인 →
동결 → RDF/SHACL·도메인 검사 후 발행 → pilot 활성화 → 평가 케이스 봉인.
이미 발행된 같은 버전이 있으면 새로 만들지 않고 토큰·원본 파일·임베딩·활성 포인터만 보정한다.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import token_sha256
from app.core.config import Settings
from app.core.jsonutil import dumps
from app.db.sqlutil import execute, fetch_all, fetch_one, fetch_value
from app.mvp import build
from app.mvp import catalog_knowledge as K
from app.mvp.catalog_sections import BY_ID as SECTION_DEFS
from app.mvp.catalog_sections import DOCX, FANUC, FESTO, RAG
from app.mvp.model import EntityDef
from app.mvp.scenarios import ASSET_CODE, CASES, PRODUCER_KEY, alert_payload
from app.mvp.sources import SOURCES, file_sha256, locate, printed_label
from app.ontology import publisher
from app.services.answering import embedding as emb

RELEASE_VERSION = "rsw-mvp-v1"
LOADER, REVIEWER, OPERATOR, SERVICE = "rsw:loader", "rsw:reviewer", "rsw:operator", "rsw:ml-service"
CONFIG_BASIS = (
    "MVP 기준 장비 가정 (RSW 용접건 MVP 오류 상황 정의서 2026-09-25): 논문 데이터의 서보 공압식 RSW 용접건을 구조가 "
    "가장 가까운 Festo CPX-SB-LT-WELD-SA 구동계로 가정한다. 실제 제조사는 미확인이며 팀 확정 전이다."
)
BINDING_BASIS = ("Wang et al.(2024) 데이터셋 변수 설명 (RAG 활용정리 p.1 보유 데이터 핵심, 정의서 구성 부품 표). "
                 "센서 이름이 마스킹되어 단위는 미확인.")
SCOPE_BASIS = {
    "applicable": "MVP 장비 기준 문서 (정의서·RAG 활용정리는 MVP 장비 기준 팀 분석, Festo는 MVP 기준 장비 가정).",
    "analogous": "유사 장비 참고: 공압식 건·공정 제어 매뉴얼 (RAG 활용정리 권장 신뢰도 계층).",
    "comparison": "구동 방식이 다른 전동 서보 비교 참고. 공압 고유 원인에 직접 적용하지 않는다 (RAG 활용정리 5장).",
}
SCOPE_EVIDENCE = {DOCX: ["D-OVERVIEW"], RAG: ["D-OVERVIEW"], FESTO: ["D-OVERVIEW", "R-TIERS"], FANUC: ["R-TIERS", "R-FANUC"]}


class LoadError(RuntimeError):
    pass


def _lang(value: str) -> str:
    return "ko" if re.search(r"[가-힣]", value) else "en"


async def _actor(session: AsyncSession, key: str, name: str, role: str) -> UUID:
    await execute(session, """
        INSERT INTO rsw.actors (external_key, display_name, role) VALUES (:k, :n, :r)
        ON CONFLICT (external_key) DO NOTHING""", k=key, n=name, r=role)
    return await fetch_value(session, "SELECT id FROM rsw.actors WHERE external_key = :k", k=key)


async def _credential(session: AsyncSession, actor: UUID, token: str, label: str, producer: UUID | None = None) -> None:
    digest = token_sha256(token)
    existing = await fetch_one(session, "SELECT actor_id FROM rsw.api_credentials WHERE token_sha256 = :t", t=digest)
    if existing is not None and existing["actor_id"] != actor:
        raise LoadError(f"토큰이 다른 주체에 이미 등록되어 있습니다 ({label}). MVP_* 토큰 값을 바꾸세요.")
    await execute(session, """
        INSERT INTO rsw.api_credentials (actor_id, token_sha256, producer_id, label) VALUES (:a, :t, :p, :l)
        ON CONFLICT (token_sha256) DO NOTHING""", a=actor, t=digest, p=producer, l=label)


async def _entity(session: AsyncSession, kind: str, namespace: str, code: str) -> UUID:
    await execute(session, """
        INSERT INTO rsw.knowledge_entities (kind, namespace, local_code) VALUES (:k, :ns, :c)
        ON CONFLICT (kind, namespace, local_code) DO NOTHING""", k=kind, ns=namespace, c=code)
    return await fetch_value(session, """
        SELECT id FROM rsw.knowledge_entities WHERE kind = :k AND namespace = :ns AND local_code = :c""",
        k=kind, ns=namespace, c=code)


def section_namespace(doc_key: str) -> str:
    source = SOURCES[doc_key]
    return f"doc:{source.key}@{source.revision}"


# ---------------------------------------------------------------- 원문


class SourceState:
    def __init__(self) -> None:
        self.revisions: dict[str, UUID] = {}
        self.sections: dict[str, UUID] = {}
        self.spans: dict[tuple[str, str | None], UUID] = {}   # (절, span 이름 또는 None=첫 본문 span)
        self.chunks: dict[tuple[str, int], UUID] = {}
        self.created_revisions: list[str] = []

    def span(self, ref: str) -> tuple[UUID, UUID]:
        """'절#이름' → (section_id, span_id)."""
        sid, _, name = ref.partition("#")
        key = (sid, name or None)
        if key not in self.spans:
            raise LoadError(f"근거 span을 찾을 수 없습니다: {ref}")
        return self.sections[sid], self.spans[key]


def _store_file(raw_dir: Path, storage_root: Path, doc_key: str) -> str:
    source = SOURCES[doc_key]
    path = locate(raw_dir, source)
    target = storage_root / source.storage_relative
    if not target.exists() or file_sha256(target) != source.sha256:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return f"storage:{source.storage_relative}"


async def load_sources(session: AsyncSession, cfg: Settings, raw_dir: Path, created_by: UUID,
                       pages: dict[str, list], resolved: dict[str, build.ResolvedSection]) -> SourceState:
    state = SourceState()
    by_doc: dict[str, list[build.ResolvedSection]] = {}
    for section in resolved.values():
        by_doc.setdefault(section.definition.doc, []).append(section)
    for doc_key, source in SOURCES.items():
        uri = _store_file(raw_dir, cfg.STORAGE_ROOT, doc_key)
        row = await fetch_one(session, """
            SELECT id, sealed_at FROM rsw.document_revisions
            WHERE document_key = :k AND revision_key = :r AND extraction_version = :e""",
            k=source.key, r=source.revision, e=source.extraction_version)
        if row is None:
            revision_id = await fetch_value(session, """
                INSERT INTO rsw.document_revisions
                  (document_key, revision_key, source_kind, title, publisher, edition_label, language, source_uri,
                   source_sha256, extraction_version, page_count, created_by)
                VALUES (:k, :r, :kind, :title, :pub, :ed, :lang, :uri, :sha, :ext, :pc, :by) RETURNING id""",
                k=source.key, r=source.revision, kind=source.source_kind, title=source.title, pub=source.publisher,
                ed=source.edition, lang=source.language, uri=uri, sha=source.sha256, ext=source.extraction_version,
                pc=len(pages[doc_key]), by=created_by)
            await session.execute(text("""
                INSERT INTO rsw.document_pages (document_revision_id, page_number, locator_kind, printed_page_label,
                                                page_text, text_sha256, extraction_method)
                VALUES (:d, :n, :kind, :label, :t, :s, 'native')"""), [
                {"d": revision_id, "n": page.number, "kind": "pdf_page" if source.is_pdf else "logical_block",
                 "label": printed_label(source, page), "t": page.text, "s": build.sha(page.text)}
                for page in pages[doc_key]])
            for section in by_doc.get(doc_key, []):
                definition = section.definition
                section_id = await _entity(session, "document_section", section_namespace(doc_key), definition.sid)
                await execute(session, """
                    INSERT INTO rsw.document_sections (id, document_revision_id, section_key, section_number, title,
                                                       original_text, text_sha256)
                    VALUES (:id, :d, :k, :n, :t, :o, :s)""",
                    id=section_id, d=revision_id, k=definition.sid, n=definition.number, t=definition.title,
                    o=section.original_text, s=build.sha(section.original_text))
                for span in section.spans:
                    await execute(session, """
                        INSERT INTO rsw.section_spans (section_id, document_revision_id, page_number, ordinal, excerpt,
                                                       char_start, char_end)
                        VALUES (:s, :d, :p, :o, :e, :cs, :ce)""",
                        s=section_id, d=revision_id, p=span.page, o=span.ordinal, e=span.excerpt,
                        cs=span.char_start, ce=span.char_end)
            await execute(session, "UPDATE rsw.document_revisions SET sealed_at = now() WHERE id = :id", id=revision_id)
            state.created_revisions.append(doc_key)
        else:
            if row["sealed_at"] is None:
                raise LoadError(f"{doc_key} 판본이 봉인되지 않은 채 남아 있습니다. 수동 확인이 필요합니다.")
            revision_id = row["id"]
        state.revisions[doc_key] = revision_id
        # 절·span ID를 DB에서 다시 읽고, 봉인된 원문과 카탈로그가 같은지 확인한다.
        rows = await fetch_all(session, """
            SELECT ds.section_key, ds.id, ds.text_sha256, sp.id AS span_id, sp.ordinal, sp.excerpt
            FROM rsw.document_sections ds JOIN rsw.section_spans sp ON sp.section_id = ds.id
            WHERE ds.document_revision_id = :d ORDER BY ds.section_key, sp.ordinal""", d=revision_id)
        stored: dict[str, dict[int, dict[str, Any]]] = {}
        for r in rows:
            stored.setdefault(r["section_key"], {})[r["ordinal"]] = r
        for section in by_doc.get(doc_key, []):
            spans = stored.get(section.sid)
            if not spans or any(spans.get(s.ordinal, {}).get("excerpt") != s.excerpt for s in section.spans):
                raise LoadError(f"봉인된 판본 {doc_key}의 절 {section.sid}이 카탈로그와 다릅니다. 판본/추출 버전을 올리세요.")
            state.sections[section.sid] = next(iter(spans.values()))["id"]
            for s in section.spans:
                state.spans[(section.sid, s.name)] = spans[s.ordinal]["span_id"]
            state.spans[(section.sid, None)] = spans[section.text_spans[0].ordinal]["span_id"]
    return state


async def load_chunks(session: AsyncSession, state: SourceState, plans: list[build.ChunkPlan]) -> None:
    for plan in plans:
        section_id = state.sections[plan.sid]
        revision_id = state.revisions[SECTION_DEFS[plan.sid].doc]
        await execute(session, """
            INSERT INTO rsw.retrieval_chunks (section_id, document_revision_id, chunking_version, ordinal, language,
                                              content, heading_path, search_terms, content_sha256, page_start, page_end)
            VALUES (:s, :d, :v, :o, :lang, :c, :h, :t, :sha, :ps, :pe)
            ON CONFLICT (section_id, chunking_version, ordinal) DO NOTHING""",
            s=section_id, d=revision_id, v=build.CHUNKING_VERSION, o=plan.ordinal, lang=plan.language, c=plan.content,
            h=plan.heading_path, t=plan.search_terms, sha=build.sha(plan.content), ps=plan.page_start, pe=plan.page_end)
        row = await fetch_one(session, """
            SELECT id, content_sha256, heading_path, search_terms FROM rsw.retrieval_chunks
            WHERE section_id = :s AND chunking_version = :v AND ordinal = :o""",
            s=section_id, v=build.CHUNKING_VERSION, o=plan.ordinal)
        if (row["content_sha256"], row["heading_path"], row["search_terms"]) != (build.sha(plan.content), plan.heading_path, plan.search_terms):
            raise LoadError(f"검색 청크 {plan.sid}#{plan.ordinal}이 카탈로그와 다릅니다. CHUNKING_VERSION을 올리세요.")
        state.chunks[(plan.sid, plan.ordinal)] = row["id"]


async def load_embeddings(session: AsyncSession, cfg: Settings, state: SourceState,
                          plans: list[build.ChunkPlan]) -> dict[str, Any]:
    if cfg.EMBEDDING_BACKEND != "local":
        return {"status": "disabled", "reason": "EMBEDDING_BACKEND != local"}
    embedder = emb.local_embedder(cfg.EMBEDDING_MODEL_DIR)
    if embedder is None:
        return {"status": "model_missing", "model_dir": str(cfg.EMBEDDING_MODEL_DIR)}
    model_id = await emb.ensure_model_row(session)
    ids = [state.chunks[(p.sid, p.ordinal)] for p in plans]
    have = {r["chunk_id"] for r in await fetch_all(session, """
        SELECT chunk_id FROM rsw.chunk_embeddings WHERE embedding_model_id = :m AND chunk_id = ANY(:ids)""",
        m=model_id, ids=ids)}
    missing = [p for p in plans if state.chunks[(p.sid, p.ordinal)] not in have]
    if missing:
        vectors = await asyncio.to_thread(embedder.embed_passages, [build.embedding_passage(p) for p in missing])
        await session.execute(text("""
            INSERT INTO rsw.chunk_embeddings (chunk_id, embedding_model_id, dimensions, embedding)
            VALUES (:c, :m, :d, CAST(:v AS public.vector))"""), [
            {"c": state.chunks[(p.sid, p.ordinal)], "m": model_id, "d": len(v),
             "v": "[" + ",".join(repr(x) for x in v) + "]"} for p, v in zip(missing, vectors)])
    return {"status": "ok", "embedding_model_id": str(model_id), "model": emb.LOCAL_SPEC.model_name,
            "revision": emb.LOCAL_SPEC.revision, "dimensions": emb.LOCAL_SPEC.dimensions,
            "chunks": len(plans), "embedded_now": len(missing)}


# ---------------------------------------------------------------- 지식 배포본


async def load_release_content(session: AsyncSession, release_id: UUID, state: SourceState, config_id: UUID,
                               reviewer: UUID, plans: list[build.ChunkPlan]) -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    entity_defs: dict[str, EntityDef] = {e.ref: e for e in K.ENTITIES}

    async def version(entity_id: UUID, kind: str, label_ko: str, label_en: str | None, description: str,
                      metadata: dict[str, Any]) -> None:
        await execute(session, """
            INSERT INTO rsw.entity_versions (release_id, entity_id, entity_kind, label_ko, label_en, description, metadata, review_status)
            VALUES (:r, :e, :k, :lk, :le, :d, CAST(:m AS jsonb), 'reviewed')""",
            r=release_id, e=entity_id, k=kind, lk=label_ko, le=label_en, d=description, m=dumps(metadata))

    for ref, entity in entity_defs.items():
        ids[ref] = await _entity(session, entity.kind, entity.namespace, entity.code)
        await version(ids[ref], entity.kind, entity.label_ko, entity.label_en, entity.description, entity.metadata)
        for alias in entity.aliases:
            await execute(session, """
                INSERT INTO rsw.entity_aliases (release_id, entity_id, language, alias) VALUES (:r, :e, :l, :a)
                ON CONFLICT DO NOTHING""", r=release_id, e=ids[ref], l=_lang(alias), a=alias)
    for sid, definition in SECTION_DEFS.items():
        source = SOURCES[definition.doc]
        ids[f"sec:{sid}"] = state.sections[sid]
        pages = sorted({s.page for s in definition.spans})
        await version(state.sections[sid], "document_section", definition.label_ko,
                      definition.title if source.language == "en" else None,
                      f"{source.title} {definition.number or ''} {definition.title} (쪽: {', '.join(map(str, pages))}).".replace("  ", " "),
                      {"document_key": source.key, "manual_id": source.manual_id, "drive_type": source.drive_type,
                       "pages": pages, "locator_kind": "pdf_page" if source.is_pdf else "logical_block"})

    for scenario in K.SCENARIOS:
        await execute(session, """
            INSERT INTO rsw.scenarios (release_id, entity_id, scenario_kind, question_examples, required_context)
            VALUES (:r, :e, :k, CAST(:q AS jsonb), CAST(:rc AS jsonb))""",
            r=release_id, e=ids[scenario.entity.ref], k=scenario.scenario_kind, q=dumps(list(scenario.question_examples)),
            rc=dumps(list(scenario.required_context)))
    for feature in K.FEATURES:
        await execute(session, """
            INSERT INTO rsw.feature_definitions (release_id, entity_id, feature_kind, value_type, unit, unit_status, definition_status)
            VALUES (:r, :e, :fk, :vt, :u, :us, :ds)""",
            r=release_id, e=ids[feature.entity.ref], fk=feature.feature_kind, vt=feature.value_type, u=feature.unit,
            us=feature.unit_status, ds=feature.definition_status)
        await execute(session, """
            INSERT INTO rsw.feature_bindings (release_id, asset_config_id, source_tag, feature_id, source_unit,
                                              conversion, verification_status, verification_basis)
            VALUES (:r, :c, :tag, :f, NULL, '{}'::jsonb, 'verified', :basis)""",
            r=release_id, c=config_id, tag=feature.source_tag, f=ids[feature.entity.ref], basis=BINDING_BASIS)
    for pattern in K.PATTERNS:
        await execute(session, """
            INSERT INTO rsw.evidence_patterns (release_id, entity_id, pattern_kind, conditions, baseline_definition,
                                               window_definition, empirical_status)
            VALUES (:r, :e, :k, CAST(:c AS jsonb), CAST(:b AS jsonb), CAST(:w AS jsonb), :s)""",
            r=release_id, e=ids[pattern.entity.ref], k=pattern.pattern_kind, c=dumps(pattern.conditions),
            b=None if pattern.baseline_definition is None else dumps(pattern.baseline_definition),
            w=None if pattern.window_definition is None else dumps(pattern.window_definition), s=pattern.empirical_status)
    for procedure in K.PROCEDURES:
        await execute(session, """
            INSERT INTO rsw.procedures (release_id, entity_id, procedure_kind, preconditions, required_role, source_section_id)
            VALUES (:r, :e, :k, CAST(:p AS jsonb), :role, :s)""",
            r=release_id, e=ids[procedure.entity.ref], k=procedure.procedure_kind, p=dumps(list(procedure.preconditions)),
            role=procedure.required_role, s=state.sections[procedure.source_section])
        for number, step in enumerate(procedure.steps, start=1):
            section_id, span_id = state.span(step.source)
            await execute(session, """
                INSERT INTO rsw.procedure_steps (release_id, procedure_id, step_number, instruction_ko, expected_result,
                                                 branch_condition, source_section_id, source_span_id)
                VALUES (:r, :p, :n, :i, :x, CAST(:b AS jsonb), :s, :sp)""",
                r=release_id, p=ids[procedure.entity.ref], n=number, i=step.instruction_ko, x=step.expected_result,
                b=None if step.branch_condition is None else dumps(step.branch_condition), s=section_id, sp=span_id)
    for spec in K.SPECS:
        section_id, span_id = state.span(spec.source)
        await execute(session, """
            INSERT INTO rsw.numeric_specs (release_id, entity_id, quantity, comparison, value_low, value_high,
                                           original_value_text, unit, spec_kind, conditions, interpretation_status,
                                           source_section_id, source_span_id)
            VALUES (:r, :e, :q, :c, :lo, :hi, :o, :u, :k, CAST(:cond AS jsonb), :st, :s, :sp)""",
            r=release_id, e=ids[spec.entity.ref], q=spec.quantity, c=spec.comparison, lo=spec.value_low,
            hi=spec.value_high, o=spec.original_value_text, u=spec.unit, k=spec.spec_kind, cond=dumps(spec.conditions),
            st=spec.interpretation_status, s=section_id, sp=span_id)

    kinds = {ref: e.kind for ref, e in entity_defs.items()} | {f"sec:{sid}": "document_section" for sid in SECTION_DEFS}
    for relation in K.RELATIONS:
        relation_id = await fetch_value(session, """
            INSERT INTO rsw.knowledge_relations (release_id, subject_id, subject_kind, predicate, object_id, object_kind,
                                                 conditions, evidence_kind, empirical_status, review_status)
            VALUES (:r, :s, :sk, :p, :o, :ok, CAST(:c AS jsonb), :ek, :es, 'reviewed') RETURNING id""",
            r=release_id, s=ids[relation.subject], sk=kinds[relation.subject], p=relation.predicate,
            o=ids[relation.object], ok=kinds[relation.object], c=dumps(relation.conditions),
            ek=relation.evidence_kind, es=relation.empirical_status)
        for ref in relation.evidence:
            section_id, span_id = state.span(ref)
            await execute(session, """
                INSERT INTO rsw.relation_evidence (release_id, relation_id, section_id, span_id, evidence_role, explanation)
                VALUES (:r, :rel, :s, :sp, 'supports', :x)""",
                r=release_id, rel=relation_id, s=section_id, sp=span_id, x=relation.explanation)

    async def applicability(content_ref: str, kind: str, scope: str, evidence: list[str]) -> None:
        row_id = await fetch_value(session, """
            INSERT INTO rsw.applicability (release_id, content_entity_id, content_kind, asset_config_id, reference_scope,
                                           conditions, review_status, reviewed_by, review_basis)
            VALUES (:r, :c, :k, :ac, :scope, '{}'::jsonb, 'reviewed', :by, :basis) RETURNING id""",
            r=release_id, c=ids[content_ref], k=kind, ac=config_id, scope=scope, by=reviewer, basis=SCOPE_BASIS[scope])
        for sid in dict.fromkeys(evidence):
            await execute(session, """
                INSERT INTO rsw.applicability_evidence (release_id, applicability_id, section_id) VALUES (:r, :a, :s)""",
                r=release_id, a=row_id, s=state.sections[sid])

    for sid, definition in SECTION_DEFS.items():
        scope = definition.scope or build.DEFAULT_SCOPE[definition.doc]
        await applicability(f"sec:{sid}", "document_section", scope, SCOPE_EVIDENCE.get(definition.doc, ["R-TIERS"]))
    for procedure in K.PROCEDURES:
        await applicability(procedure.entity.ref, "procedure", "applicable", [procedure.source_section, "D-OVERVIEW"])
    for spec in K.SPECS:
        sid = spec.source.split("#")[0]
        await applicability(spec.entity.ref, "numeric_spec", build.DEFAULT_SCOPE[SECTION_DEFS[sid].doc], [sid])

    for affected, left, right, description, resolution in K.CONFLICTS:
        await execute(session, """
            INSERT INTO rsw.knowledge_conflicts (release_id, affected_entity_id, left_section_id, right_section_id,
                                                 description, status, resolution, resolved_by)
            VALUES (:r, :e, :l, :rt, :d, 'resolved', :res, :by)""",
            r=release_id, e=ids[affected], l=state.sections[left], rt=state.sections[right], d=description,
            res=resolution, by=reviewer)
    for plan in plans:
        await execute(session, "INSERT INTO rsw.release_chunks (release_id, chunk_id, section_id) VALUES (:r, :c, :s)",
                      r=release_id, c=state.chunks[(plan.sid, plan.ordinal)], s=state.sections[plan.sid])
    return ids


async def load_evaluation_cases(session: AsyncSession, release_id: UUID, state: SourceState, reviewer: UUID) -> list[str]:
    created = []
    for case in CASES:
        key = f"rsw-mvp-{case.key}"
        exists = await fetch_one(session, "SELECT id FROM rsw.evaluation_cases WHERE case_key = :k AND release_id = :r",
                                 k=key, r=release_id)
        if exists is not None:
            continue
        revision = (await fetch_value(session, "SELECT coalesce(max(revision), 0) FROM rsw.evaluation_cases WHERE case_key = :k",
                                      k=key)) + 1
        snapshot = {"kind": case.kind, "expected_scenario": case.scenario, "expected_methods": list(case.expected_methods),
                    "question": case.question, "alert": alert_payload(case, event_id=f"eval-{case.key.lower()}")}
        case_id = await fetch_value(session, """
            INSERT INTO rsw.evaluation_cases (case_key, revision, release_id, split, origin, question, input_snapshot,
                                              expected_status, required_questions, forbidden_claims, reviewed_by, reviewed_at)
            VALUES (:k, :rev, :r, 'regression', 'expert_authored', :q, CAST(:snap AS jsonb), 'answered', '[]'::jsonb,
                    CAST(:forbidden AS jsonb), :by, now()) RETURNING id""",
            k=key, rev=revision, r=release_id, q=case.question, snap=dumps(snapshot),
            forbidden=dumps(["FANUC 모터 고장을 이 장비의 원인으로 단정"]), by=reviewer)
        for sid, grade in case.expected_sections.items():
            await execute(session, """
                INSERT INTO rsw.evaluation_expected_sections (case_id, release_id, section_id, relevance_grade)
                VALUES (:c, :r, :s, :g)""", c=case_id, r=release_id, s=state.sections[sid], g=grade)
        await execute(session, "UPDATE rsw.evaluation_cases SET sealed_at = now() WHERE id = :id", id=case_id)
        created.append(key)
    return created


# ---------------------------------------------------------------- 진입점


async def load_mvp(session: AsyncSession, cfg: Settings, *, raw_dir: Path | None = None, embeddings: bool = True,
                   publish_release: bool = True, activate: bool = True) -> dict[str, Any]:
    """한 트랜잭션 안에서 호출한다. 반환값은 적재 보고서."""
    raw_dir = raw_dir or cfg.RAW_DATA_DIR
    loader = await _actor(session, LOADER, "RSW MVP 적재 도구", "developer")
    reviewer = await _actor(session, REVIEWER, "RSW MVP 도메인 검토자", "domain_reviewer")
    operator = await _actor(session, OPERATOR, "RSW 현장 작업자", "operator")
    service = await _actor(session, SERVICE, "RSW ML 핸드오프 어댑터", "service")
    await execute(session, """
        INSERT INTO rsw.event_producers (producer_key, description) VALUES (:k, :d)
        ON CONFLICT (producer_key) DO NOTHING""", k=PRODUCER_KEY,
        d="ML 핸드오프 어댑터: fault_class → classification, hypotheses[].id → rule_trigger, trigger.rule_code → observed_codes")
    producer = await fetch_value(session, "SELECT id FROM rsw.event_producers WHERE producer_key = :k", k=PRODUCER_KEY)
    await _credential(session, service, cfg.MVP_ML_TOKEN, "RSW ML producer token", producer)
    await _credential(session, operator, cfg.MVP_OPERATOR_TOKEN, "RSW operator token")
    await _credential(session, reviewer, cfg.MVP_REVIEWER_TOKEN, "RSW reviewer token")

    await execute(session, """
        INSERT INTO rsw.assets (asset_code, display_name, location) VALUES (:c, :n, :l) ON CONFLICT (asset_code) DO NOTHING""",
        c=ASSET_CODE, n="서보 공압식 RSW 용접건 (논문 데이터 MVP 장비)", l="자동차 차체 공장 (Wang et al., 2024 데이터셋)")
    asset_id = await fetch_value(session, "SELECT id FROM rsw.assets WHERE asset_code = :c", c=ASSET_CODE)
    await execute(session, """
        INSERT INTO rsw.asset_configs
          (asset_id, revision, manufacturer, model, controller_manufacturer, controller_model, drive_type, components,
           verification_status, verified_by, verified_at, verification_basis, effective_from)
        VALUES (:a, 1, 'Festo (MVP 기준 가정)', 'CPX-SB-LT-WELD-SA 기준 서보 공압 용접건', 'Festo', 'CPX-SB-LT-WELD-SA',
                'servo_pneumatic', CAST(:comp AS jsonb), 'verified', :by, now(), :basis, :eff)
        ON CONFLICT (asset_id, revision) DO NOTHING""",
        a=asset_id, comp=dumps({c.code: c.label_ko for c in K.COMPONENTS}),
        by=reviewer, basis=CONFIG_BASIS, eff=datetime(2024, 1, 1, tzinfo=timezone.utc))
    config_id = await fetch_value(session, "SELECT id FROM rsw.asset_configs WHERE asset_id = :a AND revision = 1", a=asset_id)

    pages = await asyncio.to_thread(build.load_pages, raw_dir)
    resolved = build.resolve_sections(pages)
    plans = build.plan_chunks(resolved)
    state = await load_sources(session, cfg, raw_dir, loader, pages, resolved)
    await load_chunks(session, state, plans)
    embedding_report = await load_embeddings(session, cfg, state, plans) if embeddings else {"status": "skipped"}

    existing = await fetch_one(session, "SELECT id, status FROM rsw.knowledge_releases WHERE version = :v", v=RELEASE_VERSION)
    created = existing is None
    publish_report: dict[str, Any] | None = None
    if existing is None:
        release_id = await publisher.create_draft(session, version=RELEASE_VERSION, created_by=loader,
                                                  validation_contract_version=cfg.VALIDATION_CONTRACT_VERSION)
        await load_release_content(session, release_id, state, config_id, reviewer, plans)
        await publisher.approve(session, release_id, reviewer)
        await publisher.freeze(session, release_id)
        status = "frozen"
    else:
        release_id, status = existing["id"], existing["status"]
        if status == "draft":
            raise LoadError(f"{RELEASE_VERSION}가 draft 상태로 남아 있습니다. 검토 후 release 명령으로 진행하세요.")
    if status == "frozen" and publish_release:
        publish_report = await publisher.publish(session, release_id, cfg.STORAGE_ROOT)
        if publish_report["result"] != "published":
            failed = [c for c in publish_report["domain_checks"] if not c["passed"] and c["severity"] == "failure"]
            raise LoadError(f"발행 검사 실패: SHACL={publish_report['shacl']['conforms']} "
                            f"{publish_report['shacl']['report_text'][:1500]} domain={failed}")
        status = "published"
    if status == "published":
        if activate:
            await publisher.activate(session, release_id, cfg.KNOWLEDGE_DEPLOYMENT, loader)
        cases = await load_evaluation_cases(session, release_id, state, reviewer)
    else:
        cases = []
    return {
        "created": created, "release_version": RELEASE_VERSION, "release_id": str(release_id), "release_status": status,
        "activated": activate and status == "published", "deployment": cfg.KNOWLEDGE_DEPLOYMENT,
        "asset_code": ASSET_CODE, "asset_config_id": str(config_id),
        "sources": {k: {"revision_id": str(v), "created": k in state.created_revisions} for k, v in state.revisions.items()},
        "sections": len(state.sections), "chunks": len(state.chunks), "embeddings": embedding_report,
        "evaluation_cases_created": cases,
        "publish": None if publish_report is None else {
            "graph_uri": publish_report["graph"]["uri"], "graph_sha256": publish_report["graph"]["sha256"],
            "triples": publish_report["graph"]["triples"], "shacl_conforms": publish_report["shacl"]["conforms"],
            "domain_checks": [{"check": c["check"], "passed": c["passed"], "severity": c["severity"],
                               "violations": c["violation_count"]} for c in publish_report["domain_checks"]]},
        "source_notes": K.SOURCE_NOTES,
    }
