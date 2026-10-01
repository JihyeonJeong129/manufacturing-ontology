-- RSW maintenance MVP: PostgreSQL 18 + pgvector 0.8+.
-- Initial migration for an EMPTY rsw schema. No equipment facts are seeded.
-- Install pgvector on the server before executing this file.
-- Runtime roles, authentication and the publication service are separate concerns.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA rsw;
SET search_path = rsw, public;

CREATE DOMAIN sha256_hex AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN object_json AS jsonb CHECK (jsonb_typeof(VALUE) = 'object');
CREATE DOMAIN array_json AS jsonb CHECK (jsonb_typeof(VALUE) = 'array');

-- 1. Identity, stable ontology IDs and physical equipment.
CREATE TABLE actors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_key text NOT NULL UNIQUE,
    display_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('developer','domain_reviewer','operator','service')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_entities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('component_type','scenario','fault_type','alarm_code','diagnostic_rule','hypothesis','evidence_pattern','feature','document_section','procedure','numeric_spec')),
    namespace text NOT NULL CHECK (length(namespace) > 0),
    local_code text NOT NULL CHECK (length(local_code) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, namespace, local_code),
    UNIQUE (id, kind)
);

CREATE TABLE assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_code text NOT NULL UNIQUE,
    display_name text NOT NULL,
    location text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE asset_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id uuid NOT NULL REFERENCES assets(id),
    revision integer NOT NULL CHECK (revision > 0),
    manufacturer text,
    model text,
    controller_manufacturer text,
    controller_model text,
    controller_firmware text,
    drive_type text NOT NULL CHECK (drive_type IN ('servo_pneumatic','pneumatic','servo_electric','unknown')),
    components object_json NOT NULL DEFAULT '{}'::jsonb,
    verification_status text NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified')),
    verified_by uuid REFERENCES actors(id),
    verified_at timestamptz,
    verification_basis text,
    effective_from timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asset_id, revision),
    UNIQUE (id, asset_id),
    CHECK (verification_status <> 'verified' OR (manufacturer IS NOT NULL AND model IS NOT NULL AND controller_model IS NOT NULL AND verified_by IS NOT NULL AND verified_at IS NOT NULL AND verification_basis IS NOT NULL AND drive_type <> 'unknown'))
);

-- 2. Immutable source/extraction revisions and precise page anchors.
CREATE TABLE document_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_key text NOT NULL,
    revision_key text NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('manual','paper','team_analysis','expert_note','site_procedure','maintenance_record')),
    title text NOT NULL,
    publisher text,
    edition_label text,
    language text NOT NULL,
    source_uri text NOT NULL,
    source_sha256 sha256_hex NOT NULL,
    extraction_version text NOT NULL,
    page_count integer CHECK (page_count > 0),
    sealed_at timestamptz,
    created_by uuid REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_key, revision_key, extraction_version)
);

CREATE TABLE document_pages (
    document_revision_id uuid NOT NULL REFERENCES document_revisions(id),
    page_number integer NOT NULL CHECK (page_number > 0),
    locator_kind text NOT NULL DEFAULT 'pdf_page' CHECK (locator_kind IN ('pdf_page','logical_block')),
    printed_page_label text,
    page_text text NOT NULL,
    text_sha256 sha256_hex NOT NULL,
    extraction_method text NOT NULL CHECK (extraction_method IN ('native','ocr','manual','html')),
    PRIMARY KEY (document_revision_id, page_number)
);

CREATE TABLE document_sections (
    id uuid PRIMARY KEY,
    entity_kind text NOT NULL DEFAULT 'document_section' CHECK (entity_kind = 'document_section'),
    document_revision_id uuid NOT NULL REFERENCES document_revisions(id),
    section_key text NOT NULL,
    section_number text,
    title text NOT NULL,
    original_text text NOT NULL,
    text_sha256 sha256_hex NOT NULL,
    FOREIGN KEY (id, entity_kind) REFERENCES knowledge_entities(id, kind),
    UNIQUE (document_revision_id, section_key),
    UNIQUE (id, document_revision_id)
);

CREATE TABLE section_spans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id uuid NOT NULL,
    document_revision_id uuid NOT NULL,
    page_number integer NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    excerpt text NOT NULL,
    char_start integer CHECK (char_start >= 0),
    char_end integer,
    bbox array_json,
    FOREIGN KEY (section_id, document_revision_id) REFERENCES document_sections(id, document_revision_id),
    FOREIGN KEY (document_revision_id, page_number) REFERENCES document_pages(document_revision_id, page_number),
    UNIQUE (section_id, ordinal),
    UNIQUE (id, section_id),
    CHECK ((char_start IS NULL AND char_end IS NULL) OR (char_start IS NOT NULL AND char_end IS NOT NULL AND char_end > char_start))
);

-- 3. Knowledge releases. Curated tables below are scoped by release_id.
-- draft -> frozen -> published. Frozen data is never reopened; clone to a new draft.
CREATE TABLE knowledge_releases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version text NOT NULL UNIQUE,
    parent_release_id uuid REFERENCES knowledge_releases(id),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','frozen','published')),
    ontology_version text NOT NULL,
    ontology_sha256 sha256_hex NOT NULL,
    shapes_version text NOT NULL,
    shapes_sha256 sha256_hex NOT NULL,
    builder_version text NOT NULL,
    validation_contract_version text NOT NULL,
    domain_reviewed_by uuid REFERENCES actors(id),
    domain_reviewed_at timestamptz,
    frozen_at timestamptz,
    graph_uri text,
    graph_sha256 sha256_hex,
    validation_report object_json,
    shacl_conforms boolean,
    publish_checks_passed boolean,
    published_at timestamptz,
    created_by uuid NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, status),
    CHECK (parent_release_id IS DISTINCT FROM id),
    CHECK ((domain_reviewed_by IS NULL) = (domain_reviewed_at IS NULL)),
    CHECK (status = 'draft' OR (frozen_at IS NOT NULL AND domain_reviewed_by IS NOT NULL)),
    CHECK (status <> 'published' OR (published_at IS NOT NULL AND graph_uri IS NOT NULL AND graph_sha256 IS NOT NULL AND validation_report IS NOT NULL AND shacl_conforms IS TRUE AND publish_checks_passed IS TRUE))
);

