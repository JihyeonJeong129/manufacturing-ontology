"""질의·검색 청크 임베딩.

기본 구성은 로컬 ONNX 모델 intfloat/multilingual-e5-small(384차원, 코사인)이다. 모델 파일은 이미지를
만들 때 고정 리비전으로 EMBEDDING_MODEL_DIR에 내려받는다(python -m app.cli embedding-download).
run에는 embedding_models 행 하나가 고정되고, 같은 행으로 만든 chunk_embeddings만 조회한다.
다른 모델(외부 API 등)을 붙일 때는 embedding_models 행을 따로 만들고 register_embedder()로 연결한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.sqlutil import fetch_one


class QueryEmbedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


_REGISTRY: dict[UUID, QueryEmbedder] = {}


def register_embedder(model_id: UUID, embedder: QueryEmbedder) -> None:
    _REGISTRY[model_id] = embedder


def unregister_embedder(model_id: UUID) -> None:
    _REGISTRY.pop(model_id, None)


def get_embedder(model_id: UUID | None) -> QueryEmbedder | None:
    return _REGISTRY.get(model_id) if model_id else None


# ---------------------------------------------------------------- 로컬 모델 명세


@dataclass(frozen=True)
class LocalModelSpec:
    provider: str
    model_name: str
    revision: str
    dimensions: int
    distance_metric: str
    preprocessing_version: str
    query_prefix: str
    passage_prefix: str
    pooling: str
    onnx_file: str
    files: tuple[str, ...]

    def config(self) -> dict[str, Any]:
        return {
            "revision": self.revision, "pooling": self.pooling, "normalize": True, "onnx_file": self.onnx_file,
            "query_prefix": self.query_prefix, "passage_prefix": self.passage_prefix,
            "passage_text": "heading_path + '\\n' + search_terms + '\\n' + content",
        }

    def config_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.config(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _load_spec() -> LocalModelSpec:
    raw = json.loads((Path(__file__).with_name("local_model.json")).read_text(encoding="utf-8"))
    return LocalModelSpec(**{**raw, "files": tuple(raw["files"])})


# 모델 명세는 local_model.json 하나만 기준으로 한다 (Dockerfile의 모델 다운로드 단계도 같은 파일을 읽는다).
E5_SMALL = _load_spec()
LOCAL_SPEC = E5_SMALL


def model_available(model_dir: Path, spec: LocalModelSpec = LOCAL_SPEC) -> bool:
    return all((model_dir / name).is_file() for name in spec.files)


def download_model(model_dir: Path, spec: LocalModelSpec = LOCAL_SPEC) -> Path:
    """Hugging Face에서 고정 리비전의 필요한 파일만 내려받는다 (이미지 빌드 단계에서 사용)."""
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=spec.model_name, revision=spec.revision, allow_patterns=list(spec.files),
                      local_dir=str(model_dir))
    if not model_available(model_dir, spec):
        raise RuntimeError(f"임베딩 모델 파일이 완전하지 않습니다: {model_dir}")
    return model_dir


class LocalOnnxEmbedder:
    """fastembed(ONNX Runtime)로 e5 계열 모델을 실행한다. 모델은 처음 쓸 때 한 번만 읽는다."""

    def __init__(self, model_dir: Path, spec: LocalModelSpec = LOCAL_SPEC) -> None:
        self.model_dir = model_dir
        self.spec = spec
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                from fastembed.common.model_description import ModelSource, PoolingType

                try:
                    TextEmbedding.add_custom_model(
                        model=self.spec.model_name, pooling=PoolingType.MEAN, normalization=True,
                        sources=ModelSource(hf=self.spec.model_name), dim=self.spec.dimensions,
                        model_file=self.spec.onnx_file)
                except ValueError:
                    pass  # 같은 프로세스에서 이미 등록됨
                self._model = TextEmbedding(self.spec.model_name, specific_model_path=str(self.model_dir))
            return self._model

    def embed_query_sync(self, text: str) -> list[float]:
        vector = next(iter(self._load().embed([self.spec.query_prefix + text])))
        return [float(v) for v in vector]

    def embed_passages(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        model = self._load()
        return [[float(v) for v in vector]
                for vector in model.embed([self.spec.passage_prefix + t for t in texts], batch_size=batch_size)]

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_query_sync, text)

    def warm_up(self) -> None:
        self.embed_query_sync("warm up")


_LOCAL: dict[str, LocalOnnxEmbedder] = {}


def local_embedder(model_dir: Path) -> LocalOnnxEmbedder | None:
    """모델 파일이 있으면 프로세스 안에서 공유하는 로컬 임베더를 돌려준다."""
    if not model_available(model_dir):
        return None
    key = str(model_dir.resolve())
    if key not in _LOCAL:
        _LOCAL[key] = LocalOnnxEmbedder(model_dir)
    return _LOCAL[key]


# ---------------------------------------------------------------- DB 연결


async def ensure_model_row(session: AsyncSession, spec: LocalModelSpec = LOCAL_SPEC) -> UUID:
    await session.execute(*_sql(
        """
        INSERT INTO rsw.embedding_models (provider, model_name, model_version, dimensions, distance_metric,
                                          preprocessing_version, config, config_sha256)
        VALUES (:provider, :name, :version, :dims, :metric, :prep, CAST(:config AS jsonb), :sha)
        ON CONFLICT (provider, model_name, model_version, config_sha256) DO NOTHING
        """,
        provider=spec.provider, name=spec.model_name, version=spec.revision, dims=spec.dimensions,
        metric=spec.distance_metric, prep=spec.preprocessing_version,
        config=json.dumps(spec.config(), ensure_ascii=False), sha=spec.config_sha256(),
    ))
    model_id = await find_model_row(session, spec)
    assert model_id is not None
    return model_id


async def find_model_row(session: AsyncSession, spec: LocalModelSpec = LOCAL_SPEC) -> UUID | None:
    row = await fetch_one(
        session,
        """
        SELECT id FROM rsw.embedding_models
        WHERE provider = :provider AND model_name = :name AND model_version = :version AND config_sha256 = :sha
        """,
        provider=spec.provider, name=spec.model_name, version=spec.revision, sha=spec.config_sha256(),
    )
    return row["id"] if row else None


async def resolve_run_model_id(session: AsyncSession, cfg: Settings) -> UUID | None:
    """질문 실행에 고정할 임베딩 모델. 설정값이 있으면 그 값, 없으면 로컬 모델의 DB 행."""
    if cfg.EMBEDDING_MODEL_ID is not None:
        return cfg.EMBEDDING_MODEL_ID
    if cfg.EMBEDDING_BACKEND != "local":
        return None
    return await find_model_row(session)


async def resolve_embedder(session: AsyncSession, model_id: UUID | None, cfg: Settings) -> QueryEmbedder | None:
    """run에 고정된 모델과 정확히 같은 질의 임베더만 돌려준다. 없으면 None(벡터 검색 생략)."""
    registered = get_embedder(model_id)
    if registered is not None or model_id is None:
        return registered
    if cfg.EMBEDDING_BACKEND != "local":
        return None
    local_id = await find_model_row(session)
    if local_id != model_id:
        return None
    return local_embedder(cfg.EMBEDDING_MODEL_DIR)


def _sql(sql: str, **params: Any) -> tuple[Any, dict[str, Any]]:
    from sqlalchemy import text

    return text(sql), params
