"""적재 카탈로그의 선언형 데이터 구조.

카탈로그는 원문을 다시 타이핑하지 않는다. 인용 span은 (쪽, 시작 문자열, 끝 문자열)로 적고,
적재할 때 봉인 대상 추출 텍스트에서 실제 부분 문자열과 문자 위치를 찾는다(resolve_span).
찾지 못하면 적재가 실패하므로 원문과 다른 인용이 DB에 들어가지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpanRef:
    page: int                 # PDF 쪽 번호 또는 Word 논리 블록 번호
    start: str                # span이 시작하는 원문 문자열
    end: str | None = None    # span이 끝나는 원문 문자열(포함). 없으면 start 자체가 span
    name: str | None = None   # 관계 근거·절차 단계에서 참조할 이름
    nth: int = 1              # 같은 쪽에 start가 여러 번 나오면 몇 번째인지


@dataclass(frozen=True)
class SectionDef:
    sid: str                  # 카탈로그 전역 키이자 document_sections.section_key
    doc: str                  # SOURCES 키
    number: str | None
    title: str                # 원문 표기 제목
    label_ko: str             # 개념 표시 이름
    spans: tuple[SpanRef, ...]            # 절 본문 = span들을 줄바꿈으로 이은 것
    terms: str                # 검토된 한국어 검색어 (retrieval_chunks.search_terms)
    anchors: tuple[SpanRef, ...] = ()     # 본문 밖 추가 span (절차 단계 등 한 줄 안의 일부)
    scope: str | None = None  # 적용성 재정의 (기본은 문서별)
    searchable: bool = True   # False면 검색 청크를 만들지 않는다 (문서 구조 설명 같은 메타 절)


@dataclass(frozen=True)
class EntityDef:
    ref: str                  # 카탈로그 전역 키 (예: fault:E01)
    kind: str
    namespace: str
    code: str
    label_ko: str
    description: str
    label_en: str | None = None
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioDef:
    entity: EntityDef
    scenario_kind: str
    question_examples: tuple[str, ...]
    required_context: tuple[Any, ...] = ()


@dataclass(frozen=True)
class FeatureDef:
    entity: EntityDef
    feature_kind: str
    value_type: str
    source_tag: str
    unit: str | None = None
    unit_status: str = "unknown"
    definition_status: str = "source_checked"


@dataclass(frozen=True)
class PatternDef:
    entity: EntityDef
    pattern_kind: str
    conditions: dict[str, Any]
    baseline_definition: dict[str, Any] | None = None
    window_definition: dict[str, Any] | None = None
    empirical_status: str = "hypothesis"


@dataclass(frozen=True)
class StepDef:
    instruction_ko: str
    expected_result: str | None
    source: str               # "SECTION#span-name" 또는 "SECTION" (첫 span)
    branch_condition: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProcedureDef:
    entity: EntityDef
    procedure_kind: str
    required_role: str
    preconditions: tuple[str, ...]
    source_section: str
    steps: tuple[StepDef, ...]


@dataclass(frozen=True)
class SpecDef:
    entity: EntityDef
    quantity: str
    comparison: str
    value_low: float | None
    value_high: float | None
    original_value_text: str
    unit: str | None
    spec_kind: str
    source: str               # "SECTION#span-name"
    conditions: dict[str, Any] = field(default_factory=dict)
    interpretation_status: str = "verified"


@dataclass(frozen=True)
class RelationDef:
    subject: str              # 개념 ref
    predicate: str
    object: str               # 개념 ref 또는 "sec:SECTION"
    evidence: tuple[str, ...]  # "SECTION#span-name" 목록 (supports)
    evidence_kind: str
    empirical_status: str
    explanation: str
    conditions: dict[str, Any] = field(default_factory=dict)


class AnchorError(ValueError):
    pass


def resolve_span(text: str, ref: SpanRef) -> tuple[int, int, str]:
    """원문 텍스트에서 span의 (char_start, char_end, excerpt)를 찾는다. 못 찾으면 AnchorError."""
    index, cursor = -1, 0
    for _ in range(ref.nth):
        index = text.find(ref.start, cursor)
        if index < 0:
            raise AnchorError(f"p.{ref.page}: 시작 문자열을 찾지 못함 {ref.start[:60]!r} (nth={ref.nth})")
        cursor = index + 1
    if ref.end is None:
        end = index + len(ref.start)
    elif ref.end == "$":
        newline = text.find("\n", index)
        end = len(text) if newline < 0 else newline
    else:
        found = text.find(ref.end, index + len(ref.start))
        if found < 0:
            raise AnchorError(f"p.{ref.page}: 끝 문자열을 찾지 못함 {ref.end[:60]!r} (시작 {ref.start[:40]!r})")
        end = found + len(ref.end)
    excerpt = text[index:end]
    if not excerpt.strip():
        raise AnchorError(f"p.{ref.page}: 빈 span {ref.start[:40]!r}")
    if len(excerpt) > 2500:
        raise AnchorError(f"p.{ref.page}: span이 너무 김 ({len(excerpt)}자) {ref.start[:40]!r}")
    return index, end, excerpt