CREATE TABLE entity_versions (
    release_id uuid NOT NULL REFERENCES knowledge_releases(id),
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL,
    label_ko text NOT NULL,
    label_en text,
    description text NOT NULL,
    metadata object_json NOT NULL DEFAULT '{}'::jsonb,
    review_status text NOT NULL DEFAULT 'draft' CHECK (review_status IN ('draft','reviewed','rejected')),
    PRIMARY KEY (release_id, entity_id),
    UNIQUE (release_id, entity_id, entity_kind),
    FOREIGN KEY (entity_id, entity_kind) REFERENCES knowledge_entities(id, kind)
);

CREATE TABLE entity_aliases (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    language text NOT NULL,
    alias text NOT NULL,
    PRIMARY KEY (release_id, entity_id, language, alias),
    FOREIGN KEY (release_id, entity_id) REFERENCES entity_versions(release_id, entity_id)
);

CREATE TABLE scenarios (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL DEFAULT 'scenario' CHECK (entity_kind = 'scenario'),
    scenario_kind text NOT NULL CHECK (scenario_kind IN ('fault_response','cause_exploration','missing_context','operational_context')),
    question_examples array_json NOT NULL DEFAULT '[]'::jsonb,
    required_context array_json NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (release_id, entity_id),
    FOREIGN KEY (release_id, entity_id, entity_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind)
);

CREATE TABLE feature_definitions (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL DEFAULT 'feature' CHECK (entity_kind = 'feature'),
    feature_kind text NOT NULL CHECK (feature_kind IN ('measurement','setpoint','state','counter','derived','unknown')),
    value_type text NOT NULL CHECK (value_type IN ('number','string','boolean','unknown')),
    unit text,
    unit_status text NOT NULL DEFAULT 'unknown' CHECK (unit_status IN ('unknown','verified','not_applicable')),
    definition_status text NOT NULL DEFAULT 'unverified' CHECK (definition_status IN ('unverified','source_checked','domain_validated')),
    PRIMARY KEY (release_id, entity_id),
    FOREIGN KEY (release_id, entity_id, entity_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    CHECK (unit_status <> 'verified' OR unit IS NOT NULL)
);

CREATE TABLE feature_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    asset_config_id uuid NOT NULL REFERENCES asset_configs(id),
    source_tag text NOT NULL,
    feature_id uuid NOT NULL,
    source_unit text,
    conversion object_json NOT NULL DEFAULT '{}'::jsonb,
    verification_status text NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified')),
    verification_basis text,
    FOREIGN KEY (release_id, feature_id) REFERENCES feature_definitions(release_id, entity_id),
    UNIQUE (release_id, asset_config_id, source_tag),
    UNIQUE (id, release_id, asset_config_id),
    CHECK (verification_status <> 'verified' OR verification_basis IS NOT NULL)
);

CREATE TABLE evidence_patterns (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL DEFAULT 'evidence_pattern' CHECK (entity_kind = 'evidence_pattern'),
    pattern_kind text NOT NULL CHECK (pattern_kind IN ('sensor','setpoint_change','error_history','external_context')),
    conditions object_json NOT NULL,
    baseline_definition object_json,
    window_definition object_json,
    empirical_status text NOT NULL DEFAULT 'hypothesis' CHECK (empirical_status IN ('hypothesis','tested','supported','refuted')),
    PRIMARY KEY (release_id, entity_id),
    FOREIGN KEY (release_id, entity_id, entity_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind)
);

