"""POST/GET /alerts: 원본 보존, 중복·충돌, 정규화 결과, 버전 고정, 도메인 판정."""
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.core.jsonutil import as_json
from app.db.sqlutil import execute, fetch_all, fetch_one
from app.demo.seed import sample_alert
from app.main import create_app
from tests.contract import assert_contract
from tests.integration.helpers import drain, post_alert, ready_alert


async def test_new_alert_duplicate_and_conflict(client, tokens, database):
    payload = sample_alert(f"it-{uuid.uuid4().hex}")
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    first = await post_alert(client, tokens["ml"], payload, raw=raw)
    assert first.status_code == 201
    body = first.json()
    assert first.headers["location"] == f"/api/v1/alerts/{body['alert_id']}" == body["status_url"]
    assert first.headers["retry-after"] == "2" and first.headers["cache-control"] == "no-store"
    assert body["duplicate"] is False and body["normalization"]["status"] == "queued"

    # 공백·키 순서만 다른 같은 JSON은 같은 사건이다 (배열 순서는 유지).
    reordered = json.dumps(dict(reversed(list(payload.items()))), separators=(",", ":"), ensure_ascii=False).encode()
    again = await post_alert(client, tokens["ml"], raw=reordered)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["alert_id"] == body["alert_id"] and again.json()["received_at"] == body["received_at"]
    assert "location" not in again.headers

    changed = dict(payload, asset_key="RSW-DEMO-002")
    conflict = await post_alert(client, tokens["ml"], changed)
    assert conflict.status_code == 409 and conflict.json()["code"] == "EVENT_ID_CONFLICT"
    assert_contract(conflict.json(), "Problem")

    explicit_null = dict(payload, observation_window=None)  # 필드 생략과 명시적 null은 다르다
    assert (await post_alert(client, tokens["ml"], explicit_null)).status_code == 409

    async with database.session() as s:
        rows = await fetch_all(s, "SELECT raw_payload, parsed_payload, payload_sha256 FROM rsw.alert_events WHERE source_event_id = :e",
                               e=payload["event_id"])
        jobs = await fetch_all(s, "SELECT id FROM rsw.background_jobs WHERE alert_id = :a", a=body["alert_id"])
    assert len(rows) == 1 and len(jobs) == 1
    assert bytes(rows[0]["raw_payload"]) == raw, "최초 원본 바이트를 그대로 보존한다"
    assert as_json(rows[0]["parsed_payload"]) == payload


async def test_alert_details_before_and_after_normalization(client, tokens, worker, seed):
    created = (await post_alert(client, tokens["ml"])).json()
    pending = await client.get(f"/api/v1/alerts/{created['alert_id']}", headers=tokens["operator"])
    assert pending.status_code == 200 and pending.headers["retry-after"] == "2"
    assert pending.json()["context"] is None and pending.json()["related_scenarios"]["status"] == "not_evaluated"
    assert_contract(pending.json(), "AlertDetails")

    await drain(worker)
    details = (await client.get(f"/api/v1/alerts/{created['alert_id']}", headers=tokens["operator"])).json()
    assert_contract(details, "AlertDetails")
    context = details["context"]
    assert details["normalization"]["status"] == "succeeded"
    assert context["status"] == "mapped" and context["knowledge_version"] == "demo-kb-v1"
    assert context["asset"]["verification_status"] == "verified"
    assert context["code_mappings"][0]["entity_id"] == seed["entities"]["E01"]
    assert context["observation_mappings"][0]["status"] == "mapped"
    assert context["ml_mappings"][0]["target_kind"] == "fault_type"
    assert [w["code"] for w in context["warnings"]] == ["ML_SCORE_UNCALIBRATED"]
    scenarios = details["related_scenarios"]
    assert scenarios["status"] == "matched" and scenarios["matcher_version"] == "rsw-matcher-v1"
    assert scenarios["items"][0]["code"] == "S01"
    assert set(scenarios["items"][0]["match_methods"]) == {"observed_code", "ml_classification"}
    assert "원인 확정" in scenarios["items"][0]["explanation"]
    assert details["submitted_event"]["event_id"] == created["event_id"]

    explicit = await client.get(f"/api/v1/alerts/{created['alert_id']}", params={"context_id": context["context_id"]},
                                headers=tokens["operator"])
    assert explicit.json()["context"]["context_id"] == context["context_id"]
    other = await client.get(f"/api/v1/alerts/{created['alert_id']}", params={"context_id": str(uuid.uuid4())},
                             headers=tokens["operator"])
    assert other.status_code == 404 and other.json()["code"] == "CONTEXT_NOT_FOUND"
    assert (await client.get(f"/api/v1/alerts/{uuid.uuid4()}", headers=tokens["operator"])).status_code == 404


async def test_sealed_context_children_cannot_change(database, client, tokens, worker):
    import pytest
    from sqlalchemy.exc import DBAPIError

    details = await ready_alert(client, tokens, worker)
    async with database.session() as s:
        sealed = await fetch_one(s, "SELECT sealed_at FROM rsw.alert_contexts WHERE id = :id", id=details["context"]["context_id"])
    assert sealed["sealed_at"] is not None
    async with database.session() as s:
        with pytest.raises(DBAPIError) as info:
            async with s.begin():
                await execute(s, """INSERT INTO rsw.observed_codes (context_id, release_id, raw_namespace, raw_code, mapping_status)
                                    SELECT id, release_id, 'x', 'late', 'unmapped' FROM rsw.alert_contexts WHERE id = :id""",
                              id=details["context"]["context_id"])
    assert "already sealed" in str(info.value)


