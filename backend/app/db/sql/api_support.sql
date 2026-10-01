-- RSW 연동 API 보조 마이그레이션 (API_SPEC.md 10.2 ~ 10.4 및 인증 토큰).
-- rsw_schema.sql 적용 후 실행한다. 기존 43개 테이블 정의는 변경하지 않는다.
SET search_path = rsw, public;

-- 10.2 내구성 있는 처리 작업. normalize_alert는 alert만, generate_answer는 run만 설정한다.
CREATE TABLE background_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('normalize_alert','generate_answer')),
    alert_id uuid REFERENCES alert_events(id),
    run_id uuid,
    release_id uuid NOT NULL REFERENCES knowledge_releases(id),
    config object_json NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    result_context_id uuid,
    error object_json,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    -- 작업이 고정한 release와 결과/실행의 release가 같아야 한다.
    FOREIGN KEY (run_id, release_id) REFERENCES assistant_runs(id, release_id),
    FOREIGN KEY (result_context_id, release_id) REFERENCES alert_contexts(id, release_id),
    CHECK ((kind = 'normalize_alert' AND alert_id IS NOT NULL AND run_id IS NULL)
        OR (kind = 'generate_answer' AND run_id IS NOT NULL AND alert_id IS NULL)),
    CHECK (result_context_id IS NULL OR kind = 'normalize_alert'),
    CHECK (status <> 'running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (status NOT IN ('succeeded','failed') OR finished_at IS NOT NULL),
    CHECK (status <> 'failed' OR error IS NOT NULL),
    CHECK (kind <> 'normalize_alert' OR status <> 'succeeded' OR result_context_id IS NOT NULL)
);
CREATE UNIQUE INDEX background_jobs_one_per_run ON background_jobs (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX background_jobs_ready ON background_jobs (available_at, created_at) WHERE status IN ('queued','running');
CREATE INDEX background_jobs_alert_latest ON background_jobs (alert_id, created_at DESC, id DESC) WHERE alert_id IS NOT NULL;
CREATE INDEX background_jobs_result_context ON background_jobs (result_context_id) WHERE result_context_id IS NOT NULL;

-- 완료된 작업은 변경·삭제하지 않는다. 대상/설정/고정 release도 바꿀 수 없다.
CREATE FUNCTION guard_background_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Background jobs cannot be deleted'; END IF;
    IF OLD.status IN ('succeeded','failed') THEN RAISE EXCEPTION 'Finished background job is immutable'; END IF;
    IF (NEW.id, NEW.kind, NEW.alert_id, NEW.run_id, NEW.release_id, NEW.config, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.kind, OLD.alert_id, OLD.run_id, OLD.release_id, OLD.config, OLD.created_at) THEN
        RAISE EXCEPTION 'Background job target and configuration are immutable';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER background_job_guard BEFORE UPDATE OR DELETE ON background_jobs
FOR EACH ROW EXECUTE FUNCTION guard_background_job();

-- 인증 토큰. 토큰 원문은 저장하지 않고 SHA-256만 저장한다.
-- producer_id가 있는 자격 증명만 해당 producer_key로 경보를 등록할 수 있다.
CREATE TABLE api_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id uuid NOT NULL REFERENCES actors(id),
    token_sha256 sha256_hex NOT NULL UNIQUE,
    producer_id uuid REFERENCES event_producers(id),
    label text NOT NULL CHECK (length(label) > 0),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE INDEX api_credentials_actor ON api_credentials (actor_id);

-- 10.3 질문·피드백 멱등 기록. (주체, 메서드, 정규화 경로, 키)가 유일하다.
CREATE TABLE api_idempotency_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id uuid NOT NULL REFERENCES actors(id),
    method text NOT NULL CHECK (method = 'POST'),
    resource_path text NOT NULL CHECK (length(resource_path) > 0),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    request_body object_json NOT NULL,
    request_sha256 sha256_hex NOT NULL,
    response_status integer NOT NULL CHECK (response_status BETWEEN 200 AND 299),
    response_body object_json NOT NULL,
    run_id uuid REFERENCES assistant_runs(id),
    feedback_id uuid REFERENCES feedback(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    UNIQUE (principal_id, method, resource_path, idempotency_key),
    CHECK ((run_id IS NULL) <> (feedback_id IS NULL)),
    CHECK (expires_at >= created_at + interval '24 hours')
);
CREATE INDEX api_idempotency_expiry ON api_idempotency_requests (expires_at);

-- 10.4 한 피드백은 최대 한 번만 정정된다.
CREATE UNIQUE INDEX feedback_one_successor ON feedback (supersedes_id) WHERE supersedes_id IS NOT NULL;

-- 조회 보조 인덱스
CREATE INDEX feedback_run_lookup ON feedback (run_id, created_at);
CREATE INDEX answer_citations_run_lookup ON answer_citations (run_id);
CREATE INDEX retrieval_candidates_run_lookup ON retrieval_candidates (run_id, method, rank);