CREATE TABLE procedures (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL DEFAULT 'procedure' CHECK (entity_kind = 'procedure'),
    procedure_kind text NOT NULL CHECK (procedure_kind IN ('inspection','maintenance','information_gathering','escalation')),
    preconditions array_json NOT NULL,
    required_role text NOT NULL,
    source_section_id uuid NOT NULL REFERENCES document_sections(id),
    PRIMARY KEY (release_id, entity_id),
    FOREIGN KEY (release_id, entity_id, entity_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    FOREIGN KEY (release_id, source_section_id) REFERENCES entity_versions(release_id, entity_id)
);

CREATE TABLE procedure_steps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    procedure_id uuid NOT NULL,
    step_number integer NOT NULL CHECK (step_number > 0),
    instruction_ko text NOT NULL,
    expected_result text,
    branch_condition object_json,
    source_section_id uuid NOT NULL REFERENCES document_sections(id),
    source_span_id uuid,
    FOREIGN KEY (release_id, procedure_id) REFERENCES procedures(release_id, entity_id),
    FOREIGN KEY (release_id, source_section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (source_span_id, source_section_id) REFERENCES section_spans(id, section_id),
    UNIQUE (release_id, procedure_id, step_number)
);

CREATE TABLE numeric_specs (
    release_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    entity_kind text NOT NULL DEFAULT 'numeric_spec' CHECK (entity_kind = 'numeric_spec'),
    quantity text NOT NULL,
    comparison text NOT NULL CHECK (comparison IN ('eq','lt','le','gt','ge','range','unresolved')),
    value_low numeric,
    value_high numeric,
    original_value_text text NOT NULL,
    unit text,
    spec_kind text NOT NULL CHECK (spec_kind IN ('paper_definition','equipment_setting','maintenance_limit','recommendation')),
    conditions object_json NOT NULL,
    interpretation_status text NOT NULL DEFAULT 'unverified' CHECK (interpretation_status IN ('unverified','verified')),
    source_section_id uuid NOT NULL REFERENCES document_sections(id),
    source_span_id uuid,
    PRIMARY KEY (release_id, entity_id),
    FOREIGN KEY (release_id, entity_id, entity_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    FOREIGN KEY (release_id, source_section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (source_span_id, source_section_id) REFERENCES section_spans(id, section_id),
    CHECK (comparison <> 'range' OR (value_low IS NOT NULL AND value_high IS NOT NULL AND value_low <= value_high)),
    CHECK (comparison IN ('range','unresolved') OR (value_low IS NOT NULL AND value_high IS NULL)),
    CHECK (value_low IS NULL OR value_low NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    CHECK (value_high IS NULL OR value_high NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    CHECK (interpretation_status <> 'verified' OR (unit IS NOT NULL AND comparison <> 'unresolved'))
);

-- Allowed endpoint types are a versioned application/ontology contract.
CREATE TABLE relation_rules (
    predicate text NOT NULL,
    subject_kind text NOT NULL,
    object_kind text NOT NULL,
    description text NOT NULL,
    PRIMARY KEY (predicate, subject_kind, object_kind)
);
INSERT INTO relation_rules VALUES
 ('related_fault','scenario','fault_type','Scenario concerns a fault classification'),
 ('raises_hypothesis','scenario','hypothesis','Scenario raises a candidate, not a confirmed cause'),
 ('has_pattern','hypothesis','evidence_pattern','Candidate has an observation pattern'),
 ('suggests','evidence_pattern','hypothesis','Pattern may suggest a candidate'),
 ('may_contribute_to','hypothesis','fault_type','Conditional, non-certain causal candidate'),
 ('maps_to','alarm_code','fault_type','Source-specific reviewed mapping; not owl:sameAs'),
 ('triggers','diagnostic_rule','scenario','Rule may trigger a response scenario'),
 ('has_procedure','scenario','procedure','Scenario has a candidate procedure'),
 ('checks_hypothesis','procedure','hypothesis','Procedure checks a candidate'),
 ('uses_spec','procedure','numeric_spec','Procedure uses a conditional numeric specification'),
 ('concerns_component','hypothesis','component_type','Candidate concerns a component type'),
 ('concerns_component','procedure','component_type','Procedure concerns a component type'),
 ('documented_in','alarm_code','document_section','Source documents code meaning'),
 ('documented_in','fault_type','document_section','Source documents a fault definition'),
 ('documented_in','hypothesis','document_section','Source discusses a candidate'),
 ('documented_in','scenario','document_section','Source is relevant to a scenario'),
 ('documented_in','procedure','document_section','Source documents procedure'),
 ('documented_in','numeric_spec','document_section','Source documents a numeric value');

CREATE TABLE knowledge_relations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    subject_id uuid NOT NULL,
    subject_kind text NOT NULL,
    predicate text NOT NULL,
    object_id uuid NOT NULL,
    object_kind text NOT NULL,
    scope_asset_config_id uuid REFERENCES asset_configs(id),
    conditions object_json NOT NULL DEFAULT '{}'::jsonb,
    evidence_kind text NOT NULL CHECK (evidence_kind IN ('manual','paper','team_analysis','expert','mixed','inference')),
    empirical_status text NOT NULL DEFAULT 'hypothesis' CHECK (empirical_status IN ('hypothesis','source_statement','tested','supported','refuted')),
    review_status text NOT NULL DEFAULT 'draft' CHECK (review_status IN ('draft','reviewed','rejected')),
    FOREIGN KEY (release_id, subject_id, subject_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    FOREIGN KEY (release_id, object_id, object_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    FOREIGN KEY (predicate, subject_kind, object_kind) REFERENCES relation_rules(predicate, subject_kind, object_kind),
    UNIQUE (release_id, id)
);

CREATE TABLE relation_evidence (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    relation_id uuid NOT NULL,
    section_id uuid NOT NULL REFERENCES document_sections(id),
    span_id uuid,
    evidence_role text NOT NULL CHECK (evidence_role IN ('supports','refutes','context')),
    explanation text NOT NULL,
    FOREIGN KEY (release_id, relation_id) REFERENCES knowledge_relations(release_id, id),
    FOREIGN KEY (release_id, section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (span_id, section_id) REFERENCES section_spans(id, section_id)
);

CREATE TABLE applicability (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    content_entity_id uuid NOT NULL,
    content_kind text NOT NULL CHECK (content_kind IN ('document_section','procedure','numeric_spec')),
    asset_config_id uuid NOT NULL REFERENCES asset_configs(id),
    component_scope text,
    reference_scope text NOT NULL DEFAULT 'unknown' CHECK (reference_scope IN ('applicable','analogous','comparison','unknown')),
    conditions object_json NOT NULL DEFAULT '{}'::jsonb,
    review_status text NOT NULL DEFAULT 'draft' CHECK (review_status IN ('draft','reviewed','rejected')),
    reviewed_by uuid REFERENCES actors(id),
    review_basis text,
    FOREIGN KEY (release_id, content_entity_id, content_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    UNIQUE (release_id, id),
    CHECK (review_status <> 'reviewed' OR (reviewed_by IS NOT NULL AND review_basis IS NOT NULL))
);

CREATE TABLE applicability_evidence (
    release_id uuid NOT NULL,
    applicability_id uuid NOT NULL,
    section_id uuid NOT NULL REFERENCES document_sections(id),
    PRIMARY KEY (release_id, applicability_id, section_id),
    FOREIGN KEY (release_id, applicability_id) REFERENCES applicability(release_id, id),
    FOREIGN KEY (release_id, section_id) REFERENCES entity_versions(release_id, entity_id)
);

CREATE TABLE knowledge_conflicts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id uuid NOT NULL,
    affected_entity_id uuid NOT NULL,
    left_section_id uuid NOT NULL REFERENCES document_sections(id),
    right_section_id uuid NOT NULL REFERENCES document_sections(id),
    description text NOT NULL,
    status text NOT NULL DEFAULT 'unresolved' CHECK (status IN ('unresolved','resolved')),
    resolution text,
    resolved_by uuid REFERENCES actors(id),
    FOREIGN KEY (release_id, affected_entity_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (release_id, left_section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (release_id, right_section_id) REFERENCES entity_versions(release_id, entity_id),
    CHECK (status <> 'resolved' OR (resolution IS NOT NULL AND resolved_by IS NOT NULL))
);

-- 4. Retrieval: immutable chunks and model-specific vectors; exact search initially.
CREATE TABLE retrieval_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id uuid NOT NULL,
    document_revision_id uuid NOT NULL,
    chunking_version text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    language text NOT NULL,
    content text NOT NULL,
    heading_path text NOT NULL,
    search_terms text NOT NULL DEFAULT '',
    content_sha256 sha256_hex NOT NULL,
    page_start integer NOT NULL,
    page_end integer NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, heading_path || ' ' || search_terms || ' ' || content)) STORED,
    FOREIGN KEY (section_id, document_revision_id) REFERENCES document_sections(id, document_revision_id),
    FOREIGN KEY (document_revision_id, page_start) REFERENCES document_pages(document_revision_id, page_number),
    FOREIGN KEY (document_revision_id, page_end) REFERENCES document_pages(document_revision_id, page_number),
    UNIQUE (section_id, chunking_version, ordinal),
    UNIQUE (id, section_id),
    CHECK (page_start <= page_end)
);

CREATE TABLE embedding_models (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    distance_metric text NOT NULL DEFAULT 'cosine' CHECK (distance_metric IN ('cosine','l2','inner_product')),
    preprocessing_version text NOT NULL,
    config object_json NOT NULL DEFAULT '{}'::jsonb,
    config_sha256 sha256_hex NOT NULL,
    UNIQUE (provider, model_name, model_version, config_sha256),
    UNIQUE (id, dimensions)
);

CREATE TABLE chunk_embeddings (
    chunk_id uuid NOT NULL REFERENCES retrieval_chunks(id),
    embedding_model_id uuid NOT NULL,
    dimensions integer NOT NULL,
    embedding public.vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, embedding_model_id),
    FOREIGN KEY (embedding_model_id, dimensions) REFERENCES embedding_models(id, dimensions),
    CHECK (public.vector_dims(embedding) = dimensions)
);

CREATE TABLE release_chunks (
    release_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    section_id uuid NOT NULL,
    PRIMARY KEY (release_id, chunk_id),
    FOREIGN KEY (release_id, section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (chunk_id, section_id) REFERENCES retrieval_chunks(id, section_id)
);

CREATE TABLE knowledge_deployments (
    name text PRIMARY KEY,
    release_id uuid,
    release_status text NOT NULL DEFAULT 'published' CHECK (release_status = 'published'),
    activated_by uuid NOT NULL REFERENCES actors(id),
    activated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (release_id, release_status) REFERENCES knowledge_releases(id, status)
);

-- 5. Incoming ML events are append-only; normalization is a separate versioned row.
CREATE TABLE event_producers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    producer_key text NOT NULL UNIQUE,
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    producer_id uuid NOT NULL REFERENCES event_producers(id),
    source_event_id text NOT NULL,
    reported_asset_key text NOT NULL,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    producer_schema_version text NOT NULL,
    raw_payload bytea NOT NULL,
    parsed_payload object_json NOT NULL,
    payload_sha256 sha256_hex NOT NULL,
    UNIQUE (producer_id, source_event_id)
);

CREATE TABLE alert_contexts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id uuid NOT NULL REFERENCES alert_events(id),
    release_id uuid NOT NULL,
    release_status text NOT NULL DEFAULT 'published' CHECK (release_status = 'published'),
    asset_config_id uuid REFERENCES asset_configs(id),
    normalizer_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('mapped','partial','unmapped','invalid')),
    window_start timestamptz,
    window_end timestamptz,
    operating_context object_json NOT NULL DEFAULT '{}'::jsonb,
    warnings array_json NOT NULL DEFAULT '[]'::jsonb,
    sealed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (release_id, release_status) REFERENCES knowledge_releases(id, status),
    UNIQUE (id, release_id),
    UNIQUE (id, release_id, asset_config_id),
    CHECK ((window_start IS NULL AND window_end IS NULL) OR (window_start IS NOT NULL AND window_end IS NOT NULL AND window_start <= window_end)),
    CHECK (status <> 'mapped' OR asset_config_id IS NOT NULL)
);

CREATE TABLE observed_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_id uuid NOT NULL,
    release_id uuid NOT NULL,
    raw_namespace text NOT NULL,
    raw_code text NOT NULL,
    code_entity_id uuid,
    code_kind text NOT NULL DEFAULT 'alarm_code' CHECK (code_kind = 'alarm_code'),
    mapping_status text NOT NULL CHECK (mapping_status IN ('mapped','unmapped','ambiguous')),
    observed_at timestamptz,
    FOREIGN KEY (context_id, release_id) REFERENCES alert_contexts(id, release_id),
    FOREIGN KEY (release_id, code_entity_id, code_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    CHECK ((mapping_status = 'mapped' AND code_entity_id IS NOT NULL) OR (mapping_status <> 'mapped' AND code_entity_id IS NULL))
);

CREATE TABLE observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_id uuid NOT NULL,
    release_id uuid NOT NULL,
    asset_config_id uuid,
    binding_id uuid,
    raw_feature_key text NOT NULL,
    value jsonb,
    summary object_json,
    unit text,
    quality text NOT NULL CHECK (quality IN ('valid','missing','suspect','unknown')),
    baseline object_json,
    observed_at timestamptz,
    raw_series_ref text,
    FOREIGN KEY (context_id, release_id) REFERENCES alert_contexts(id, release_id),
    FOREIGN KEY (context_id, release_id, asset_config_id) REFERENCES alert_contexts(id, release_id, asset_config_id),
    FOREIGN KEY (binding_id, release_id, asset_config_id) REFERENCES feature_bindings(id, release_id, asset_config_id),
    CHECK (binding_id IS NULL OR asset_config_id IS NOT NULL),
    CHECK (quality <> 'valid' OR value IS NOT NULL OR summary IS NOT NULL)
);

-- One row per model/rule output item. Original payload remains in alert_events.
CREATE TABLE ml_assessments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_id uuid NOT NULL,
    release_id uuid NOT NULL,
    producer_run_key text,
    model_name text NOT NULL,
    model_version text NOT NULL,
    assessment_kind text NOT NULL CHECK (assessment_kind IN ('classification','hypothesis','rule_trigger','anomaly')),
    raw_target_namespace text,
    raw_target_code text,
    target_entity_id uuid,
    target_kind text CHECK (target_kind IN ('fault_type','hypothesis','diagnostic_rule')),
    score_kind text,
    score_value double precision,
    calibration_status text NOT NULL DEFAULT 'unknown' CHECK (calibration_status IN ('unknown','uncalibrated','calibrated')),
    calibration_ref text,
    details object_json NOT NULL DEFAULT '{}'::jsonb,
    caveats array_json NOT NULL DEFAULT '[]'::jsonb,
    FOREIGN KEY (context_id, release_id) REFERENCES alert_contexts(id, release_id),
    FOREIGN KEY (release_id, target_entity_id, target_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    CHECK ((target_entity_id IS NULL) = (target_kind IS NULL)),
    CHECK (target_entity_id IS NULL OR
      (assessment_kind = 'classification' AND target_kind = 'fault_type') OR
      (assessment_kind = 'hypothesis' AND target_kind = 'hypothesis') OR
      (assessment_kind = 'rule_trigger' AND target_kind = 'diagnostic_rule')),
    CHECK (score_value IS NULL OR score_kind IS NOT NULL),
    CHECK (score_value IS NULL OR score_value NOT IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)),
    CHECK (score_kind IS DISTINCT FROM 'probability' OR score_value IS NULL OR score_value BETWEEN 0 AND 1),
    CHECK (calibration_status <> 'calibrated' OR calibration_ref IS NOT NULL)
);

-- 6. Retrieval/answer traces, reviews and actual maintenance outcomes.
CREATE TABLE assistant_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_id uuid NOT NULL,
    release_id uuid NOT NULL,
    requested_by uuid REFERENCES actors(id),
    question text NOT NULL,
    model_provider text NOT NULL,
    model_name text NOT NULL,
    prompt_version text NOT NULL,
    retriever_version text NOT NULL,
    embedding_model_id uuid REFERENCES embedding_models(id),
    parameters object_json NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','answered','needs_context','insufficient_evidence','failed')),
    answer object_json,
    input_snapshot object_json,
    error object_json,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    FOREIGN KEY (context_id, release_id) REFERENCES alert_contexts(id, release_id),
    UNIQUE (id, release_id),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (status IN ('pending','running') OR finished_at IS NOT NULL),
    CHECK (status NOT IN ('answered','needs_context','insufficient_evidence') OR answer IS NOT NULL),
    CHECK (status <> 'failed' OR error IS NOT NULL)
);

