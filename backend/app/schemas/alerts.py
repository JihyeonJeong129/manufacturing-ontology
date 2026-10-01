"""경보 수신/조회 DTO. docs/design/api/openapi.json 의 AlertInput 계열과 일치시킨다."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.schemas.common import Notice, Output, Scalar, StrictInput, UtcDatetime, WorkError, finite

Str200 = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Str128 = Annotated[StrictStr, Field(min_length=1, max_length=128)]
Number = StrictInt | float  # strict 모드에서 float는 JSON 정수도 허용


class ObservationWindow(StrictInput):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> "ObservationWindow":
        if self.start > self.end:
            raise ValueError("observation_window.start는 end보다 늦을 수 없습니다.")
        return self


class CodeInput(StrictInput):
    namespace: Str200
    code: Str128
    observed_at: AwareDatetime | None = None


class ObservationSummary(StrictInput):
    sample_count: Annotated[StrictInt, Field(ge=0)] | None = None
    min: Number | None = None
    max: Number | None = None
    mean: Number | None = None
    stddev: Annotated[Number, Field(ge=0)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _non_empty(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data:
            raise ValueError("summary에는 적어도 한 필드가 필요합니다.")
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    raise ValueError(f"summary.{key}에 null은 허용되지 않습니다.")
        return data

    @model_validator(mode="after")
    def _consistent(self) -> "ObservationSummary":
        for name in ("min", "max", "mean", "stddev"):
            finite(getattr(self, name), name)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("summary.min은 max보다 클 수 없습니다.")
        if self.mean is not None:
            if self.min is not None and self.mean < self.min:
                raise ValueError("summary.mean은 min 이상이어야 합니다.")
            if self.max is not None and self.mean > self.max:
                raise ValueError("summary.mean은 max 이하여야 합니다.")
        return self


class ObservationInput(StrictInput):
    feature_key: Str200
    value: Scalar | None = None
    summary: ObservationSummary | None = None
    unit: Annotated[StrictStr, Field(max_length=64)] | None = None
    quality: Literal["valid", "missing", "suspect", "unknown"]
    observed_at: AwareDatetime | None = None
    baseline: dict[str, Any] | None = None
    raw_series_ref: Annotated[StrictStr, Field(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _valid_has_value(self) -> "ObservationInput":
        if isinstance(self.value, float):
            finite(self.value, "value")
        if self.quality == "valid" and self.value is None and self.summary is None:
            raise ValueError("quality='valid'이면 null이 아닌 value 또는 summary가 필요합니다.")
        return self


class ModelIdentity(StrictInput):
    name: Str200
    version: Str200


class MlTarget(StrictInput):
    namespace: Str200
    code: Str128


class MlScore(StrictInput):
    kind: Literal["probability", "logit", "anomaly_score", "rule_score", "other"]
    value: Number
    calibration_status: Literal["unknown", "uncalibrated", "calibrated"]
    calibration_ref: Annotated[StrictStr, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def _score_rules(self) -> "MlScore":
        finite(self.value, "score.value")
        if self.kind == "probability" and not (0 <= self.value <= 1):
            raise ValueError("probability 점수는 0 이상 1 이하여야 합니다.")
        if self.calibration_status == "calibrated":
            if self.kind != "probability":
                raise ValueError("calibrated 점수는 probability 종류여야 합니다.")
            if not self.calibration_ref:
                raise ValueError("calibrated 점수에는 calibration_ref가 필요합니다.")
        return self


class MlAssessmentInput(StrictInput):
    kind: Literal["classification", "hypothesis", "rule_trigger", "anomaly"]
    model: ModelIdentity
    run_key: Annotated[StrictStr, Field(max_length=200)] | None = None
    target: MlTarget | None = None
    score: MlScore | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    caveats: Annotated[list[Annotated[StrictStr, Field(max_length=500)]], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _target_required(self) -> "MlAssessmentInput":
        if self.kind in ("classification", "hypothesis", "rule_trigger") and self.target is None:
            raise ValueError(f"kind='{self.kind}'에는 namespace와 code를 가진 target이 필요합니다.")
        return self


class OperatingContext(StrictInput):
    cycle_state: Literal["welding", "non_welding", "unknown"] | None = None
    workpiece_type: Annotated[StrictStr, Field(max_length=200)] | None = None
    shift: Annotated[StrictStr, Field(max_length=100)] | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _no_null_cycle_state(cls, data: Any) -> Any:
        if isinstance(data, dict) and "cycle_state" in data and data["cycle_state"] is None:
            raise ValueError("cycle_state에 null은 허용되지 않습니다. 모르면 'unknown'을 사용합니다.")
        return data


class AlertInput(StrictInput):
    schema_version: Literal["1.0"]
    producer_schema_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    event_id: Str128
    producer_key: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    asset_key: Str128
    occurred_at: AwareDatetime
    observation_window: ObservationWindow | None = None
    observed_codes: Annotated[list[CodeInput], Field(max_length=64)]
    observations: Annotated[list[ObservationInput], Field(max_length=128)]
    ml_assessments: Annotated[list[MlAssessmentInput], Field(max_length=64)]
    operating_context: OperatingContext | None = None

    @field_validator("operating_context", mode="before")
    @classmethod
    def _context_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("operating_context는 생략할 수 있지만 null은 허용되지 않습니다.")
        return value


# ----------------------------- 응답 -----------------------------


class Processing(Output):
    status: Literal["queued", "running", "succeeded", "failed"]
    updated_at: UtcDatetime
    error: WorkError | None


class AlertAccepted(Output):
    alert_id: UUID
    event_id: str
    producer_key: str
    received_at: UtcDatetime
    duplicate: bool
    normalization: Processing
    status_url: str


class AssetResolution(Output):
    reported_key: str
    asset_id: UUID | None
    asset_config_id: UUID | None
    verification_status: Literal["unverified", "verified", "unresolved"]


class CodeMapping(Output):
    id: UUID
    input_index: Annotated[int, Field(ge=0)]
    entity_id: UUID | None
    status: Literal["mapped", "unmapped", "ambiguous"]


class ObservationMapping(Output):
    id: UUID
    input_index: Annotated[int, Field(ge=0)]
    feature_id: UUID | None
    binding_id: UUID | None
    status: Literal["mapped", "unmapped", "invalid"]


class MlMapping(Output):
    id: UUID
    input_index: Annotated[int, Field(ge=0)]
    target_entity_id: UUID | None
    target_kind: Literal["fault_type", "hypothesis", "diagnostic_rule"] | None


class AlertContext(Output):
    context_id: UUID
    knowledge_release_id: UUID
    knowledge_version: str
    normalizer_version: str
    status: Literal["mapped", "partial", "unmapped", "invalid"]
    asset: AssetResolution
    code_mappings: list[CodeMapping]
    observation_mappings: list[ObservationMapping]
    ml_mappings: list[MlMapping]
    warnings: list[Notice]
    sealed_at: UtcDatetime


class ScenarioMatch(Output):
    scenario_id: UUID
    namespace: str
    code: str
    label: str
    match_methods: Annotated[
        list[Literal["observed_code", "ml_classification", "rule_trigger", "pattern", "operating_context"]],
        Field(min_length=1),
    ]
    relation_ids: list[UUID]
    explanation: str


class ScenarioMatches(Output):
    status: Literal["not_evaluated", "matched", "no_match", "failed"]
    matcher_version: str | None
    items: Annotated[list[ScenarioMatch], Field(max_length=10)]
    error: WorkError | None


class AlertDetails(Output):
    alert_id: UUID
    received_at: UtcDatetime
    submitted_event: dict[str, Any]
    normalization: Processing
    context: AlertContext | None
    related_scenarios: ScenarioMatches
