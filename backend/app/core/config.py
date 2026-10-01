from functools import lru_cache
from pathlib import Path
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수(.env)에서 설정을 로드한다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "fastapi-app"
    APP_ENV: str = "development"
    DEBUG: bool = False

    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432
    DB_ECHO: bool = False

    # ----- RSW 지식/처리 계약 버전 -----
    # 활성 지식 배포본 포인터(knowledge_deployments.name)
    KNOWLEDGE_DEPLOYMENT: str = "pilot"
    NORMALIZER_VERSION: str = "rsw-normalizer-v1"
    MATCHER_VERSION: str = "rsw-matcher-v1"
    RETRIEVER_VERSION: str = "rsw-retriever-v1"
    PROMPT_VERSION: str = "rsw-grounded-template-v1"
    ANSWER_MODEL_PROVIDER: str = "builtin"
    ANSWER_MODEL_NAME: str = "grounded-template-v1"
    VALIDATION_CONTRACT_VERSION: str = "rsw-conditions-v1"
    # 질문 실행에 고정할 임베딩 모델(embedding_models.id). 비워 두면 EMBEDDING_BACKEND에 따라 정한다.
    EMBEDDING_MODEL_ID: UUID | None = None
    # local: 이미지에 포함된 intfloat/multilingual-e5-small(ONNX)로 벡터 검색. none: 벡터 검색 안 함
    EMBEDDING_BACKEND: str = "local"
    EMBEDDING_MODEL_DIR: Path = Path("/opt/models/multilingual-e5-small")
    # 질문과 가장 가까운 원문 절이 한 시나리오의 설명 절이면 그 시나리오를 '질문 기반 후보'로 연결한다.
    QUESTION_SCENARIO_LINKING: bool = True

    # ----- 파일 저장소 (원본 PDF, RDF 스냅샷) -----
    STORAGE_ROOT: Path = Path("/data/storage")

    # ----- 요청 제한 -----
    ALERT_MAX_BYTES: int = 1024 * 1024
    QUESTION_MAX_BYTES: int = 64 * 1024
    FEEDBACK_MAX_BYTES: int = 64 * 1024
    IDEMPOTENCY_TTL_HOURS: int = 24
    IDEMPOTENCY_WAIT_SECONDS: float = 2.0
    RETRY_AFTER_SECONDS: int = 2
    # 이 값보다 미래 시각의 경보/관측은 도메인 모순(invalid)으로 판정
    CLOCK_SKEW_SECONDS: int = 300
    CONVERSATION_MAX_DEPTH: int = 20

    # ----- 워커 -----
    WORKER_POLL_INTERVAL_SECONDS: float = 1.0
    JOB_LEASE_SECONDS: int = 60
    JOB_MAX_ATTEMPTS: int = 3
    JOB_RETRY_BASE_SECONDS: float = 2.0

    # ----- 답변 생성 -----
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_CANDIDATE_LIMIT: int = 20

    # ----- RSW MVP 실제 지식 적재 (python -m app.cli load-mvp) -----
    RAW_DATA_DIR: Path = Path("/data/raw")
    MVP_ML_TOKEN: str = "rsw-ml-token-change-me"
    MVP_OPERATOR_TOKEN: str = "rsw-operator-token-change-me"
    MVP_REVIEWER_TOKEN: str = "rsw-reviewer-token-change-me"

    # ----- 데모 시드 전용 토큰 -----
    DEMO_ML_TOKEN: str = "demo-ml-token-change-me"
    DEMO_OPERATOR_TOKEN: str = "demo-operator-token-change-me"
    DEMO_REVIEWER_TOKEN: str = "demo-reviewer-token-change-me"

    @property
    def DATABASE_URL(self) -> str:
        return self.database_url_for(self.POSTGRES_DB)

    def database_url_for(self, database: str) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{database}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