CREATE TABLE retrieval_candidates (
    run_id uuid NOT NULL,
    release_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    method text NOT NULL CHECK (method IN ('code','graph','keyword','vector','fusion','rerank')),
    rank integer NOT NULL CHECK (rank > 0),
    score double precision,
    selected boolean NOT NULL DEFAULT false,
    applicability_result object_json NOT NULL,
    PRIMARY KEY (run_id, chunk_id, method),
    FOREIGN KEY (run_id, release_id) REFERENCES assistant_runs(id, release_id),
    FOREIGN KEY (release_id, chunk_id) REFERENCES release_chunks(release_id, chunk_id)
);

CREATE TABLE answer_citations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    release_id uuid NOT NULL,
    claim_key text NOT NULL,
    section_id uuid NOT NULL REFERENCES document_sections(id),
    span_id uuid NOT NULL,
    cited_text text NOT NULL,
    FOREIGN KEY (run_id, release_id) REFERENCES assistant_runs(id, release_id),
    FOREIGN KEY (release_id, section_id) REFERENCES entity_versions(release_id, entity_id),
    FOREIGN KEY (span_id, section_id) REFERENCES section_spans(id, section_id)
);

CREATE TABLE feedback (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES assistant_runs(id),
    reviewer_id uuid NOT NULL REFERENCES actors(id),
    verdict text NOT NULL CHECK (verdict IN ('acceptable','needs_revision','incorrect')),
    error_categories array_json NOT NULL DEFAULT '[]'::jsonb,
    comment text,
    supersedes_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, run_id),
    FOREIGN KEY (supersedes_id, run_id) REFERENCES feedback(id, run_id),
    CHECK (supersedes_id IS DISTINCT FROM id)
);