async def test_unverified_unknown_and_unregistered_assets(client, tokens, worker):
    unverified = await ready_alert(client, tokens, worker, sample_alert(f"it-{uuid.uuid4().hex}", asset_key="RSW-DEMO-002"))
    ctx = unverified["context"]
    assert ctx["status"] == "partial" and ctx["asset"]["verification_status"] == "unverified"
    codes = {w["code"] for w in ctx["warnings"]}
    assert {"ASSET_CONFIG_UNVERIFIED", "FEATURE_UNMAPPED"} <= codes  # 구성별 binding이 없으면 관측을 매핑하지 않는다

    unknown = sample_alert(f"it-{uuid.uuid4().hex}", asset_key="NO-SUCH-ASSET")
    unknown["observed_codes"] = [{"namespace": "other.vendor", "code": "DEMO-E01"}]  # 같은 코드 문자열, 다른 namespace
    unknown["ml_assessments"] = []
    unknown["observations"] = []
    details = await ready_alert(client, tokens, worker, unknown)
    ctx = details["context"]
    assert ctx["status"] == "unmapped"
    assert ctx["asset"] == {"reported_key": "NO-SUCH-ASSET", "asset_id": None, "asset_config_id": None, "verification_status": "unresolved"}
    assert ctx["code_mappings"][0]["status"] == "unmapped"
    assert details["related_scenarios"]["status"] == "no_match"


async def test_domain_contradictions_make_context_invalid(client, tokens, worker):
    future = sample_alert(f"it-{uuid.uuid4().hex}")
    future["occurred_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    future["observation_window"] = None
    details = await ready_alert(client, tokens, worker, future)
    assert details["normalization"]["status"] == "succeeded", "invalid 판정도 정상 저장된 정규화 결과다"
    assert details["context"]["status"] == "invalid"
    assert "EVENT_TIME_IN_FUTURE" in {w["code"] for w in details["context"]["warnings"]}

    mismatch = sample_alert(f"it-{uuid.uuid4().hex}")
    mismatch["observations"][0]["value"] = "fast"  # number feature에 문자열
    details = await ready_alert(client, tokens, worker, mismatch)
    assert details["context"]["status"] == "invalid"
    assert details["context"]["observation_mappings"][0]["status"] == "invalid"


async def test_knowledge_not_ready_rejects_new_events_only(cfg, database, client, tokens):
    payload = sample_alert(f"it-{uuid.uuid4().hex}")
    assert (await post_alert(client, tokens["ml"], payload)).status_code == 201
    no_release_cfg = cfg.model_copy(update={"KNOWLEDGE_DEPLOYMENT": "missing-deployment"})
    import httpx

    app = create_app(no_release_cfg, database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        rejected = await post_alert(other, tokens["ml"])
        assert rejected.status_code == 503 and rejected.json()["code"] == "KNOWLEDGE_NOT_READY"
        assert rejected.headers["retry-after"] == "2"
        duplicate = await post_alert(other, tokens["ml"], dict(payload))
        assert duplicate.status_code == 200, "이미 저장된 사건은 활성 지식이 없어도 기존 결과를 반환한다"


async def test_job_keeps_release_pinned_after_deployment_changes(cfg, database, client, tokens, worker, seed):
    import httpx

    pinned_cfg = cfg.model_copy(update={"KNOWLEDGE_DEPLOYMENT": f"pin-{uuid.uuid4().hex[:6]}"})
    async with database.session() as s, s.begin():
        actor = (await fetch_one(s, "SELECT id FROM rsw.actors WHERE external_key = 'demo:developer'"))["id"]
        await execute(s, "INSERT INTO rsw.knowledge_deployments (name, release_id, activated_by) VALUES (:n, :r, :a)",
                      n=pinned_cfg.KNOWLEDGE_DEPLOYMENT, r=seed["release_id"], a=actor)
    app = create_app(pinned_cfg, database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        created = (await post_alert(other, tokens["ml"])).json()
    async with database.session() as s, s.begin():  # 활성 배포본 비활성화
        await execute(s, "UPDATE rsw.knowledge_deployments SET release_id = NULL WHERE name = :n", n=pinned_cfg.KNOWLEDGE_DEPLOYMENT)
    await drain(worker)
    details = (await client.get(f"/api/v1/alerts/{created['alert_id']}", headers=tokens["operator"])).json()
    assert details["normalization"]["status"] == "succeeded"
    assert details["context"]["knowledge_release_id"] == seed["release_id"]


async def test_producer_boundaries(client, tokens, database, cfg):
    other_producer = sample_alert(f"it-{uuid.uuid4().hex}")
    other_producer["producer_key"] = "someone-else"
    denied = await post_alert(client, tokens["ml"], other_producer)
    assert denied.status_code == 403 and denied.json()["code"] == "PRODUCER_NOT_ALLOWED"
    assert (await post_alert(client, tokens["operator"])).status_code == 403
    created = (await post_alert(client, tokens["ml"])).json()
    own = await client.get(f"/api/v1/alerts/{created['alert_id']}", headers=tokens["ml"])
    assert own.status_code == 200, "생산자는 자기가 보낸 경보의 status_url을 조회할 수 있다"
    bad_token = await client.get(f"/api/v1/alerts/{created['alert_id']}", headers={"Authorization": "Bearer nope"})
    assert bad_token.status_code == 401
