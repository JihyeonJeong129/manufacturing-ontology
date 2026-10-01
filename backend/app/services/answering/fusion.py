"""검색 방식별 후보 순위 결합 (Reciprocal Rank Fusion). 점수는 방식 사이에 직접 비교하지 않는다."""
from __future__ import annotations

from typing import Any
from uuid import UUID

RRF_K = 60
# 정확한 코드/그래프 근거를 키워드·벡터 확장보다 우선한다.
METHOD_WEIGHT = {"code": 2.0, "graph": 1.5, "keyword": 1.0, "vector": 1.0}


def fuse(ranked: dict[str, list[UUID]], excluded: set[UUID]) -> list[tuple[UUID, float]]:
    scores: dict[UUID, float] = {}
    for method, chunk_ids in ranked.items():
        weight = METHOD_WEIGHT.get(method, 1.0)
        for rank, chunk_id in enumerate(chunk_ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
    ordered = sorted(((cid, s) for cid, s in scores.items() if cid not in excluded), key=lambda x: (-x[1], str(x[0])))
    return ordered


def keyword_query(texts: list[str]) -> str | None:
    """to_tsquery('simple', ...)에 넣을 OR 질의. 단어 문자만 사용하므로 주입 위험이 없다.

    'simple' 구성은 한국어 형태소 분석을 하지 않으므로 2자 이상 토큰은 접두 일치(:*)로 확장한다.
    """
    import re

    tokens: list[str] = []
    for text in texts:
        for token in re.findall(r"\w+", text.lower()):
            if len(token) < 2 or token in tokens:
                continue
            tokens.append(token)
    if not tokens:
        return None
    return " | ".join(f"{t}:*" for t in tokens[:32])


def as_rows(run_id: UUID, release_id: UUID, method: str, ranked: list[tuple[UUID, float | None]],
            assessments: dict[UUID, dict[str, Any]], selected: set[UUID] | None = None) -> list[dict[str, Any]]:
    rows = []
    for rank, (chunk_id, score) in enumerate(ranked, start=1):
        rows.append({
            "run_id": run_id, "release_id": release_id, "chunk_id": chunk_id, "method": method,
            "rank": rank, "score": score, "selected": bool(selected and chunk_id in selected),
            "applicability_result": assessments[chunk_id],
        })
    return rows