CREATE TABLE maintenance_outcomes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    context_id uuid NOT NULL,
    release_id uuid NOT NULL,
    finding text NOT NULL,
    confirmed_entity_id uuid,
    confirmed_kind text CHECK (confirmed_kind IN ('hypothesis','fault_type')),
    source_section_id uuid NOT NULL REFERENCES document_sections(id),
    verified_by uuid NOT NULL REFERENCES actors(id),
    verified_at timestamptz NOT NULL,
    supersedes_id uuid,
    FOREIGN KEY (context_id, release_id) REFERENCES alert_contexts(id, release_id),
    FOREIGN KEY (release_id, confirmed_entity_id, confirmed_kind) REFERENCES entity_versions(release_id, entity_id, entity_kind),
    UNIQUE (id, context_id, release_id),
    FOREIGN KEY (supersedes_id, context_id, release_id) REFERENCES maintenance_outcomes(id, context_id, release_id),
    CHECK ((confirmed_entity_id IS NULL) = (confirmed_kind IS NULL)),
    CHECK (supersedes_id IS DISTINCT FROM id)
);

-- 7. Fixed, separately reviewed evaluation cases.
CREATE TABLE evaluation_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_key text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    release_id uuid NOT NULL,
    release_status text NOT NULL DEFAULT 'published' CHECK (release_status = 'published'),
    split text NOT NULL CHECK (split IN ('development','holdout','regression')),
    origin text NOT NULL CHECK (origin IN ('real_event','expert_authored','synthetic')),
    source_event_id uuid REFERENCES alert_events(id),
    question text NOT NULL,
    input_snapshot object_json NOT NULL,
    expected_status text NOT NULL CHECK (expected_status IN ('answered','needs_context','insufficient_evidence')),
    required_questions array_json NOT NULL DEFAULT '[]'::jsonb,
    forbidden_claims array_json NOT NULL DEFAULT '[]'::jsonb,
    reviewed_by uuid NOT NULL REFERENCES actors(id),
    reviewed_at timestamptz NOT NULL,
    sealed_at timestamptz,
    FOREIGN KEY (release_id, release_status) REFERENCES knowledge_releases(id, status),
    UNIQUE (case_key, revision),
    UNIQUE (id, release_id),
    CHECK (origin <> 'real_event' OR source_event_id IS NOT NULL)
);

