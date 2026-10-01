"""10개 상황의 ML 경보 입력과 동작 검증 케이스.

경보 본문은 ML 핸드오프를 연동 API 계약(AlertInput)으로 옮긴 형태다. 어댑터 규칙:
  fault_class.code        -> ml_assessments[kind=classification] target wang2024.fault_class/E0x
  hypotheses[].id (P1~P9) -> ml_assessments[kind=rule_trigger]   target rsw.ml.rule/Px
  trigger.rule_code       -> observed_codes                        wang2024.error_code/E0xx
  sensor_findings[]       -> observations (feature_key = c1~c18)
관측값 숫자는 논문 데이터의 실제 행이 아니라 방향(평소 대비 높음/낮음)을 보여 주는 시연 입력이다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.mvp.catalog_knowledge import NS_ERROR, NS_FAULT, NS_RULE

PRODUCER_KEY = "rsw-ml"
ASSET_CODE = "RSW-GUN-01"
PRODUCER_SCHEMA_VERSION = "rsw-ml-handoff-0.1"
ML_MODEL = {"name": "rsw-fault-predictor", "version": "handoff-0.1"}
CAVEATS = ["센서 패턴은 학습 데이터로 확인되지 않은 점검 후보다.", "점수는 보정되지 않았다."]


@dataclass(frozen=True)
class Case:
    key: str                          # S01~S10, NL-S09
    scenario: str                     # 기대 상황 ID
    kind: str                         # ml_error_code | natural_language
    question: str
    codes: tuple[str, ...] = ()
    fault: str | None = None
    rules: tuple[str, ...] = ()
    observations: tuple[tuple[str, float, float], ...] = ()   # (센서, 값, 평소 평균)
    anomaly_score: float | None = None
    expected_sections: dict[str, int] = field(default_factory=dict)   # 절 → 관련도(1~3)
    expected_methods: tuple[str, ...] = ()
    fault_probability: float = 0.85


CASES: list[Case] = [
    Case("S01", "S01", "ml_error_code", "E012 떴는데 뭐부터 봐야 돼?", ("E012",), "E01", ("P1",),
         (("c5", 3.1, 4.2), ("c4", 410.0, 260.0), ("c13", 4.2, 4.2)),
         expected_sections={"D-S01": 3, "F-10.1-12": 3, "F-5.3": 2, "F-8.11": 2},
         expected_methods=("observed_code", "ml_classification", "rule_trigger"), fault_probability=0.91),
    Case("S02", "S02", "ml_error_code", "캡 교체 주기가 언제야?", ("E016",), "E02", ("P5", "P6"),
         (("c3", -1.8, 0.0), ("c1", 2.6, 0.4)),
         expected_sections={"D-S02": 3, "F-8.8": 2, "F-9.14.8": 2},
         expected_methods=("observed_code", "ml_classification", "rule_trigger")),
    Case("S03", "S03", "ml_error_code", "E028은 무슨 뜻이야?", ("E028",), "E03", ("P5",),
         (("c3", 14.0, 2.0), ("c7", 118.0, 125.0), ("c15", 2.0, 2.0)),
         expected_sections={"D-S03": 3, "F-9.9": 2, "F-8.5": 2},
         expected_methods=("observed_code", "ml_classification", "rule_trigger")),
    Case("S04", "S04", "ml_error_code", "잠금 진단은 어떻게 돌려?", ("E029",), "E04", ("P4",),
         (("c3", 7.5, 0.2),),
         expected_sections={"D-S04": 3, "F-9.14.10": 3, "F-11.3": 2},
         expected_methods=("observed_code", "ml_classification", "rule_trigger")),
    Case("S05", "S05", "ml_error_code", "용접 힘이 약한 것 같은데 어디부터 봐?", (), None, ("P2",),
         (("c2", 2.9, 3.6), ("c5", 3.4, 4.2), ("c14", 3.6, 3.6)),
         expected_sections={"D-S05": 3, "M-4": 2, "F-6": 2}, expected_methods=("rule_trigger",)),
    Case("S06", "S06", "ml_error_code", "마찰값이 계속 오르는데 뭐가 문제야?", (), None, ("P3",),
         (("c6", 0.62, 0.35), ("c2", 3.1, 3.6), ("c5", 4.2, 4.2)),
         expected_sections={"D-S06": 3, "M-4": 2, "F-6": 2}, expected_methods=("rule_trigger",)),
    Case("S07", "S07", "ml_error_code", "캡이 빨리 닳는 이유가 뭐야?", (), None, ("P6",),
         (("c1", 1.9, 0.5), ("c11", 812000.0, 400000.0)),
         expected_sections={"D-S07": 3, "F-9.14.9": 2, "M-9-1": 2}, expected_methods=("rule_trigger",)),
    Case("S08", "S08", "ml_error_code", "정비하고 나서 위치가 안 맞아. 보정 다시 해야 돼?", (), None, ("P5",),
         (("c3", 3.2, 0.1), ("c7", 121.0, 125.0)),
         expected_sections={"D-S08": 3, "F-9.9": 2, "F-8.5": 2}, expected_methods=("rule_trigger",)),
    Case("S09", "S09", "ml_error_code", "건은 멀쩡한데 왜 라인이 멈췄어?", (), None, ("P9",),
         (("c2", 3.6, 3.6), ("c5", 4.2, 4.2)),
         expected_sections={"D-S09": 3, "M-9-1": 2, "Y-1.6.6": 2}, expected_methods=("rule_trigger",)),
    Case("S10", "S10", "ml_error_code", "이 경보 진짜야, 아니면 설정 바뀐 거야?", ("E003", "E011", "E029"), None, ("P7", "P8"),
         (("c16", 1.6, 1.2),),
         expected_sections={"D-S10": 3, "A-2.1.2": 2, "F-9.15": 2}, expected_methods=("rule_trigger",)),
    # 자연어 질의: ML은 분류하지 못한 이상 점수만 보냈고, 작업자가 본 상황을 문장으로 묻는다.
    Case("NL-S09", "S09", "natural_language", "라인은 멈췄는데 건 자체는 정상으로 움직여요. 뭘 먼저 확인해야 하나요?",
         observations=(("c2", 3.6, 3.6), ("c5", 4.2, 4.2)), anomaly_score=0.71,
         expected_sections={"D-S09": 3, "M-9-1": 2, "Y-1.6.6": 2, "A-2.1.6": 1}),
]
BY_KEY = {c.key: c for c in CASES}
DEMO_ML_CASE = "S01"
DEMO_NL_CASE = "NL-S09"


def alert_payload(case: Case, *, event_id: str | None = None, occurred_at: str = "2026-09-29T01:00:00Z",
                  cycle_state: str = "welding") -> dict[str, Any]:
    ml: list[dict[str, Any]] = []
    if case.fault:
        ml.append({"kind": "classification", "model": ML_MODEL, "target": {"namespace": NS_FAULT, "code": case.fault},
                   "score": {"kind": "probability", "value": case.fault_probability, "calibration_status": "uncalibrated"},
                   "caveats": CAVEATS})
    for rule in case.rules:
        ml.append({"kind": "rule_trigger", "model": ML_MODEL, "target": {"namespace": NS_RULE, "code": rule},
                   "details": {"sensor_findings": [{"sensor": s, "value": v, "baseline_mean": b}
                                                   for s, v, b in case.observations]},
                   "caveats": CAVEATS[:1]})
    if case.anomaly_score is not None:
        ml.append({"kind": "anomaly", "model": ML_MODEL,
                   "score": {"kind": "anomaly_score", "value": case.anomaly_score, "calibration_status": "uncalibrated"},
                   "details": {"note": "라인 정지 전후 이상 점수. 고장 분류와 센서 규칙은 발화하지 않았다."},
                   "caveats": CAVEATS})
    return {
        "schema_version": "1.0",
        "producer_schema_version": PRODUCER_SCHEMA_VERSION,
        "event_id": event_id or f"rsw-{case.key.lower()}-{uuid.uuid4().hex[:10]}",
        "producer_key": PRODUCER_KEY,
        "asset_key": ASSET_CODE,
        "occurred_at": occurred_at,
        "observation_window": {"start": "2026-09-29T00:50:00Z", "end": occurred_at},
        "observed_codes": [{"namespace": NS_ERROR, "code": code, "observed_at": occurred_at} for code in case.codes],
        "observations": [{"feature_key": sensor, "value": value, "unit": None, "quality": "valid",
                          "observed_at": occurred_at, "baseline": {"mean": baseline, "basis": "해당 건의 평소 값"}}
                         for sensor, value, baseline in case.observations],
        "ml_assessments": ml,
        "operating_context": {"cycle_state": cycle_state},
    }
