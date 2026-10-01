-- RSW schema smoke / negative checks.
-- IMPORTANT: This script has NOT been executed against PostgreSQL or pgvector.
-- It has only been inspected statically. Passing this file is not yet verified.
-- Run only in a disposable development database AFTER installing schema.sql.
-- Suggested client: psql -X -v ON_ERROR_STOP=1 -d YOUR_TEST_DB -f checks.sql
-- Everything created below is synthetic test data and is rolled back.
-- The existing schema, extension and migration are not modified.
-- This is a single-session test: it does NOT verify concurrency, authorization,
-- SHACL, file hashes, excerpt accuracy, retrieval quality or external APIs.

BEGIN;
SET LOCAL search_path = rsw, public;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $checks$
DECLARE
    v_actor uuid := gen_random_uuid();
    v_release uuid := gen_random_uuid();
    v_other_release uuid := gen_random_uuid();
    v_document uuid := gen_random_uuid();
    v_section uuid := gen_random_uuid();
    v_section_two uuid := gen_random_uuid();
    v_late_section uuid := gen_random_uuid();
    v_span uuid := gen_random_uuid();
    v_scenario uuid := gen_random_uuid();
    v_other_fault uuid := gen_random_uuid();
    v_alarm_a uuid := gen_random_uuid();
    v_alarm_b uuid := gen_random_uuid();
    v_chunk uuid := gen_random_uuid();
    v_chunk_two uuid := gen_random_uuid();
    v_model uuid := gen_random_uuid();
    v_other_model uuid := gen_random_uuid();
    v_producer uuid := gen_random_uuid();
    v_event uuid := gen_random_uuid();
    v_context uuid := gen_random_uuid();
    v_run uuid := gen_random_uuid();
    v_other_run uuid := gen_random_uuid();
    v_case uuid := gen_random_uuid();
    v_feedback uuid := gen_random_uuid();
    v_prefix text := '__rsw_smoke_' || gen_random_uuid()::text;
    v_hash text := repeat('a', 64); -- Synthetic fixture only; not a real digest.
    v_rejected boolean;
    v_passed integer := 0;