CREATE TABLE evaluation_expected_sections (
    case_id uuid NOT NULL,
    release_id uuid NOT NULL,
    section_id uuid NOT NULL REFERENCES document_sections(id),
    relevance_grade integer NOT NULL CHECK (relevance_grade BETWEEN 1 AND 3),
    requirement_group text NOT NULL DEFAULT 'main',
    PRIMARY KEY (case_id, section_id),
    FOREIGN KEY (case_id, release_id) REFERENCES evaluation_cases(id, release_id),
    FOREIGN KEY (release_id, section_id) REFERENCES entity_versions(release_id, entity_id)
);

CREATE TABLE evaluation_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL,
    run_id uuid NOT NULL,
    release_id uuid NOT NULL,
    evaluator_version text NOT NULL,
    metrics object_json NOT NULL,
    human_verdict text CHECK (human_verdict IN ('pass','fail','undecided')),
    evaluated_by uuid REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, release_id) REFERENCES evaluation_cases(id, release_id),
    FOREIGN KEY (run_id, release_id) REFERENCES assistant_runs(id, release_id)
);

-- 8. Indexes. Embeddings intentionally have NO approximate vector index in v1.
CREATE INDEX entity_alias_lookup ON entity_aliases (release_id, language, alias);
CREATE INDEX section_revision_lookup ON document_sections (document_revision_id);
CREATE INDEX relation_forward ON knowledge_relations (release_id, subject_id, predicate);
CREATE INDEX relation_reverse ON knowledge_relations (release_id, object_id, predicate);
CREATE INDEX applicability_lookup ON applicability (release_id, asset_config_id, content_entity_id, reference_scope, review_status);
CREATE INDEX conflicts_lookup ON knowledge_conflicts (release_id, affected_entity_id, status);
CREATE INDEX chunks_section_lookup ON retrieval_chunks (section_id);
CREATE INDEX chunks_fulltext ON retrieval_chunks USING gin (search_tsv);
CREATE INDEX embeddings_model_lookup ON chunk_embeddings (embedding_model_id);
CREATE INDEX alerts_time_lookup ON alert_events (reported_asset_key, occurred_at DESC);
CREATE INDEX contexts_asset_lookup ON alert_contexts (asset_config_id, created_at DESC);
CREATE INDEX contexts_event_lookup ON alert_contexts (alert_id);
CREATE INDEX observed_codes_context_lookup ON observed_codes (context_id);
CREATE INDEX observations_context_lookup ON observations (context_id);
CREATE INDEX ml_assessments_context_lookup ON ml_assessments (context_id);
CREATE INDEX assistant_context_lookup ON assistant_runs (context_id, started_at DESC);

-- 9. Lifecycle enforcement. These protect normal application DML, not a DB owner.
CREATE FUNCTION reject_row_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%.% is append-only; create a new revision/record', TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$;

