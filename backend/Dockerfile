# ---------- Builder: 의존성 빌드 (컴파일 필요한 라이브러리 대비) ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 로컬 임베딩 모델(intfloat/multilingual-e5-small, ONNX)을 고정 리비전으로 내려받는다.
# 명세 파일이 바뀔 때만 이 층을 다시 만든다 (앱 코드 변경과 무관).
COPY app/services/answering/local_model.json /build/local_model.json
COPY scripts/download_embedding_model.py /build/download_embedding_model.py
RUN python /build/download_embedding_model.py /build/local_model.json /opt/models/multilingual-e5-small

# ---------- Runtime: 실행에 필요한 것만 ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    STORAGE_ROOT=/data/storage \
    EMBEDDING_MODEL_DIR=/opt/models/multilingual-e5-small \
    HF_HUB_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/storage/documents /data/storage/graphs /data/raw \
    && chown -R appuser:appuser /data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

WORKDIR /app
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser app ./app

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------- Test: 런타임 이미지 + 개발 의존성 + 테스트 코드 ----------
# docker compose --profile test run --rm tests
FROM runtime AS test

USER root
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=appuser:appuser pytest.ini ./pytest.ini
COPY --chown=appuser:appuser tests ./tests
USER appuser

HEALTHCHECK NONE
CMD ["pytest", "-q"]
