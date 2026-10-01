"""경보 정규화 (normalizer_version = rsw-normalizer-v1).

작업이 고정한 지식 배포본으로 장비 구성·코드·관측·ML 결과를 해석해 alert_contexts와 자식 행을
만들고 한 트랜잭션에서 봉인한다. alert_contexts는 봉인 외 UPDATE가 금지되어 있으므로
(guard_sealable_record) 자식 UUID를 먼저 발급해 매핑 메타데이터(_integration)를 INSERT 시점에 넣는다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jsonutil import as_json, dumps, utc_iso
from app.db.sqlutil import fetch_all, fetch_one
from app.schemas.common import notice
from app.services import scenario_matcher

ML_TARGET_KIND = {"classification": "fault_type", "hypothesis": "hypothesis", "rule_trigger": "diagnostic_rule"}


class NormalizationError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class AssetResolution:
    asset_id: UUID | None
    asset_config_id: UUID | None
    verification_status: str  # unverified | verified | unresolved
    warnings: list[dict[str, Any]]


def decide_context_status(
    *,
    invalid: bool,
    asset_found: bool,
    config_verified: bool,
    code_statuses: list[str],
    observation_statuses: list[str],
    ml_mapped: list[bool],
) -> str:
    """context 해석 상태.

    - invalid: JSON 계약은 통과했지만 도메인상 해석할 수 없는 모순이 있다.
    - mapped: 검증된 장비 구성 + 모든 코드/관측/ML target 매핑 성공.
    - partial: 장비가 식별되었거나 일부 매핑이 있다.
    - unmapped: 원문은 보존했지만 유용한 매핑이 없다.
    """
    if invalid:
        return "invalid"
    all_mapped = (
        all(s == "mapped" for s in code_statuses)
        and all(s == "mapped" for s in observation_statuses)
        and all(ml_mapped)
    )
    if config_verified and all_mapped:
        return "mapped"
    any_mapped = (
        any(s == "mapped" for s in code_statuses)
        or any(s == "mapped" for s in observation_statuses)
        or any(ml_mapped)
    )
    if asset_found or any_mapped:
        return "partial"
    return "unmapped"


def value_matches_type(value: Any, value_type: str) -> bool:
    if value is None or value_type == "unknown":
        return True
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "string":
        return isinstance(value, str)
    return True


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def resolve_asset(session: AsyncSession, asset_key: str, occurred_at: datetime) -> AssetResolution:
    asset = await fetch_one(session, "SELECT id FROM rsw.assets WHERE asset_code = :code", code=asset_key)
    if asset is None:
        return AssetResolution(None, None, "unresolved", [
            notice("ASSET_NOT_REGISTERED", "등록되지 않은 장비 키입니다. 장비 구성을 확인해야 합니다.", "asset_key"),
        ])
    candidates = await fetch_all(
        session,
        """
        SELECT id, verification_status, effective_from FROM rsw.asset_configs
        WHERE asset_id = :asset_id AND effective_from <= :occurred_at
          AND effective_from = (SELECT max(effective_from) FROM rsw.asset_configs
                                WHERE asset_id = :asset_id AND effective_from <= :occurred_at)
        """,
        asset_id=asset["id"], occurred_at=occurred_at,
    )
    if len(candidates) != 1:
        message = ("경보 발생 시각에 유효한 장비 구성이 없습니다." if not candidates
                   else "경보 발생 시각에 유효한 장비 구성이 여러 개라 하나로 정할 수 없습니다.")
        return AssetResolution(asset["id"], None, "unresolved", [
            notice("ASSET_CONFIG_UNRESOLVED", message + " 장비 구성 정보를 확인해야 합니다.", "asset_key"),
        ])
    config = candidates[0]
    warnings = []
    if config["verification_status"] != "verified":
        warnings.append(notice("ASSET_CONFIG_UNVERIFIED", "장비 구성이 아직 검증되지 않았습니다. 절차 적용 판단이 제한됩니다.", "asset_key"))
    return AssetResolution(asset["id"], config["id"], config["verification_status"], warnings)


async def _entity_in_release(session: AsyncSession, release_id: UUID, kind: str, namespace: str, code: str) -> UUID | None:
    row = await fetch_one(
        session,
        """
        SELECT ke.id FROM rsw.knowledge_entities ke
        JOIN rsw.entity_versions ev ON ev.entity_id = ke.id AND ev.release_id = :rid
        WHERE ke.kind = :kind AND ke.namespace = :ns AND ke.local_code = :code AND ev.review_status = 'reviewed'
        """,
        rid=release_id, kind=kind, ns=namespace, code=code,
    )
    return row["id"] if row else None


async def normalize_alert(session: AsyncSession, job: dict[str, Any], clock_skew_seconds: int) -> UUID:
    """정규화를 수행하고 봉인된 context id를 반환한다. 호출자가 트랜잭션과 lease를 관리한다."""
    release_id: UUID = job["release_id"]
    config = as_json(job["config"]) or {}
    normalizer_version = config.get("normalizer_version", "unknown")
    matcher_version = config.get("matcher_version", "unknown")

    alert = await fetch_one(
        session,
        """
        SELECT id, reported_asset_key, occurred_at, received_at, parsed_payload
        FROM rsw.alert_events WHERE id = :id
        """,
        id=job["alert_id"],
    )
    if alert is None:
        raise NormalizationError("ALERT_MISSING", "정규화 대상 경보가 없습니다.")
    payload = as_json(alert["parsed_payload"])
    received_at: datetime = alert["received_at"]
    future_limit = received_at + timedelta(seconds=clock_skew_seconds)

    warnings: list[dict[str, Any]] = []
    invalid = False

    asset = await resolve_asset(session, alert["reported_asset_key"], alert["occurred_at"])
    warnings += asset.warnings
    if alert["occurred_at"] > future_limit:
        invalid = True
        warnings.append(notice("EVENT_TIME_IN_FUTURE", "경보 발생 시각이 수신 시각보다 미래입니다.", "occurred_at"))

    # ----- 관측 코드 -----
    code_rows, code_mappings, code_entity_ids = [], [], []
    for index, item in enumerate(payload.get("observed_codes", [])):
        row_id = uuid.uuid4()
        entity_id = await _entity_in_release(session, release_id, "alarm_code", item["namespace"], item["code"])
        status = "mapped" if entity_id else "unmapped"
        observed_at = _parse_time(item.get("observed_at"))
        if observed_at and observed_at > future_limit:
            invalid = True
            warnings.append(notice("EVENT_TIME_IN_FUTURE", "코드 관측 시각이 수신 시각보다 미래입니다.", f"observed_codes[{index}].observed_at"))
        if entity_id:
            code_entity_ids.append(entity_id)
        else:
            warnings.append(notice("CODE_UNMAPPED", f"코드 {item['namespace']}/{item['code']}에 대한 검토된 정의가 없습니다.", f"observed_codes[{index}]"))
        code_rows.append({"id": row_id, "raw_namespace": item["namespace"], "raw_code": item["code"],
                          "code_entity_id": entity_id, "mapping_status": status, "observed_at": observed_at})
        code_mappings.append({"id": str(row_id), "input_index": index, "entity_id": str(entity_id) if entity_id else None, "status": status})

    # ----- 관측값 -----
    obs_rows, obs_mappings = [], []
    for index, item in enumerate(payload.get("observations", [])):
        row_id = uuid.uuid4()
        binding = None
        if asset.asset_config_id is not None:
            binding = await fetch_one(
                session,
                """
                SELECT fb.id, fb.feature_id, fb.source_unit, fb.verification_status, fd.value_type, fd.unit AS feature_unit
                FROM rsw.feature_bindings fb
                JOIN rsw.feature_definitions fd ON fd.release_id = fb.release_id AND fd.entity_id = fb.feature_id
                JOIN rsw.entity_versions ev ON ev.release_id = fb.release_id AND ev.entity_id = fb.feature_id
                WHERE fb.release_id = :rid AND fb.asset_config_id = :acid AND fb.source_tag = :tag
                  AND ev.review_status = 'reviewed'
                """,
                rid=release_id, acid=asset.asset_config_id, tag=item["feature_key"],
            )
        field = f"observations[{index}]"
        observed_at = _parse_time(item.get("observed_at"))
        if observed_at and observed_at > future_limit:
            invalid = True
            warnings.append(notice("EVENT_TIME_IN_FUTURE", "관측 시각이 수신 시각보다 미래입니다.", f"{field}.observed_at"))
        status = "unmapped"
        if binding is None:
            warnings.append(notice("FEATURE_UNMAPPED", f"센서/특징 키 {item['feature_key']}의 검토된 연결 정보가 없습니다.", field))
        else:
            status = "mapped"
            if not value_matches_type(item.get("value"), binding["value_type"]):
                status = "invalid"
                invalid = True
                warnings.append(notice("OBSERVATION_TYPE_MISMATCH", f"{item['feature_key']} 값의 타입이 feature 정의({binding['value_type']})와 다릅니다.", f"{field}.value"))
            if binding["verification_status"] != "verified":
                warnings.append(notice("FEATURE_BINDING_UNVERIFIED", f"{item['feature_key']} 연결 정보가 아직 검증되지 않았습니다.", field))
            unit = item.get("unit")
            if binding["source_unit"] is not None and unit != binding["source_unit"]:
                warnings.append(notice("UNIT_MISMATCH", f"{item['feature_key']} 단위({unit})가 등록된 원시 단위({binding['source_unit']})와 다릅니다. 직접 비교하지 않습니다.", f"{field}.unit"))
        obs_rows.append({
            "id": row_id,
            "asset_config_id": asset.asset_config_id if binding else None,
            "binding_id": binding["id"] if binding else None,
            "raw_feature_key": item["feature_key"],
            "value": item.get("value"),
            "summary": item.get("summary"),
            "unit": item.get("unit"),
            "quality": item["quality"],
            "baseline": item.get("baseline"),
            "observed_at": observed_at,
            "raw_series_ref": item.get("raw_series_ref"),
        })
        obs_mappings.append({"id": str(row_id), "input_index": index,
                             "feature_id": str(binding["feature_id"]) if binding else None,
                             "binding_id": str(binding["id"]) if binding else None, "status": status})

    # ----- ML 결과 -----
    ml_rows, ml_mappings, ml_mapped = [], [], []
    fault_type_ids, rule_ids = [], []
    for index, item in enumerate(payload.get("ml_assessments", [])):
        row_id = uuid.uuid4()
        kind = item["kind"]
        target = item.get("target")
        target_kind = ML_TARGET_KIND.get(kind)
        target_id = None
        if target and target_kind:
            target_id = await _entity_in_release(session, release_id, target_kind, target["namespace"], target["code"])
            ml_mapped.append(target_id is not None)
            if target_id is None:
                warnings.append(notice("ML_TARGET_UNMAPPED", f"ML 결과 대상 {target['namespace']}/{target['code']}에 대한 검토된 정의가 없습니다.", f"ml_assessments[{index}].target"))
            elif target_kind == "fault_type":
                fault_type_ids.append(target_id)
            elif target_kind == "diagnostic_rule":
                rule_ids.append(target_id)
        score = item.get("score") or {}
        if score and score.get("calibration_status") != "calibrated":
            warnings.append(notice("ML_SCORE_UNCALIBRATED", "ML 점수는 보정되지 않았으므로 실제 발생 확률로 해석하지 않습니다.", f"ml_assessments[{index}].score"))
        ml_rows.append({
            "id": row_id, "producer_run_key": item.get("run_key"),
            "model_name": item["model"]["name"], "model_version": item["model"]["version"],
            "assessment_kind": kind,
            "raw_target_namespace": target["namespace"] if target else None,
            "raw_target_code": target["code"] if target else None,
            "target_entity_id": target_id, "target_kind": target_kind if target_id else None,
            "score_kind": score.get("kind"), "score_value": score.get("value"),
            "calibration_status": score.get("calibration_status", "unknown"),
            "calibration_ref": score.get("calibration_ref"),
            "details": item.get("details") or {}, "caveats": item.get("caveats") or [],
        })
        ml_mappings.append({"id": str(row_id), "input_index": index,
                            "target_entity_id": str(target_id) if target_id else None,
                            "target_kind": target_kind if target_id else None})

    status = decide_context_status(
        invalid=invalid, asset_found=asset.asset_id is not None,
        config_verified=asset.asset_config_id is not None and asset.verification_status == "verified",
        code_statuses=[r["mapping_status"] for r in code_rows],
        observation_statuses=[m["status"] for m in obs_mappings],
        ml_mapped=ml_mapped,
    )

    # ----- 시나리오 매칭 (실패해도 정규화는 성공으로 남긴다) -----
    related: dict[str, Any]
    try:
        async with session.begin_nested():
            items = await scenario_matcher.match_scenarios(
                session, release_id, asset.asset_config_id, code_entity_ids, fault_type_ids, rule_ids)
        related = {"status": "matched" if items else "no_match", "matcher_version": matcher_version,
                   "items": [_jsonable(i) for i in items], "error": None}
    except Exception as exc:  # noqa: BLE001 - 매칭 실패는 결과에 기록한다.
        related = {"status": "failed", "matcher_version": matcher_version, "items": [],
                   "error": {"code": "SCENARIO_MATCH_FAILED", "message": f"시나리오 매칭에 실패했습니다: {type(exc).__name__}", "retryable": True}}

    window = payload.get("observation_window") or None
    submitted_context = payload.get("operating_context") or {}
    integration = {
        "job_id": str(job["id"]),
        "normalizer_version": normalizer_version,
        "matcher_version": matcher_version,
        "asset": {
            "reported_key": alert["reported_asset_key"],
            "asset_id": str(asset.asset_id) if asset.asset_id else None,
            "asset_config_id": str(asset.asset_config_id) if asset.asset_config_id else None,
            "verification_status": asset.verification_status,
        },
        "code_mappings": code_mappings,
        "observation_mappings": obs_mappings,
        "ml_mappings": ml_mappings,
        "related_scenarios": related,
    }
    context_id = uuid.uuid4()
    await session.execute(*_sql(
        """
        INSERT INTO rsw.alert_contexts
          (id, alert_id, release_id, asset_config_id, normalizer_version, status,
           window_start, window_end, operating_context, warnings)
        VALUES (:id, :alert_id, :release_id, :acid, :nv, :status, :ws, :we,
                CAST(:oc AS jsonb), CAST(:warnings AS jsonb))
        """,
        id=context_id, alert_id=alert["id"], release_id=release_id, acid=asset.asset_config_id,
        nv=normalizer_version, status=status,
        ws=_parse_time(window["start"]) if window else None, we=_parse_time(window["end"]) if window else None,
        oc=dumps({**submitted_context, "_integration": integration}), warnings=dumps(warnings),
    ))
    for row in code_rows:
        await session.execute(*_sql(
            """
            INSERT INTO rsw.observed_codes (id, context_id, release_id, raw_namespace, raw_code, code_entity_id, mapping_status, observed_at)
            VALUES (:id, :cid, :rid, :ns, :code, :eid, :status, :observed_at)
            """,
            id=row["id"], cid=context_id, rid=release_id, ns=row["raw_namespace"], code=row["raw_code"],
            eid=row["code_entity_id"], status=row["mapping_status"], observed_at=row["observed_at"],
        ))
    for row in obs_rows:
        await session.execute(*_sql(
            """
            INSERT INTO rsw.observations
              (id, context_id, release_id, asset_config_id, binding_id, raw_feature_key, value, summary,
               unit, quality, baseline, observed_at, raw_series_ref)
            VALUES (:id, :cid, :rid, :acid, :bid, :key, CAST(:value AS jsonb), CAST(:summary AS jsonb),
                    :unit, :quality, CAST(:baseline AS jsonb), :observed_at, :ref)
            """,
            id=row["id"], cid=context_id, rid=release_id, acid=row["asset_config_id"], bid=row["binding_id"],
            key=row["raw_feature_key"],
            value=None if row["value"] is None else dumps(row["value"]),
            summary=None if row["summary"] is None else dumps(row["summary"]),
            unit=row["unit"], quality=row["quality"],
            baseline=None if row["baseline"] is None else dumps(row["baseline"]),
            observed_at=row["observed_at"], ref=row["raw_series_ref"],
        ))
    for row in ml_rows:
        await session.execute(*_sql(
            """
            INSERT INTO rsw.ml_assessments
              (id, context_id, release_id, producer_run_key, model_name, model_version, assessment_kind,
               raw_target_namespace, raw_target_code, target_entity_id, target_kind, score_kind, score_value,
               calibration_status, calibration_ref, details, caveats)
            VALUES (:id, :cid, :rid, :prk, :mn, :mv, :kind, :rtn, :rtc, :tid, :tkind, :sk, :sv,
                    :cs, :cref, CAST(:details AS jsonb), CAST(:caveats AS jsonb))
            """,
            id=row["id"], cid=context_id, rid=release_id, prk=row["producer_run_key"],
            mn=row["model_name"], mv=row["model_version"], kind=row["assessment_kind"],
            rtn=row["raw_target_namespace"], rtc=row["raw_target_code"], tid=row["target_entity_id"],
            tkind=row["target_kind"], sk=row["score_kind"],
            sv=None if row["score_value"] is None else float(row["score_value"]),
            cs=row["calibration_status"], cref=row["calibration_ref"],
            details=dumps(row["details"]), caveats=dumps(row["caveats"]),
        ))
    # 모든 자식 행을 넣은 뒤 봉인한다. 트리거가 sealed_at을 now()로 확정한다.
    await session.execute(*_sql("UPDATE rsw.alert_contexts SET sealed_at = now() WHERE id = :id", id=context_id))
    return context_id


def _jsonable(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "scenario_id": str(item["scenario_id"]), "relation_ids": [str(r) for r in item["relation_ids"]]}


def _sql(sql: str, **params: Any) -> tuple[Any, dict[str, Any]]:
    from sqlalchemy import text

    return text(sql), params


__all__ = ["normalize_alert", "decide_context_status", "value_matches_type", "NormalizationError", "utc_iso"]