-- Seal a complete parent+child aggregate after importing all its children.
CREATE FUNCTION guard_sealable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Sealable records cannot be deleted'; END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.sealed_at IS NOT NULL THEN RAISE EXCEPTION 'Insert unsealed, populate children, then seal'; END IF;
        RETURN NEW;
    END IF;
    IF OLD.sealed_at IS NOT NULL THEN RAISE EXCEPTION 'Sealed record is immutable'; END IF;
    IF (to_jsonb(NEW) - 'sealed_at') IS DISTINCT FROM (to_jsonb(OLD) - 'sealed_at') OR NEW.sealed_at IS NULL THEN
        RAISE EXCEPTION 'Only a one-time sealing update is allowed; otherwise create a new revision';
    END IF;
    NEW.sealed_at := now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER source_seal BEFORE INSERT OR UPDATE OR DELETE ON document_revisions FOR EACH ROW EXECUTE FUNCTION guard_sealable_record();
CREATE TRIGGER context_seal BEFORE INSERT OR UPDATE OR DELETE ON alert_contexts FOR EACH ROW EXECUTE FUNCTION guard_sealable_record();
CREATE TRIGGER evaluation_case_seal BEFORE INSERT OR UPDATE OR DELETE ON evaluation_cases FOR EACH ROW EXECUTE FUNCTION guard_sealable_record();

CREATE FUNCTION guard_aggregate_child() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sealed timestamptz; parent_found boolean;
BEGIN
    IF TG_ARGV[0] = 'document' THEN
        SELECT sealed_at INTO sealed FROM rsw.document_revisions WHERE id = NEW.document_revision_id FOR UPDATE;
    ELSIF TG_ARGV[0] = 'context' THEN
        SELECT sealed_at INTO sealed FROM rsw.alert_contexts WHERE id = NEW.context_id FOR UPDATE;
    ELSIF TG_ARGV[0] = 'evaluation_case' THEN
        SELECT sealed_at INTO sealed FROM rsw.evaluation_cases WHERE id = NEW.case_id FOR UPDATE;
    ELSE RAISE EXCEPTION 'Unknown aggregate kind';
    END IF;
    parent_found := FOUND;
    IF NOT parent_found OR sealed IS NOT NULL THEN RAISE EXCEPTION 'Parent is missing or already sealed'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER source_page_open BEFORE INSERT ON document_pages FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('document');
CREATE TRIGGER source_section_open BEFORE INSERT ON document_sections FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('document');
CREATE TRIGGER source_span_open BEFORE INSERT ON section_spans FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('document');
CREATE TRIGGER context_codes_open BEFORE INSERT ON observed_codes FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('context');
CREATE TRIGGER context_observations_open BEFORE INSERT ON observations FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('context');
CREATE TRIGGER context_ml_open BEFORE INSERT ON ml_assessments FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('context');
CREATE TRIGGER expected_sections_open BEFORE INSERT ON evaluation_expected_sections FOR EACH ROW EXECUTE FUNCTION guard_aggregate_child('evaluation_case');

CREATE FUNCTION guard_evaluation_result() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE case_sealed timestamptz; run_state text;
BEGIN
    SELECT sealed_at INTO case_sealed FROM rsw.evaluation_cases WHERE id = NEW.case_id FOR UPDATE;
    SELECT status INTO run_state FROM rsw.assistant_runs WHERE id = NEW.run_id FOR UPDATE;
    IF case_sealed IS NULL OR run_state IS NULL OR run_state IN ('pending','running') THEN
        RAISE EXCEPTION 'Evaluation requires a sealed case and a completed run';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER evaluation_ready BEFORE INSERT ON evaluation_results FOR EACH ROW EXECUTE FUNCTION guard_evaluation_result();

CREATE FUNCTION guard_release_child() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    rid uuid;
    release_state text;
BEGIN
    IF TG_OP = 'DELETE' THEN rid := OLD.release_id; ELSE rid := NEW.release_id; END IF;
    IF TG_OP = 'UPDATE' AND NEW.release_id IS DISTINCT FROM OLD.release_id THEN
        RAISE EXCEPTION 'Moving records between knowledge releases is forbidden';
    END IF;
    -- All knowledge writes take this same lock; freezing waits for pending writes.
    SELECT status INTO release_state FROM rsw.knowledge_releases WHERE id = rid FOR UPDATE;
    IF release_state IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'Knowledge release % is not editable (state %)', rid, release_state;
    END IF;
    -- Any content/review change invalidates the final whole-release domain approval.
    UPDATE rsw.knowledge_releases SET domain_reviewed_by = NULL, domain_reviewed_at = NULL
      WHERE id = rid AND domain_reviewed_by IS NOT NULL;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;

