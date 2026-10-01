"""카탈로그를 실제 원문 텍스트에 맞춰 해석한다 (DB 없이 실행).

적재기(loader)와 동작 검증 테스트가 같은 결과를 쓰도록 span 위치 계산, 절 본문, 검색 청크 분할을
여기서 한 번만 정의한다.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.mvp.catalog_sections import DEFAULT_SCOPE, SECTIONS, SectionDef
from app.mvp.model import resolve_span
from app.mvp.sources import SOURCES, ExtractedPage, extract

CHUNKING_VERSION = "rsw-mvp-chunk-v1"
CHUNK_MAX_CHARS = 1200


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


@dataclass(frozen=True)
class ResolvedSpan:
    ordinal: int
    name: str | None
    page: int
    excerpt: str
    char_start: int
    char_end: int
    in_text: bool          # 절 본문을 이루는 span인지(False면 한 줄 안의 부분 anchor)


@dataclass(frozen=True)
class ResolvedSection:
    definition: SectionDef
    spans: tuple[ResolvedSpan, ...]

    @property
    def sid(self) -> str:
        return self.definition.sid

    @property
    def text_spans(self) -> list[ResolvedSpan]:
        return [s for s in self.spans if s.in_text]

    @property
    def original_text(self) -> str:
        return "\n".join(s.excerpt for s in self.text_spans)

    @property
    def pages(self) -> list[int]:
        return sorted({s.page for s in self.text_spans})

    @property
    def scope(self) -> str:
        return self.definition.scope or DEFAULT_SCOPE[self.definition.doc]

    def span(self, name: str | None) -> ResolvedSpan:
        if name is None:
            return self.text_spans[0]
        for span in self.spans:
            if span.name == name:
                return span
        raise KeyError(f"{self.sid}#{name}")


@dataclass(frozen=True)
class ChunkPlan:
    sid: str
    ordinal: int
    content: str
    heading_path: str
    search_terms: str
    language: str
    page_start: int
    page_end: int


def load_pages(raw_dir: Path, *, verify: bool = True) -> dict[str, list[ExtractedPage]]:
    return {key: extract(raw_dir, source, verify=verify) for key, source in SOURCES.items()}


def resolve_sections(pages: dict[str, list[ExtractedPage]]) -> dict[str, ResolvedSection]:
    resolved: dict[str, ResolvedSection] = {}
    for section in SECTIONS:
        by_number = {p.number: p.text for p in pages[section.doc]}
        spans = []
        for ordinal, (ref, in_text) in enumerate(
                [(r, True) for r in section.spans] + [(r, False) for r in section.anchors], start=1):
            start, end, excerpt = resolve_span(by_number[ref.page], ref)
            spans.append(ResolvedSpan(ordinal, ref.name, ref.page, excerpt, start, end, in_text))
        resolved[section.sid] = ResolvedSection(section, tuple(spans))
    return resolved


def heading_path(section: SectionDef) -> str:
    source = SOURCES[section.doc]
    number = f"{section.number} " if section.number and not section.title.startswith(section.number) else ""
    return f"{source.label_ko} > {number}{section.title}"


# 정의서 상황 절은 '고장·경보 - 원인 - 점검' 의미 단위로 나눈다 (RAG 활용정리 9장 문서 자르는 단위).
SITUATION_GROUPS = (("definition", "symptoms", "signals"), ("causes", "checks", "refs"))


def _situation_questions() -> dict[str, str]:
    """정의서의 예상 작업자 질문은 근거 인용이 아니라 검색어로만 쓴다 (scenarios.question_examples와 같은 값)."""
    from app.mvp.catalog_knowledge import SCENARIOS

    return {f"D-{s.entity.code}": " ".join(s.question_examples) for s in SCENARIOS}


def _groups(section: ResolvedSection) -> list[list[ResolvedSpan]]:
    spans = section.text_spans
    names = {s.name for s in spans}
    if section.definition.doc == "rsw-mvp-situations" and names >= {"symptoms", "causes", "checks"}:
        groups = [[s for s in spans if s.name in group] for group in SITUATION_GROUPS]
        return [g for g in groups if g]
    groups: list[list[ResolvedSpan]] = [[]]
    size = 0
    for span in spans:
        if groups[-1] and size + len(span.excerpt) + 1 > CHUNK_MAX_CHARS:
            groups.append([])
            size = 0
        groups[-1].append(span)
        size += len(span.excerpt) + 1
    return groups


def plan_chunks(sections: dict[str, ResolvedSection]) -> list[ChunkPlan]:
    """절 본문 span을 순서대로 모아 청크로 나눈다. span은 쪼개지 않는다."""
    plans: list[ChunkPlan] = []
    questions = _situation_questions()
    for section in sections.values():
        if not section.definition.searchable:
            continue
        terms = section.definition.terms
        if section.sid in questions:
            terms = f"{terms} {questions[section.sid]}"
        for ordinal, group in enumerate(_groups(section), start=1):
            plans.append(ChunkPlan(
                section.sid, ordinal, "\n".join(s.excerpt for s in group), heading_path(section.definition),
                terms, SOURCES[section.definition.doc].language,
                min(s.page for s in group), max(s.page for s in group)))
    return plans


def embedding_passage(chunk: ChunkPlan) -> str:
    """임베딩 입력(passage). 한국어 절 제목·검색어를 영문 원문 앞에 붙여 한국어 질문과의 거리를 줄인다."""
    return f"{chunk.heading_path}\n{chunk.search_terms}\n{chunk.content}"