BEGIN
    INSERT INTO actors (id, external_key, display_name, role)
    VALUES (v_actor, v_prefix, 'Synthetic smoke reviewer', 'domain_reviewer');

    INSERT INTO knowledge_entities (id, kind, namespace, local_code) VALUES
      (v_alarm_a, 'alarm_code', v_prefix || ':vendor_a', 'E12'),
      (v_alarm_b, 'alarm_code', v_prefix || ':vendor_b', 'E12'),
      (v_section, 'document_section', v_prefix, 'section-1'),
      (v_section_two, 'document_section', v_prefix, 'section-2'),
      (v_late_section, 'document_section', v_prefix, 'section-late'),
      (v_scenario, 'scenario', v_prefix, 'scenario-1'),
      (v_other_fault, 'fault_type', v_prefix, 'only-in-other-release');

    -- T01: Identical vendor codes remain different entities across namespaces.
    IF (SELECT count(*) FROM knowledge_entities
        WHERE namespace IN (v_prefix || ':vendor_a', v_prefix || ':vendor_b')
          AND kind = 'alarm_code' AND local_code = 'E12') <> 2 THEN
        RAISE EXCEPTION 'T01 failed: namespace-scoped identities were not preserved';
    END IF;
    v_passed := v_passed + 1;

    -- T02: The same kind + namespace + code cannot be inserted twice.
    v_rejected := false;
    BEGIN
        INSERT INTO knowledge_entities (kind, namespace, local_code)
        VALUES ('alarm_code', v_prefix || ':vendor_a', 'E12');
    EXCEPTION WHEN unique_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T02 failed: duplicate identity accepted'; END IF;
    v_passed := v_passed + 1;

    INSERT INTO document_revisions
      (id, document_key, revision_key, source_kind, title, language, source_uri,
       source_sha256, extraction_version, page_count, created_by)
    VALUES
      (v_document, v_prefix, 'fixture-r1', 'expert_note', 'Synthetic fixture', 'en',
       'urn:rsw:test:' || v_document::text, v_hash, 'fixture-extractor-v1', 2, v_actor);
    INSERT INTO document_pages
      (document_revision_id, page_number, page_text, text_sha256, extraction_method)
    VALUES (v_document, 1, 'Synthetic page one.', v_hash, 'manual'),
           (v_document, 2, 'Synthetic page two.', v_hash, 'manual');
    INSERT INTO document_sections
      (id, document_revision_id, section_key, title, original_text, text_sha256)
    VALUES (v_section, v_document, 'one', 'One', 'Synthetic page one.', v_hash),
           (v_section_two, v_document, 'two', 'Two', 'Synthetic page two.', v_hash);
    INSERT INTO section_spans
      (id, section_id, document_revision_id, page_number, ordinal, excerpt)
    VALUES (v_span, v_section, v_document, 1, 1, 'Synthetic page one.');
    INSERT INTO retrieval_chunks
      (id, section_id, document_revision_id, chunking_version, ordinal, language,
       content, heading_path, content_sha256, page_start, page_end)
    VALUES (v_chunk, v_section, v_document, 'fixture-v1', 1, 'en',
            'Synthetic page one.', 'One', v_hash, 1, 1),
           (v_chunk_two, v_section_two, v_document, 'fixture-v1', 1, 'en',
            'Synthetic page two.', 'Two', v_hash, 2, 2);

    INSERT INTO embedding_models
      (id, provider, model_name, model_version, dimensions,
       preprocessing_version, config_sha256)
    VALUES (v_model, 'synthetic', v_prefix || ':model-a', 'v1', 3, 'v1', v_hash),
           (v_other_model, 'synthetic', v_prefix || ':model-b', 'v1', 3, 'v1', v_hash);

    -- T03: Vector length must equal the declared dimension.
    v_rejected := false;
    BEGIN
        INSERT INTO chunk_embeddings (chunk_id, embedding_model_id, dimensions, embedding)
        VALUES (v_chunk, v_model, 3, '[1,2]'::public.vector);
    EXCEPTION WHEN check_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T03 failed: wrong vector length accepted'; END IF;
    v_passed := v_passed + 1;

    -- T04: A self-consistent vector length still must match its model definition.
    v_rejected := false;
    BEGIN
        INSERT INTO chunk_embeddings (chunk_id, embedding_model_id, dimensions, embedding)
        VALUES (v_chunk, v_model, 2, '[1,2]'::public.vector);
    EXCEPTION WHEN foreign_key_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T04 failed: model/dimension mismatch accepted'; END IF;
    v_passed := v_passed + 1;
    INSERT INTO chunk_embeddings (chunk_id, embedding_model_id, dimensions, embedding)
    VALUES (v_chunk, v_model, 3, '[1,2,3]'::public.vector);

    INSERT INTO knowledge_releases
      (id, version, ontology_version, ontology_sha256, shapes_version, shapes_sha256,
       builder_version, validation_contract_version, created_by)
    VALUES (v_release, v_prefix || ':r1', 'test-o1', v_hash, 'test-s1', v_hash,
            'test-b1', 'test-v1', v_actor),
           (v_other_release, v_prefix || ':r2', 'test-o1', v_hash, 'test-s1', v_hash,
            'test-b1', 'test-v1', v_actor);
    INSERT INTO entity_versions (release_id, entity_id, entity_kind, label_ko, description)
    VALUES (v_release, v_section, 'document_section', '시험 절 1', 'Synthetic'),
           (v_release, v_section_two, 'document_section', '시험 절 2', 'Synthetic'),
           (v_release, v_scenario, 'scenario', '시험 상황', 'Synthetic'),
           (v_release, v_alarm_a, 'alarm_code', '시험 경보', 'Synthetic'),
           (v_other_release, v_other_fault, 'fault_type', '다른 릴리스 고장', 'Synthetic');
    INSERT INTO release_chunks (release_id, chunk_id, section_id)
    VALUES (v_release, v_chunk, v_section), (v_release, v_chunk_two, v_section_two);

    -- T05: A graph edge cannot borrow an object that exists only in another release.
    v_rejected := false;
    BEGIN
        INSERT INTO knowledge_relations
          (release_id, subject_id, subject_kind, predicate, object_id, object_kind, evidence_kind)
        VALUES (v_release, v_scenario, 'scenario', 'related_fault',
                v_other_fault, 'fault_type', 'expert');
    EXCEPTION WHEN foreign_key_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T05 failed: cross-release relation accepted'; END IF;
    v_passed := v_passed + 1;

    -- T06: Editing release content invalidates its previous whole-release approval.
    UPDATE knowledge_releases SET domain_reviewed_by = v_actor, domain_reviewed_at = now()
    WHERE id = v_release;
    UPDATE entity_versions SET description = 'Synthetic amended content'
    WHERE release_id = v_release AND entity_id = v_scenario;
    IF EXISTS (SELECT 1 FROM knowledge_releases WHERE id = v_release
               AND (domain_reviewed_by IS NOT NULL OR domain_reviewed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'T06 failed: content change retained approval';
    END IF;
    v_passed := v_passed + 1;

    -- T07: Changing the builder/ontology contract also invalidates approval.
    UPDATE knowledge_releases SET domain_reviewed_by = v_actor, domain_reviewed_at = now()
    WHERE id = v_release;
    UPDATE knowledge_releases SET builder_version = 'test-b2' WHERE id = v_release;
    IF EXISTS (SELECT 1 FROM knowledge_releases WHERE id = v_release
               AND (domain_reviewed_by IS NOT NULL OR domain_reviewed_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'T07 failed: metadata change retained approval';
    END IF;
    v_passed := v_passed + 1;

    -- T08: Freezing without whole-release approval is rejected.
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET status = 'frozen' WHERE id = v_release;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Whole-release domain review%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T08 failed: unreviewed release froze'; END IF;
    v_passed := v_passed + 1;

    -- T09: A draft cannot skip directly to published.
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET status = 'published' WHERE id = v_release;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Freeze and validate%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T09 failed: draft published directly'; END IF;
    v_passed := v_passed + 1;

    -- T10: Included source revisions must be sealed before release freezing.
    UPDATE knowledge_releases SET domain_reviewed_by = v_actor, domain_reviewed_at = now()
    WHERE id = v_release;
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET status = 'frozen' WHERE id = v_release;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Seal all included source revisions%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T10 failed: unsealed source release froze'; END IF;
    v_passed := v_passed + 1;

    UPDATE document_revisions SET sealed_at = now() WHERE id = v_document;

    -- T11-T13: Sealing closes all three source child collections.
    v_rejected := false;
    BEGIN
        INSERT INTO document_pages
          (document_revision_id, page_number, page_text, text_sha256, extraction_method)
        VALUES (v_document, 3, 'Late page', v_hash, 'manual');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T11 failed: page added after source seal'; END IF;
    v_passed := v_passed + 1;
    v_rejected := false;
    BEGIN
        INSERT INTO document_sections
          (id, document_revision_id, section_key, title, original_text, text_sha256)
        VALUES (v_late_section, v_document, 'late', 'Late', 'Late section', v_hash);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T12 failed: section added after source seal'; END IF;
    v_passed := v_passed + 1;
    v_rejected := false;
    BEGIN
        INSERT INTO section_spans
          (section_id, document_revision_id, page_number, ordinal, excerpt)
        VALUES (v_section, v_document, 1, 2, 'Late span');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T13 failed: span added after source seal'; END IF;
    v_passed := v_passed + 1;

    UPDATE knowledge_releases SET status = 'frozen' WHERE id = v_release;

    -- T14: Frozen release content cannot be edited.
    v_rejected := false;
    BEGIN
        UPDATE entity_versions SET description = 'Forbidden late edit'
        WHERE release_id = v_release AND entity_id = v_scenario;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Knowledge release % is not editable%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T14 failed: frozen content changed'; END IF;
    v_passed := v_passed + 1;

    -- T15: A frozen release cannot return to draft.
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET status = 'draft' WHERE id = v_release;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Clone frozen release%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T15 failed: frozen release reopened'; END IF;
    v_passed := v_passed + 1;

    -- T16: Publication needs the required graph/validation fields.
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET status = 'published' WHERE id = v_release;
    EXCEPTION WHEN check_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T16 failed: incomplete release published'; END IF;
    v_passed := v_passed + 1;

    -- Synthetic flags simulate the publisher contract only; SHACL is not run here.
    UPDATE knowledge_releases
    SET status = 'published', graph_uri = 'urn:rsw:test:graph:' || v_release::text,
        graph_sha256 = v_hash, validation_report = '{"synthetic_fixture":true}'::jsonb,
        shacl_conforms = true, publish_checks_passed = true
    WHERE id = v_release;

    -- T17: Even metadata changes are rejected once published.
    v_rejected := false;
    BEGIN
        UPDATE knowledge_releases SET builder_version = 'forbidden' WHERE id = v_release;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Published release is immutable%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T17 failed: published release changed'; END IF;
    v_passed := v_passed + 1;

    INSERT INTO event_producers (id, producer_key, description)
    VALUES (v_producer, v_prefix, 'Synthetic event producer');
    INSERT INTO alert_events
      (id, producer_id, source_event_id, reported_asset_key, occurred_at,
       producer_schema_version, raw_payload, parsed_payload, payload_sha256)
    VALUES (v_event, v_producer, 'test-event', 'unmapped-synthetic-asset', now(),
            'fixture-v1', convert_to('{}', 'UTF8'), '{}'::jsonb, v_hash);
    INSERT INTO alert_contexts
      (id, alert_id, release_id, normalizer_version, status)
    VALUES (v_context, v_event, v_release, 'fixture-v1', 'unmapped');
    INSERT INTO observations
      (context_id, release_id, raw_feature_key, value, quality)
    VALUES (v_context, v_release, 'test-channel', '1'::jsonb, 'valid');

    -- T18: A run cannot begin before context children are sealed.
    v_rejected := false;
    BEGIN
        INSERT INTO assistant_runs
          (id, context_id, release_id, question, model_provider, model_name,
           prompt_version, retriever_version, embedding_model_id)
        VALUES (v_run, v_context, v_release, 'Synthetic question', 'synthetic',
                'synthetic-llm', 'v1', 'v1', v_model);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Start a pending run against a sealed context%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T18 failed: run accepted unsealed context'; END IF;
    v_passed := v_passed + 1;
    UPDATE alert_contexts SET sealed_at = now() WHERE id = v_context;

    -- T19-T21: Context sealing prevents later observations, codes and ML outputs.
    v_rejected := false;
    BEGIN
        INSERT INTO observations (context_id, release_id, raw_feature_key, quality)
        VALUES (v_context, v_release, 'late-channel', 'unknown');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T19 failed: late observation accepted'; END IF;
    v_passed := v_passed + 1;
    v_rejected := false;
    BEGIN
        INSERT INTO observed_codes
          (context_id, release_id, raw_namespace, raw_code, mapping_status)
        VALUES (v_context, v_release, v_prefix, 'late-code', 'unmapped');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T20 failed: late code accepted'; END IF;
    v_passed := v_passed + 1;
    v_rejected := false;
    BEGIN
        INSERT INTO ml_assessments
          (context_id, release_id, model_name, model_version, assessment_kind)
        VALUES (v_context, v_release, 'synthetic-ml', 'v1', 'anomaly');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T21 failed: late ML output accepted'; END IF;
    v_passed := v_passed + 1;

    INSERT INTO assistant_runs
      (id, context_id, release_id, question, model_provider, model_name,
       prompt_version, retriever_version, embedding_model_id)
    VALUES (v_run, v_context, v_release, 'Synthetic question', 'synthetic',
            'synthetic-llm', 'v1', 'v1', v_model),
           (v_other_run, v_context, v_release, 'Other synthetic question', 'synthetic',
            'synthetic-llm', 'v1', 'v1', v_other_model);

    -- T22: Same dimensionality does not permit borrowing another model's vector.
    v_rejected := false;
    BEGIN
        INSERT INTO retrieval_candidates
          (run_id, release_id, chunk_id, method, rank, applicability_result)
        VALUES (v_other_run, v_release, v_chunk, 'vector', 1, '{}'::jsonb);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Vector candidate requires an embedding for the run model%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T22 failed: wrong-model vector candidate accepted'; END IF;
    v_passed := v_passed + 1;

    -- T23: A candidate cannot use a different release from its run.
    v_rejected := false;
    BEGIN
        INSERT INTO retrieval_candidates
          (run_id, release_id, chunk_id, method, rank, applicability_result)
        VALUES (v_run, v_other_release, v_chunk, 'keyword', 1, '{}'::jsonb);
    EXCEPTION WHEN foreign_key_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T23 failed: candidate crossed releases'; END IF;
    v_passed := v_passed + 1;
    INSERT INTO retrieval_candidates
      (run_id, release_id, chunk_id, method, rank, selected, applicability_result)
    VALUES (v_run, v_release, v_chunk, 'vector', 1, true, '{}'::jsonb);
    INSERT INTO answer_citations
      (run_id, release_id, claim_key, section_id, span_id, cited_text)
    VALUES (v_run, v_release, 'fixture-claim', v_section, v_span, 'Synthetic page one.');

    -- T24: Completion requires a snapshot of the actual inputs.
    v_rejected := false;
    BEGIN
        UPDATE assistant_runs
        SET status = 'insufficient_evidence', answer = '{"synthetic_fixture":true}'::jsonb,
            finished_at = now()
        WHERE id = v_run;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Completed run requires the actual input/context snapshot%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T24 failed: run completed without snapshot'; END IF;
    v_passed := v_passed + 1;
    UPDATE assistant_runs
    SET status = 'insufficient_evidence', answer = '{"synthetic_fixture":true}'::jsonb,
        input_snapshot = '{"synthetic_fixture":true}'::jsonb, finished_at = now()
    WHERE id = v_run;

    -- T25-T26: Completed runs reject both edits and new citations.
    v_rejected := false;
    BEGIN
        UPDATE assistant_runs SET answer = '{}'::jsonb WHERE id = v_run;
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Completed run is immutable%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T25 failed: completed run changed'; END IF;
    v_passed := v_passed + 1;
    v_rejected := false;
    BEGIN
        INSERT INTO answer_citations
          (run_id, release_id, claim_key, section_id, span_id, cited_text)
        VALUES (v_run, v_release, 'late-claim', v_section, v_span, 'Synthetic page one.');
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Add trace/citations before completing the run%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T26 failed: citation added after completion'; END IF;
    v_passed := v_passed + 1;

    INSERT INTO evaluation_cases
      (id, case_key, revision, release_id, split, origin, question, input_snapshot,
       expected_status, reviewed_by, reviewed_at)
    VALUES (v_case, v_prefix, 1, v_release, 'regression', 'synthetic',
            'Synthetic question', '{"synthetic_fixture":true}'::jsonb,
            'insufficient_evidence', v_actor, now());
    INSERT INTO evaluation_expected_sections (case_id, release_id, section_id, relevance_grade)
    VALUES (v_case, v_release, v_section, 1);

    -- T27: Results cannot be attached to an unsealed evaluation case.
    v_rejected := false;
    BEGIN
        INSERT INTO evaluation_results (case_id, run_id, release_id, evaluator_version, metrics)
        VALUES (v_case, v_run, v_release, 'fixture-v1', '{}'::jsonb);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Evaluation requires a sealed case and a completed run%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T27 failed: unsealed evaluation was scored'; END IF;
    v_passed := v_passed + 1;
    UPDATE evaluation_cases SET sealed_at = now() WHERE id = v_case;

    -- T28: Gold sections cannot be appended after sealing.
    v_rejected := false;
    BEGIN
        INSERT INTO evaluation_expected_sections (case_id, release_id, section_id, relevance_grade)
        VALUES (v_case, v_release, v_section_two, 1);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Parent is missing or already sealed%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T28 failed: sealed gold set changed'; END IF;
    v_passed := v_passed + 1;

    -- T29: A sealed gold set still cannot score a pending run.
    v_rejected := false;
    BEGIN
        INSERT INTO evaluation_results (case_id, run_id, release_id, evaluator_version, metrics)
        VALUES (v_case, v_other_run, v_release, 'fixture-v1', '{}'::jsonb);
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Evaluation requires a sealed case and a completed run%' THEN RAISE; END IF;
        v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T29 failed: pending run was scored'; END IF;
    v_passed := v_passed + 1;
    INSERT INTO evaluation_results (case_id, run_id, release_id, evaluator_version, metrics)
    VALUES (v_case, v_run, v_release, 'fixture-v1', '{"synthetic_fixture":true}'::jsonb);

    -- T30: Feedback supersession stays within the same run.
    INSERT INTO feedback (id, run_id, reviewer_id, verdict)
    VALUES (v_feedback, v_run, v_actor, 'needs_revision');
    v_rejected := false;
    BEGIN
        INSERT INTO feedback (run_id, reviewer_id, verdict, supersedes_id)
        VALUES (v_other_run, v_actor, 'acceptable', v_feedback);
    EXCEPTION WHEN foreign_key_violation THEN v_rejected := true;
    END;
    IF NOT v_rejected THEN RAISE EXCEPTION 'T30 failed: feedback superseded another run'; END IF;
    v_passed := v_passed + 1;

    IF v_passed <> 30 THEN
        RAISE EXCEPTION 'Test bookkeeping failed: expected 30 checks, counted %', v_passed;
    END IF;
    RAISE NOTICE 'PASS: % single-session schema checks; all synthetic rows will be rolled back.', v_passed;
END;
$checks$;

ROLLBACK;