CREATE FUNCTION guard_release_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Knowledge releases cannot be deleted'; END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN RAISE EXCEPTION 'Create a release as draft'; END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'Release ID is immutable'; END IF;
    IF OLD.status = 'published' THEN RAISE EXCEPTION 'Published release is immutable'; END IF;
    IF OLD.status = 'draft' AND
       (NEW.version, NEW.parent_release_id, NEW.ontology_version, NEW.ontology_sha256, NEW.shapes_version,
        NEW.shapes_sha256, NEW.builder_version, NEW.validation_contract_version)
       IS DISTINCT FROM
       (OLD.version, OLD.parent_release_id, OLD.ontology_version, OLD.ontology_sha256, OLD.shapes_version,
        OLD.shapes_sha256, OLD.builder_version, OLD.validation_contract_version) THEN
        NEW.domain_reviewed_by := NULL; NEW.domain_reviewed_at := NULL;
    END IF;
    IF OLD.status = 'frozen' THEN
        IF NEW.status NOT IN ('frozen','published') THEN RAISE EXCEPTION 'Clone frozen release instead of reopening'; END IF;
        IF (to_jsonb(NEW) - ARRAY['status','graph_uri','graph_sha256','validation_report','shacl_conforms','publish_checks_passed','published_at'])
           IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['status','graph_uri','graph_sha256','validation_report','shacl_conforms','publish_checks_passed','published_at']) THEN
            RAISE EXCEPTION 'Frozen release metadata is immutable';
        END IF;
    ELSIF NEW.status NOT IN ('draft','frozen') THEN
        RAISE EXCEPTION 'Freeze and validate before publishing';
    END IF;
    IF OLD.status = 'draft' AND NEW.status = 'frozen' THEN
        IF NEW.domain_reviewed_by IS NULL OR NEW.domain_reviewed_at IS NULL THEN
            RAISE EXCEPTION 'Whole-release domain review is required before freezing';
        END IF;
        IF EXISTS (
            SELECT 1 FROM rsw.entity_versions ev
            JOIN rsw.document_sections ds ON ds.id = ev.entity_id
            JOIN rsw.document_revisions dr ON dr.id = ds.document_revision_id
            WHERE ev.release_id = NEW.id AND dr.sealed_at IS NULL
        ) THEN RAISE EXCEPTION 'Seal all included source revisions before freezing'; END IF;
        NEW.frozen_at := now();
        NEW.graph_uri := NULL; NEW.graph_sha256 := NULL; NEW.validation_report := NULL;
        NEW.shacl_conforms := NULL; NEW.publish_checks_passed := NULL; NEW.published_at := NULL;
    END IF;
    IF NEW.status = 'published' THEN NEW.published_at := now(); END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER release_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON knowledge_releases
FOR EACH ROW EXECUTE FUNCTION guard_release_lifecycle();

CREATE FUNCTION guard_run_completion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE context_sealed timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Assistant runs cannot be deleted'; END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT sealed_at INTO context_sealed FROM rsw.alert_contexts WHERE id = NEW.context_id FOR UPDATE;
        IF context_sealed IS NULL OR NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'Start a pending run against a sealed context';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status NOT IN ('pending','running') THEN RAISE EXCEPTION 'Completed run is immutable; create a new run'; END IF;
    IF (NEW.id, NEW.context_id, NEW.release_id, NEW.question, NEW.model_provider, NEW.model_name,
        NEW.prompt_version, NEW.retriever_version, NEW.embedding_model_id, NEW.parameters, NEW.requested_by, NEW.started_at)
       IS DISTINCT FROM
       (OLD.id, OLD.context_id, OLD.release_id, OLD.question, OLD.model_provider, OLD.model_name,
        OLD.prompt_version, OLD.retriever_version, OLD.embedding_model_id, OLD.parameters, OLD.requested_by, OLD.started_at) THEN
        RAISE EXCEPTION 'Run inputs cannot change';
    END IF;
    IF OLD.status = 'running' AND NEW.status = 'pending' THEN RAISE EXCEPTION 'Run state cannot move backward'; END IF;
    IF NEW.status NOT IN ('pending','running') AND NEW.input_snapshot IS NULL THEN
        RAISE EXCEPTION 'Completed run requires the actual input/context snapshot';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER run_completion BEFORE INSERT OR UPDATE OR DELETE ON assistant_runs
FOR EACH ROW EXECUTE FUNCTION guard_run_completion();

CREATE FUNCTION guard_run_child() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_state text; run_embedding_model uuid;
BEGIN
    SELECT status, embedding_model_id INTO run_state, run_embedding_model
      FROM rsw.assistant_runs WHERE id = NEW.run_id FOR UPDATE;
    IF run_state IS NULL OR run_state NOT IN ('pending','running') THEN
        RAISE EXCEPTION 'Add trace/citations before completing the run';
    END IF;
    IF TG_TABLE_NAME = 'retrieval_candidates' THEN
        IF NEW.method = 'vector' THEN
            IF run_embedding_model IS NULL OR NOT EXISTS (
                SELECT 1 FROM rsw.chunk_embeddings
                WHERE chunk_id = NEW.chunk_id AND embedding_model_id = run_embedding_model
            ) THEN RAISE EXCEPTION 'Vector candidate requires an embedding for the run model'; END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER citation_run_open BEFORE INSERT ON answer_citations FOR EACH ROW EXECUTE FUNCTION guard_run_child();
CREATE TRIGGER candidate_run_open BEFORE INSERT ON retrieval_candidates FOR EACH ROW EXECUTE FUNCTION guard_run_child();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
      'entity_versions','entity_aliases','scenarios','feature_definitions','feature_bindings',
      'evidence_patterns','procedures','procedure_steps','numeric_specs','knowledge_relations',
      'relation_evidence','applicability','applicability_evidence','knowledge_conflicts','release_chunks'
    ] LOOP
      EXECUTE format('CREATE TRIGGER release_edit_guard BEFORE INSERT OR UPDATE OR DELETE ON rsw.%I FOR EACH ROW EXECUTE FUNCTION rsw.guard_release_child()', table_name);
    END LOOP;
    FOREACH table_name IN ARRAY ARRAY[
      'knowledge_entities','asset_configs','document_pages','document_sections',
      'section_spans','relation_rules','retrieval_chunks','embedding_models','chunk_embeddings',
      'event_producers','alert_events','observed_codes','observations','ml_assessments',
      'retrieval_candidates','answer_citations','feedback','maintenance_outcomes',
      'evaluation_expected_sections','evaluation_results'
    ] LOOP
      EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON rsw.%I FOR EACH ROW EXECUTE FUNCTION rsw.reject_row_mutation()', table_name);
    END LOOP;
END;
$$;

COMMIT;
