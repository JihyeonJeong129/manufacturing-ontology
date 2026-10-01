"""RSW MVP 지식 카탈로그: 개념, 관계, 절차, 수치, 충돌.

출처는 'RSW 용접건 MVP 오류 상황 정의서'(2026-09-25)와 매뉴얼 5종, 'RSW용접건 매뉴얼 RAG 활용정리'다.
ID는 정의서 요청대로 ML 핸드오프와 같은 값을 쓴다.
  고장 분류 E01~E04 (wang2024.fault_class)  <- ML fault_class.code
  종료/에러 코드 E012 등 (wang2024.error_code) <- ML trigger.rule_code
  센서 해석 규칙 P1~P9 (rsw.ml.rule)          <- ML hypotheses[].id 를 rule_trigger로 전달
  센서 태그 c1~c18 (wang2024.feature)          <- ML sensor_findings[].sensor
관계의 근거는 모두 원문 span("절#span 이름")으로 연결한다.
"""
from __future__ import annotations

from app.mvp.model import (EntityDef, FeatureDef, PatternDef, ProcedureDef, RelationDef, ScenarioDef, SpecDef,
                           StepDef)

NS_FAULT = "wang2024.fault_class"
NS_ERROR = "wang2024.error_code"
NS_FESTO_ERROR = "festo.cpx-sb-lt-weld-sa.error"
NS_FEATURE = "wang2024.feature"
NS_SCENARIO = "rsw.mvp.situation"
NS_RULE = "rsw.ml.rule"
NS_PATTERN = "rsw.ml.pattern"
NS_CAUSE = "rsw.mvp.cause"
NS_COMPONENT = "rsw.mvp.component"
NS_PROCEDURE = "rsw.mvp.procedure"
NS_SPEC = "rsw.mvp.spec"

# ---------------------------------------------------------------- 고장 분류 (논문 라인 정지 고장 4종)
FAULTS = [
    EntityDef("fault:E01", "fault_type", NS_FAULT, "E01", "보정 압력 도달 지연", "설정한 보정 압력에 1800 ms 안에 도달하지 못한 라인 정지 고장 (논문 고장 분류).",
              "Counterbalance timeout", ("보정 압력 시간 초과", "Counterbalance timeout")),
    EntityDef("fault:E02", "fault_type", NS_FAULT, "E02", "전극 파손", "전극 위치가 기준 이동의 영점보다 작아진 라인 정지 고장. 캡이 빠지거나 깨져 더 많이 들어간 경우 (논문 고장 분류).",
              "Electrode broke", ("전극 캡 파손", "Electrode broke")),
    EntityDef("fault:E03", "fault_type", NS_FAULT, "E03", "원치 않는 이동", "실제 위치가 실린더 스트로크의 6.5% 넘게 목표에서 벗어난 라인 정지 고장 (논문 고장 분류).",
              "Unwanted movement", ("위치 이탈", "Unwanted movement")),
    EntityDef("fault:E04", "fault_type", NS_FAULT, "E04", "드리프트", "잠긴 실린더가 멈추지 않고 분당 5 mm 넘게 움직인 라인 정지 고장 (논문 고장 분류).",
              "Drift", ("미세 이동", "Drift")),
]

# ---------------------------------------------------------------- 에러 코드
ERROR_CODES = [
    EntityDef("code:E012", "alarm_code", NS_ERROR, "E012", "종료 코드 E012", "논문 데이터의 종료 에러 코드. E01(보정 압력 도달 지연) 고장 10분 안에 18/18건 나타났다.",
              aliases=("E012 경보",)),
    EntityDef("code:E016", "alarm_code", NS_ERROR, "E016", "종료 코드 E016", "논문 데이터의 종료 에러 코드. E02(전극 파손) 고장에서 17/18건 나타났다.",
              aliases=("E016 경보",)),
    EntityDef("code:E028", "alarm_code", NS_ERROR, "E028", "종료 코드 E028", "논문 데이터의 종료 에러 코드. E03(원치 않는 이동) 고장에서 18/18건 나타났다.",
              aliases=("E028 경보",)),
    EntityDef("code:E029", "alarm_code", NS_ERROR, "E029", "종료 코드 E029", "논문 데이터의 종료 에러 코드. E04(드리프트) 고장에서 18/18건 나타났지만, 다른 고장 건에서도 며칠 전부터 떠 있는 경우가 많아 오경보가 가장 많다.",
              aliases=("E029 경보",)),
    EntityDef("code:E003", "alarm_code", NS_ERROR, "E003", "잔고장 코드 E003", "논문 데이터의 잔고장 코드. 의미는 논문에 설명이 없어 모른다. 여러 날 떠 있기도 한다.",
              metadata={"meaning_known": False}),
    EntityDef("code:E011", "alarm_code", NS_ERROR, "E011", "잔고장 코드 E011", "논문 데이터의 잔고장 코드. 의미는 논문에 설명이 없어 모른다. 여러 날 떠 있기도 한다.",
              metadata={"meaning_known": False}),
]

# Festo 제어기 자체 오류 번호. 데이터셋 코드와 문자열이 비슷해도 다른 체계라 매핑하지 않는다.
FESTO_ERRORS = [
    ("1", "측정 시스템 오류", "Measuring system error", "F-10.1-1"),
    ("3", "압력 공급 없음", "No pressure supply", "F-10.1-3"),
    ("4", "최대 힘 초과", "Maximum force exceeded", "F-10.1-4"),
    ("6", "위치 도달 불가", "Position not reachable", "F-10.1-6"),
    ("9", "위치 결정 시간 초과", "Positioning timeout", "F-10.1-9"),
    ("10", "힘 도달 시간 초과", "Force timeout", "F-10.1-10"),
    ("11", "잠금 진단 기능 오류", "Diagnostic function", "F-10.1-11"),
    ("12", "이퀄라이저 시간 초과", "Timeout equalizer", "F-10.1-12"),
    ("14", "최대 마찰 초과", "Max. friction exceeded", "F-10.1-14"),
    ("17", "변압기 과열", "Over-temperature transformer", "F-10.1-17"),
    ("28", "캡 드레싱 오류", "Cap dressing", "F-10.1-28"),
    ("29", "캡 마모", "Cap abrasion", "F-10.1-29"),
    ("30", "밀링 불량", "Milling not OK", "F-10.1-30"),
    ("31", "캡 이탈", "Cap loss", "F-10.1-31"),
]
FESTO_CODES = [
    EntityDef(f"festo:{n}", "alarm_code", NS_FESTO_ERROR, n, f"Festo 오류 {n} {ko}",
              f"Festo CPX-SB-LT-WELD-SA 제어기 오류 {n} ({en}). 논문 데이터셋의 에러 코드와는 다른 체계다.", en)
    for n, ko, en, _ in FESTO_ERRORS
]

# ---------------------------------------------------------------- 센서 (c1~c18)
_FEATURES = [
    ("c1", "캡 오프셋", "measurement", "D-COMPONENTS#cap"),
    ("c2", "전극 힘", "measurement", "D-COMPONENTS#main_cylinder"),
    ("c3", "전극 위치", "measurement", "D-COMPONENTS#main_cylinder"),
    ("c4", "힘 형성 시간", "measurement", "D-COMPONENTS#main_cylinder"),
    ("c5", "보정 압력", "measurement", "D-COMPONENTS#compensating_cylinder"),
    ("c6", "마찰", "measurement", "D-COMPONENTS#gun_arm"),
    ("c7", "최대 열림 폭", "measurement", "R-SCOPE#data"),
    ("c8", "최대 전극 힘", "measurement", "R-SCOPE#data"),
    ("c9", "시작 마찰", "measurement", "D-COMPONENTS#gun_arm"),
    ("c10", "US2 (작동 허용)", "state", "D-COMPONENTS#controller"),
    ("c11", "용접 횟수", "counter", "R-SCOPE#data"),
    ("c12", "위치 이동 횟수", "counter", "R-SCOPE#data"),
    ("c13", "보정 압력 설정값", "setpoint", "D-COMPONENTS#setpoints"),
    ("c14", "전극 힘 설정값", "setpoint", "D-COMPONENTS#setpoints"),
    ("c15", "위치 설정값", "setpoint", "D-COMPONENTS#setpoints"),
    ("c16", "판 두께 설정값", "setpoint", "D-COMPONENTS#setpoints"),
    ("c17", "속도 설정값", "setpoint", "D-COMPONENTS#setpoints"),
    ("c18", "힘 형성 설정값", "setpoint", "D-COMPONENTS#setpoints"),
]
FEATURES = [
    FeatureDef(
        EntityDef(f"feature:{tag}", "feature", NS_FEATURE, tag, f"{tag} {name}",
                  f"논문 데이터셋(Wang et al., 2024) 변수 {tag}: {name}. 센서 이름이 마스킹되어 단위는 확인되지 않았다."
                  + (" 0 이하이면 용접이 아니라 캡 교체·드레싱 같은 비용접 작업이다." if tag == "c16" else ""),
                  aliases=(name,), metadata={"definition_source": source, "actual_or_setpoint": "setpoint" if kind == "setpoint" else "actual"}),
        kind, "unknown" if kind == "state" else "number", tag)
    for tag, name, kind, source in _FEATURES
]
FEATURE_SOURCES = {f"feature:{tag}": source for tag, _, _, source in _FEATURES}

# ---------------------------------------------------------------- 부품
_COMPONENTS = [
    ("welding_gun", "서보 공압식 RSW 용접건", None, "MVP 장비 전체. 로봇 팔 끝에서 철판 두 장을 점용접한다."),
    ("main_cylinder", "주 실린더", "welding_gun", "움직이는 전극을 밀어 철판을 가압한다. 잠금(blocking) 밸브로 위치를 고정한다."),
    ("blocking_valves", "잠금(차단) 밸브 1V2·1V3·1V4", "main_cylinder", "주 실린더 연결을 차단해 위치를 고정하는 밸브."),
    ("position_sensor", "위치 센서(변위 센서)", "main_cylinder", "전극·실린더 위치를 측정하는 센서."),
    ("compensating_cylinder", "보정 실린더(이퀄라이저)", "welding_gun", "건의 무게를 상쇄하고 고정측 전극을 철판에 부드럽게 붙인다."),
    ("mpye_valve", "비례 방향 제어 밸브(MPYE)", "welding_gun", "주 실린더의 위치·힘을 제어한다."),
    ("mpyd_valve", "압력 조절 밸브(MPYD)", "welding_gun", "보정 실린더 압력을 조절한다."),
    ("electrode_cap", "전극 캡", "welding_gun", "철판에 닿는 소모품. 드레싱으로 다듬고 주기적으로 교체한다."),
    ("gun_arm", "건 암·회전축·베어링", "welding_gun", "전극을 지지하고 움직인다."),
    ("controller", "제어기", "welding_gun", "로봇과 신호를 주고받고 에러 코드를 낸다."),
    ("peripheral", "주변 설비", None, "공기 공급·필터, 냉각수, 변압기, 용접 제어기."),
    ("air_supply", "공기 공급·필터", "peripheral", "압축 공기 공급 배관, 필터, 레귤레이터."),
    ("cooling_water", "냉각수 계통", "peripheral", "전극 암·캡과 변압기를 식히는 냉각수."),
    ("transformer", "용접 변압기", "peripheral", "용접 전류를 공급하는 변압기."),
    ("weld_controller", "용접 제어기(타이머)", "peripheral", "통전을 제어하고 용접 오류를 낸다."),
]
COMPONENTS = [
    EntityDef(f"comp:{key}", "component_type", NS_COMPONENT, key, name, desc,
              metadata={"parent": f"{NS_COMPONENT}/{parent}" if parent else None})
    for key, name, parent, desc in _COMPONENTS
]

# ---------------------------------------------------------------- ML 센서 해석 규칙과 증상 패턴 (P1~P9)
# 정의서는 P1~P9의 개별 정의를 주지 않고 상황과 짝지은 데이터 단서만 준다. 이름은 그 단서에서 옮겼다.
_P = [
    ("P1", "보정 압력 저하 + 힘 형성 시간 증가", "sensor",
     {"signals": [{"feature": "c5", "direction": "low", "reference": "c13"}, {"feature": "c4", "direction": "high"}]}, "D-SUMMARY#S01"),
    ("P2", "전극 힘 저하 + 보정 압력 저하", "sensor",
     {"signals": [{"feature": "c2", "direction": "low", "reference": "c14"}, {"feature": "c5", "direction": "low"}]}, "D-SUMMARY#S05"),
    ("P3", "마찰 증가 + 전극 힘 저하 (보정 압력 정상)", "sensor",
     {"signals": [{"feature": "c6", "direction": "high"}, {"feature": "c2", "direction": "low"},
                  {"feature": "c5", "direction": "normal"}]}, "D-SUMMARY#S06"),
    ("P4", "정지 중 전극 위치 변동", "sensor",
     {"signals": [{"feature": "c3", "direction": "unstable", "while": "locked_stationary"}]}, "D-SUMMARY#S04"),
    ("P5", "전극 위치 이탈 + 최대 열림 폭 변화", "sensor",
     {"signals": [{"feature": "c3", "direction": "deviation", "reference": "c15"}, {"feature": "c7", "direction": "change"}]}, "D-SUMMARY#S08"),
    ("P6", "캡 오프셋 변화 + 용접 횟수 누적", "sensor",
     {"signals": [{"feature": "c1", "direction": "change"}, {"feature": "c11", "direction": "accumulating"}]}, "D-SUMMARY#S07"),
    ("P7", "용접 설정값 변경", "setpoint_change",
     {"signals": [{"feature": f"c{i}", "direction": "change"} for i in range(13, 19)]}, "D-SUMMARY#S10"),
    ("P8", "최근 에러 상태 비율 증가", "error_history",
     {"signals": [{"metric": "error_state_ratio", "direction": "high", "window_minutes": 10}]}, "D-SUMMARY#S10"),
    ("P9", "건 센서 정상인데 라인 경보 발생", "external_context",
     {"signals": [{"feature": "gun_sensors", "direction": "normal"}, {"event": "line_alarm", "present": True}]}, "D-SUMMARY#S09"),
]
RULES = [
    EntityDef(f"rule:{code}", "diagnostic_rule", NS_RULE, code, f"ML 규칙 {code}: {label}",
              f"ML 핸드오프의 센서 해석 규칙 {code}. 규칙이 발화하면 연결된 상황을 후보로 올린다. 규칙 이름은 정의서 요약표의 데이터 단서에서 옮겼고 ML 원문 정의와 대조가 필요하다.",
              metadata={"detects_pattern": f"{NS_PATTERN}/{code}", "ml_handoff_field": "hypotheses[].id"})
    for code, label, *_ in _P
]
PATTERNS = [
    PatternDef(EntityDef(f"pattern:{code}", "evidence_pattern", NS_PATTERN, code, f"증상 {code}: {label}",
                         f"센서 패턴 {code}. ↑↓는 그 건의 평소 값 대비 방향이며 데이터로 확인되지 않은 점검 후보다.",
                         metadata={"summary_row": source}),
               kind, conditions,
               baseline_definition={"kind": "per_asset_usual_value", "description": "그 건의 평소 값 대비"},
               window_definition={"lookback_minutes": 10} if code == "P8" else None)
    for code, label, kind, conditions, source in _P
]

# ---------------------------------------------------------------- 원인 후보 (정의서 상황별 '원인 후보'를 중복 없이 모음)
_CAUSES = [
    ("H01", "공급 압력 부족", "보정 장치로 가는 공기 공급 압력이 부족하다."),
    ("H02", "압력 조절 밸브(MPYD)·케이블 불량", "보정 실린더 압력을 조절하는 MPYD 밸브나 케이블이 불량이다."),
    ("H03", "보정 실린더 배관 누설", "보정 실린더 배관에서 공기가 샌다."),
    ("H04", "보정 실린더 불량", "보정 실린더 자체가 불량이다."),
    ("H05", "전극 캡 과마모", "전극 캡이 수명 이상으로 닳았다."),
    ("H06", "드레싱 조건 부적절 (과다·주기·양)", "캡 드레싱이 과하거나 주기·양이 맞지 않는다."),
    ("H07", "캡 장착 불량", "전극 캡이 제대로 장착되지 않았다."),
    ("H08", "캡 교체 후 기준 위치 미재설정", "캡을 바꾼 뒤 위치 기준을 다시 잡지 않았다."),
    ("H09", "이동 범위 간섭 (장애물·지그·제품·배관·케이블)", "건 이동 범위 안에 장애물, 지그·제품, 배관·케이블이 간섭한다."),
    ("H10", "축 보정 틀어짐·미실시", "축 보정(영점·변환비)이 틀어졌거나 수행되지 않았다."),
    ("H11", "비례 방향 제어 밸브(MPYE) 응답 불량", "주 실린더를 제어하는 MPYE 밸브나 구동부 응답이 불량하다."),
    ("H12", "기계 걸림 (건 암·회전축)", "건 암·회전축이 걸려 자유롭게 움직이지 않는다."),
    ("H13", "회전축(fulcrum)·링크 부싱 마모", "회전축이나 링크의 부싱·핀이 닳아 유격이 커졌다."),
    ("H14", "이퀄라이저 슬라이드 걸림", "보정 장치(이퀄라이저) 슬라이드가 걸린다."),
    ("H15", "윤활 부족", "주 실린더·구동부 윤활이 부족하다."),
    ("H16", "잠금(차단) 밸브 누설", "주 실린더를 고정하는 잠금(차단) 밸브에서 누설이 있다."),
    ("H17", "주 실린더 내부 누설", "주 실린더 내부에서 공기가 샌다."),
    ("H18", "호스·피팅·밸브 누설", "공기 호스, 피팅, 밸브에서 공기가 샌다."),
    ("H19", "필터 막힘", "공기 필터가 막혀 공급이 부족하다."),
    ("H20", "배관 길이·구경 부족", "공기 배관이 너무 길거나 구경이 작다."),
    ("H21", "냉각 부족으로 캡 과열", "캡 냉각수가 부족해 캡이 과열된다."),
    ("H22", "위치 센서 문제", "위치 센서 측정이 잘못되었다."),
    ("H23", "냉각수 유량·온도 이상", "냉각수 유량이나 입구 온도가 기준을 벗어났다."),
    ("H24", "변압기 과열", "용접 변압기가 과열되었다."),
    ("H25", "용접 제어기 오류", "용접 제어기(타이머)에서 오류가 났다."),
    ("H26", "공정 잠금·인터록", "공정 잠금이나 인터록 때문에 라인이 멈췄다."),
    ("H27", "의도한 레시피 변경 (고장 아님)", "차종·판 두께 변경 등으로 설정값을 의도적으로 바꿨다. 고장이 아니다."),
    ("H28", "통신·센서 일시 불량", "통신이나 센서가 잠깐 불량했다."),
    ("H29", "반복 잔고장", "자잘한 에러가 반복해서 떴다 사라진다."),
]
CAUSES = [EntityDef(f"cause:{code}", "hypothesis", NS_CAUSE, code, label,
                    desc + " 원인 후보이며 점검으로 확인되기 전에는 확정 원인이 아니다.")
          for code, label, desc in _CAUSES]

# ---------------------------------------------------------------- 상황 10개
_SCENARIOS = [
    ("S01", "S01 보정 압력 도달 지연", "fault_response", "라인 정지 고장", "에러 E012, c5↓ c4↑", "E01, P1", "festo",
     ("E012 떴는데 뭐부터 봐야 돼?", "보정 압력 정상 범위가 얼마야?", "공급 압력은 정상인데 왜 안 올라가?")),
    ("S02", "S02 전극 파손", "fault_response", "라인 정지 고장", "에러 E016, c3·c1 이상", "E02, P5·P6", "festo",
     ("캡 교체 주기가 언제야?", "캡 갈고 나서 기준은 어떻게 다시 잡아?")),
    ("S03", "S03 원치 않는 이동", "fault_response", "라인 정지 고장", "에러 E028, c3 이탈", "E03, P5", "festo",
     ("E028은 무슨 뜻이야?", "축 보정은 어떻게 다시 해?")),
    ("S04", "S04 드리프트", "fault_response", "라인 정지 고장", "에러 E029, 정지 중 c3 변동", "E04, P4", "festo",
     ("잠금 진단은 어떻게 돌려?", "E029가 계속 떠 있는데 지금 멈춰야 해?")),
    ("S05", "S05 공기 공급 부족·누설로 전극 힘 저하", "cause_exploration", "원인 상황", "c2↓ c5↓", "P2 → E01", "festo",
     ("용접 힘이 약한 것 같은데 어디부터 봐?", "필터 교체 주기는?")),
    ("S06", "S06 기계 마찰·걸림·윤활 부족", "cause_exploration", "원인 상황", "c6↑ c2↓", "P3 → E03", "milco",
     ("마찰값이 계속 오르는데 뭐가 문제야?", "윤활은 어디에 무슨 그리스로 해?")),
    ("S07", "S07 전극 캡 마모·드레싱 불량", "cause_exploration", "원인 상황", "c1 변화, c11 누적", "P6 → E02", "festo",
     ("드레싱 주기를 어떻게 잡아?", "캡이 빨리 닳는 이유가 뭐야?")),
    ("S08", "S08 축 보정 틀어짐·간섭에 의한 위치 이탈", "cause_exploration", "원인 상황", "c3·c7 변화", "P5 → E03", "festo",
     ("정비하고 나서 위치가 안 맞아. 보정 다시 해야 돼?",)),
    ("S09", "S09 냉각수·변압기·용접 제어기 등 주변 설비 이상", "cause_exploration", "원인 상황", "건 센서는 정상, 경보만 발생", "P9", "abb",
     ("건은 멀쩡한데 왜 라인이 멈췄어?", "냉각수 유량은 얼마가 정상이야?")),
    ("S10", "S10 설정값 변경·잔고장 반복 (오경보 구분)", "operational_context", "운영 상황", "c13~c18 변화, 에러 비율↑", "P7·P8", "abb",
     ("이 경보 진짜야, 아니면 설정 바뀐 거야?", "E003이 여러 날 떠 있는데 무시해도 돼?")),
]
SCENARIOS = [
    ScenarioDef(
        EntityDef(f"scenario:{sid}", "scenario", NS_SCENARIO, sid, label,
                  f"MVP 오류 상황 {sid} ({kind_ko}). 데이터 단서: {clue}. ML 연결: {ml}. 1순위 매뉴얼: {manual}.",
                  metadata={"situation_type": kind_ko, "data_clue": clue, "ml_link": ml, "primary_manual": manual,
                            "definition_section": f"D-{sid}"}),
        kind, questions)
    for sid, label, kind, kind_ko, clue, ml, manual, questions in _SCENARIOS
]

# ---------------------------------------------------------------- 점검 절차 (정의서 '점검 순서')
_ROLE = "maintenance_technician"
_ISOLATE = "부품을 분해·교체하거나 가압부에 손을 넣기 전에 압축 공기 공급과 동작·부하 전원을 차단합니다 (Festo 1.2)."
_AXIS = ("축 보정은 새 전극 캡을 장착하고 PC 제어 활성화·무오류·US2 전원 ON 상태에서만 수행합니다 (Festo 8.5, 9.9).",
         "축 보정 중에는 시스템이 가압 상태이므로 전극 사이에 손을 넣지 않습니다 (Festo 9.9).")


def _proc(sid: str, label: str, kind: str, role: str, preconditions: tuple[str, ...], steps: list[StepDef]) -> ProcedureDef:
    return ProcedureDef(
        EntityDef(f"proc:{sid}", "procedure", NS_PROCEDURE, f"{sid}-CHECK", label,
                  f"{sid} 상황의 점검 순서. 정의서의 '쉽고 싸고 흔한 것 먼저' 원칙(압력 → 밸브 → 실린더 → 보정 → 기계)을 따른다. 현장 작업자 검증 전 초안이다."),
        kind, role, preconditions, f"D-{sid}", tuple(steps))


PROCEDURES = [
    _proc("S01", "S01 보정 압력 도달 지연 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("본관(메인) 공급 압력을 측정하고 보정 장치 공급 압력이 5 bar 이상인지 확인합니다.", "공급 압력 측정값 기록", "F-10.1-12#row"),
        StepDef("공급 라인의 필터와 레귤레이터 상태를 확인합니다.", "필터 막힘·레귤레이터 설정 이상 여부 기록", "D-S01#step2"),
        StepDef("보정 실린더 압력 조절 밸브(MPYD)의 케이블 연결과 밸브 동작을 확인합니다.", "MPYD 케이블·밸브 이상 여부 기록", "D-S01#step3"),
        StepDef("보정 실린더 배관과 연결부의 누설을 확인합니다.", "누설 위치 기록", "D-S01#step4"),
        StepDef("이퀄라이저(보정 압력) 설정을 다시 확인합니다. 건이 떠 있는(floating) 상태가 되도록 보정 압력을 맞춥니다.", "보정 압력 설정값 기록", "F-8.11#floating"),
    ]),
    _proc("S02", "S02 전극 파손 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("고정측·이동측 전극 캡의 파손·이탈 여부를 육안으로 확인합니다.", "캡 파손·이탈 여부 기록", "D-S02#step1"),
        StepDef("제어기 실제값 화면에서 고정측 캡 마모량과 전체 캡 마모량을 확인합니다.", "캡 마모량 기록", "F-9.15#abrasion"),
        StepDef("드레싱(밀링) 후 캡 측정 결과가 허용 범위 안인지 확인합니다.", "밀링 후 측정값과 Milling OK 여부 기록", "F-9.14.9#start"),
        StepDef("캡을 교체했다면 캡 세팅 후 형상 점검(Measure before milling)으로 새 캡 위치를 다시 기준으로 저장합니다.", "새 캡 위치 저장, TCP 리셋 확인", "F-9.14.8#geometry"),
    ]),
    _proc("S03", "S03 원치 않는 이동 점검 절차", "inspection", _ROLE, (_ISOLATE, *_AXIS), [
        StepDef("전극 이동 범위 안의 장애물과 호스·케이블 걸림을 확인합니다.", "간섭물 유무 기록", "F-10.1-9#row"),
        StepDef("새 전극 캡을 장착한 상태에서 축 보정을 다시 수행합니다.", "영점·변환비·최대 열림 값 기록", "F-8.5#newcaps"),
        StepDef("비례 방향 제어 밸브(MPYE)와 구동부 연결 상태를 확인합니다.", "밸브 케이블·구동부 연결 이상 여부 기록", "F-10.2-MOVE#move"),
        StepDef("건 암과 회전축이 걸림 없이 움직이는지 확인합니다.", "걸림 여부 기록", "D-S03#step4"),
    ]),
    _proc("S04", "S04 드리프트 점검 절차", "inspection", _ROLE,
          ("잠금 기능 진단은 로봇이 'Diagnostic function'과 'Release positioning' 비트를 설정해 시작합니다 (Festo 11.3).",
           "진단 중 공급 압력을 차단하면 진단 오류가 날 수 있습니다 (Festo 11.3).", _ISOLATE), [
        StepDef("주 실린더 잠금 기능 진단(차단 밸브 1V2·1V3·1V4 기능 시험)을 실행합니다.", "진단 결과(Diagnostic function I/O 또는 error) 기록", "F-9.14.10#functional_test"),
        StepDef("진단 중·정지 중 주 실린더 압력과 위치 변화를 기록합니다. 변화가 없으면 잠금 기능은 정상입니다.", "압력·위치 변화량 기록", "F-9.14.10#no_change"),
        StepDef("주 실린더의 차단(잠금) 밸브 동작을 확인합니다.", "차단 밸브 이상 여부 기록", "F-10.1-11#row"),
        StepDef("주 실린더 누설 테스트를 수행합니다.", "누설 여부 기록. 누설이 있으면 주 실린더 교체를 검토합니다.", "D-S04#step4"),
    ]),
    _proc("S05", "S05 공기 공급 부족·누설 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("툴링 게이지가 아니라 용접 실린더 입구에서 공기압을 측정합니다.", "실린더 입구 압력 기록", "D-S05#step1"),
        StepDef("호스와 피팅의 공기 누설을 확인합니다.", "누설 위치 기록", "D-S05#step2"),
        StepDef("5 µm 필터 상태와 교체 이력을 확인합니다. 필터는 100만 용접점마다 교체를 권장합니다.", "필터 상태와 마지막 교체 시점 기록", "F-6#filter"),
        StepDef("필요하면 생산 운전 압력과 같은 압력에서 힘 보정을 다시 수행합니다.", "힘 보정 결과 기록", "F-8.6#pressure"),
    ]),
    _proc("S06", "S06 기계 마찰·걸림·윤활 부족 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("공기·전원을 차단한 뒤 가이드와 베어링이 걸림 없이 가볍게 움직이는지 손으로 확인합니다.", "걸림 위치 기록", "F-8.6#guides"),
        StepDef("회전축·링크의 부싱·핀 유격을 확인하고 0.5 mm를 넘으면 교체합니다.", "유격 측정값 기록", "D-S06#step2"),
        StepDef("주 실린더 윤활 이력을 확인합니다. 윤활 간격은 300만 용접점을 넘지 않아야 합니다.", "마지막 윤활 시점 기록", "F-6#lube"),
        StepDef("제품·지그와의 간섭 여부를 확인합니다.", "간섭 여부 기록", "D-S06#step4"),
    ]),
    _proc("S07", "S07 전극 캡 마모·드레싱 불량 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("전극 캡 상태(마모·변형·장착)를 확인합니다.", "캡 상태 기록", "D-S07#step1"),
        StepDef("드레싱(밀링) 전후 캡 측정값과 밀링 결과를 확인합니다.", "드레싱 전후 측정값 기록", "F-9.14.9#normal"),
        StepDef("전극 암의 캡 냉각수 유량이 암당 분당 4 L 이상인지 확인합니다.", "냉각수 유량 기록", "D-S07#step3"),
        StepDef("캡 교체 이력을 확인합니다.", "교체 주기 기록", "D-S07#step4"),
    ]),
    _proc("S08", "S08 축 보정 틀어짐·위치 이탈 점검 절차", "inspection", _ROLE, (_ISOLATE, *_AXIS), [
        StepDef("최근 정비와 캡 교체 이력을 확인합니다.", "정비·교체 일시 기록", "D-S08#step1"),
        StepDef("새 캡을 장착한 상태에서 축 보정을 다시 수행하고, 산출된 변환비와 최대 전극 열림 값을 확인합니다.", "변환비·최대 열림 값 기록", "F-9.9#warning"),
        StepDef("이동 범위 안의 배관·케이블 간섭을 확인합니다.", "간섭 여부 기록", "D-S08#step3"),
    ]),
    _proc("S09", "S09 주변 설비 이상 점검 절차", "inspection", _ROLE, (_ISOLATE,), [
        StepDef("냉각수 유량(전극 암 분당 4 L 이상, DC 변압기 분당 8 L 이상)과 입구 온도(18~30 °C)를 확인합니다.", "유량·입구 온도 기록", "D-S09#step1"),
        StepDef("용접 변압기 온도(과열 신호)를 확인합니다.", "변압기 온도·과열 신호 기록", "D-S09#step2"),
        StepDef("용접 제어기(타이머)의 에러 기록을 확인합니다.", "용접 제어기 에러 코드 기록", "D-S09#step3"),
    ]),
    _proc("S10", "S10 설정값 변경·잔고장 반복 확인 절차", "information_gathering", "process_engineer", (), [
        StepDef("용접 프로그램 설정값(c13~c18) 변경 이력을 확인합니다. 의도한 레시피 변경이면 고장이 아닙니다.", "변경 일시와 변경 사유 기록", "D-S10#step1"),
        StepDef("최근 에러 코드 목록과 발생 빈도를 확인합니다.", "최근 에러 코드와 빈도 기록", "D-S10#step2"),
        StepDef("같은 증상이 반복되면 S01~S08 중 해당 상황의 점검 절차로 넘어갑니다.", "넘어간 상황 ID 기록", "D-S10#step3"),
    ]),
]

# ---------------------------------------------------------------- 수치 기준
SPECS = [
    SpecDef(EntityDef("spec:E01-LIMIT", "numeric_spec", NS_SPEC, "E01-LIMIT", "E01 보정 압력 도달 제한 시간 1800 ms",
                      "논문 고장 E01 정의의 보정 압력 도달 제한 시간."),
            "보정 압력 도달 시간", "le", 1800, None, "1800 ms 안에 도달", "ms", "paper_definition", "D-S01#definition",
            {"applies_to": f"{NS_FAULT}/E01"}),
    SpecDef(EntityDef("spec:E03-DEVIATION", "numeric_spec", NS_SPEC, "E03-DEVIATION", "E03 위치 편차 한계 스트로크의 6.5%",
                      "논문 고장 E03 정의의 목표 위치 대비 편차 한계."),
            "목표 위치 대비 실제 위치 편차", "le", 6.5, None, "실린더 스트로크의 6.5%", "% of cylinder stroke", "paper_definition",
            "D-S03#definition", {"applies_to": f"{NS_FAULT}/E03"}),
    SpecDef(EntityDef("spec:E04-DRIFT", "numeric_spec", NS_SPEC, "E04-DRIFT", "E04 잠금 중 이동 한계 분당 5 mm",
                      "논문 고장 E04 정의의 잠긴 실린더 이동 속도 한계."),
            "잠긴 실린더 이동 속도", "le", 5, None, "분당 5 mm", "mm/min", "paper_definition", "D-S04#definition",
            {"applies_to": f"{NS_FAULT}/E04"}),
    SpecDef(EntityDef("spec:FESTO-EQ-TIMEOUT", "numeric_spec", NS_SPEC, "FESTO-EQ-TIMEOUT", "Festo 이퀄라이저 압력 도달 제한 2초",
                      "Festo 오류 12(Timeout equalizer) 판정 시간."),
            "이퀄라이저 압력 도달 시간", "le", 2, None, "Equalizer pressure was not reached within 2 seconds.", "s",
            "equipment_setting", "F-10.1-12#row"),
    SpecDef(EntityDef("spec:FESTO-EQ-SUPPLY", "numeric_spec", NS_SPEC, "FESTO-EQ-SUPPLY", "보정 장치 공급 압력 5 bar 이상",
                      "Festo 오류 12 조치의 공급 압력 기준."),
            "보정 장치 공급 압력", "ge", 5, None, "Check supply pressure at least 5 bar.", "bar", "maintenance_limit", "F-10.1-12#row"),
    SpecDef(EntityDef("spec:FESTO-FILTER", "numeric_spec", NS_SPEC, "FESTO-FILTER", "5 µm 필터 교체 주기 100만 용접점",
                      "Festo 정비 지침의 필터 교체 권장 주기."),
            "5 µm 필터 교체 주기", "le", 1_000_000, None, "every 1 million welding points", "welding points", "recommendation", "F-6#filter"),
    SpecDef(EntityDef("spec:FESTO-LUBE", "numeric_spec", NS_SPEC, "FESTO-LUBE", "주 실린더 윤활 간격 300만 용접점 이내",
                      "Festo 정비 지침의 윤활 간격 한계."),
            "주 실린더 윤활 간격", "le", 3_000_000, None, "should not be more than 3 million welding points apart", "welding points",
            "maintenance_limit", "F-6#lube"),
    SpecDef(EntityDef("spec:MILCO-BUSHING", "numeric_spec", NS_SPEC, "MILCO-BUSHING", "링크 핀 유격 0.5 mm 초과 시 부싱 교체",
                      "MILCO 예방정비 일정의 링크 핀 유격 기준 (유사 장비 참고)."),
            "링크 핀·부싱 유격", "le", 0.5, None, "± .5mm – Replace Bushings", "mm", "maintenance_limit", "M-3-1#link_pins"),
    SpecDef(EntityDef("spec:MILCO-ARM-WATER", "numeric_spec", NS_SPEC, "MILCO-ARM-WATER", "전극 암 냉각수 분당 4 L 이상",
                      "MILCO 냉각수 요구 조건 (유사 장비 참고)."),
            "전극 암 냉각수 유량", "ge", 4, None, "a minimum of 1 gal/min (4 l/min)", "L/min", "maintenance_limit", "M-9-1#arm_flow"),
    SpecDef(EntityDef("spec:MILCO-DC-TRAFO-WATER", "numeric_spec", NS_SPEC, "MILCO-DC-TRAFO-WATER", "DC 변압기 냉각수 분당 8 L 이상",
                      "MILCO 변압기 냉각 요구 조건 (유사 장비 참고)."),
            "DC 변압기 냉각수 유량", "ge", 8, None, "2 gal/min (8 l/min)", "L/min", "maintenance_limit", "M-9-1#transformer",
            {"transformer_type": "dc_mid_frequency"}),
    SpecDef(EntityDef("spec:MILCO-INLET-TEMP", "numeric_spec", NS_SPEC, "MILCO-INLET-TEMP", "냉각수 입구 온도 18~30 °C",
                      "MILCO 냉각수 입구 온도 범위 (유사 장비 참고)."),
            "냉각수 입구 온도", "range", 18, 30, "between 65 and 85 °F (18-30 °C)", "°C", "maintenance_limit", "M-9-1#inlet"),
    SpecDef(EntityDef("spec:MILCO-TRAFO-TEMP", "numeric_spec", NS_SPEC, "MILCO-TRAFO-TEMP", "변압기 온도 140 °C 미만",
                      "MILCO 예방정비 일정의 변압기 온도 기준 (유사 장비 참고)."),
            "변압기 온도", "lt", 140, None, "<140°C – lower duty cycle", "°C", "maintenance_limit", "M-3-1#transformer_temp"),
    SpecDef(EntityDef("spec:MILCO-HOSE", "numeric_spec", NS_SPEC, "MILCO-HOSE", "제어 밸브-실린더 공기 호스 최대 10 ft",
                      "MILCO 공기 관리 기준: 3/8 내경, 최대 10 ft (유사 장비 참고)."),
            "제어 밸브-실린더 공기 호스 길이", "le", 10, None, "3/8 inside diameter and a maximum length of ten feet", "ft",
            "recommendation", "M-6-9#hoses", {"hose_inside_diameter": "3/8 in"}),
    SpecDef(EntityDef("spec:MILCO-CAP-ALIGN", "numeric_spec", NS_SPEC, "MILCO-CAP-ALIGN", "캡 정렬 허용 편차 0.8 mm",
                      "MILCO 캡 어댑터 교체 시 캡 정렬 기준 (유사 장비 참고)."),
            "캡 정렬 편차", "le", 0.8, None, "aligned (within ± .030” [.8 mm])", "mm", "maintenance_limit", "M-6-2#align"),
]

ENTITIES: list[EntityDef] = (
    FAULTS + ERROR_CODES + FESTO_CODES + [f.entity for f in FEATURES] + COMPONENTS + RULES
    + [p.entity for p in PATTERNS] + CAUSES + [s.entity for s in SCENARIOS] + [p.entity for p in PROCEDURES]
    + [s.entity for s in SPECS]
)

# ================================================================ 관계
# evidence_kind / empirical_status는 정의서 표현을 따른다: 데이터로 본 것(18/18건)은 supported,
# 문서가 그대로 말한 것은 source_statement, 매뉴얼 기반 추정("이어질 수 있다")은 hypothesis.
RELATIONS: list[RelationDef] = []


def rel(subject: str, predicate: str, obj: str, evidence: tuple[str, ...] | str, kind: str, status: str, explanation: str) -> None:
    RELATIONS.append(RelationDef(subject, predicate, obj, (evidence,) if isinstance(evidence, str) else evidence,
                                 kind, status, explanation))


# 에러 코드 → 고장 분류 (정의서 EDA: 고장 10분 안 발생 비율)
for code, fault, sid, rate in (("E012", "E01", "S01", "18/18"), ("E016", "E02", "S02", "17/18"),
                               ("E028", "E03", "S03", "18/18"), ("E029", "E04", "S04", "18/18")):
    rel(f"code:{code}", "maps_to", f"fault:{fault}", (f"D-{sid}#signals", f"D-SUMMARY#{sid}"), "team_analysis", "supported",
        f"정의서 데이터 신호: {fault} 고장 건에서 {code}가 {rate}건 나타났다. 같은 개념이라는 뜻(owl:sameAs)이 아니다.")

# 상황 → 고장 분류 (S01~S04는 논문 라인 정지 고장 4종)
for sid, fault in (("S01", "E01"), ("S02", "E02"), ("S03", "E03"), ("S04", "E04")):
    rel(f"scenario:{sid}", "related_fault", f"fault:{fault}", (f"D-SUMMARY#{sid}", f"D-{sid}#definition"), "team_analysis",
        "source_statement", f"정의서 요약표: {sid}는 {fault} 라인 정지 고장 대응 상황이다.")

# ML 센서 해석 규칙 → 상황 (정의서 요약표 'ML 연결')
for rule, sid in (("P1", "S01"), ("P5", "S02"), ("P6", "S02"), ("P5", "S03"), ("P4", "S04"), ("P2", "S05"),
                  ("P3", "S06"), ("P6", "S07"), ("P5", "S08"), ("P9", "S09"), ("P7", "S10"), ("P8", "S10")):
    rel(f"rule:{rule}", "triggers", f"scenario:{sid}", f"D-SUMMARY#{sid}", "team_analysis", "source_statement",
        f"정의서 요약표의 ML 연결 열: {rule}가 {sid}와 짝을 이룬다.")

# 상황 → 원인 후보 (정의서 상황별 '원인 후보')
RAISES = {
    "S01": ["H01", "H02", "H03", "H04"], "S02": ["H05", "H06", "H07", "H08"], "S03": ["H09", "H10", "H11", "H12"],
    "S04": ["H16", "H17"], "S05": ["H18", "H19", "H20", "H17"], "S06": ["H13", "H14", "H15", "H09"],
    "S07": ["H06", "H07", "H21"], "S08": ["H10", "H22", "H09"], "S09": ["H23", "H24", "H25", "H26"],
    "S10": ["H27", "H28", "H29"],
}
for sid, causes in RAISES.items():
    for cause in causes:
        rel(f"scenario:{sid}", "raises_hypothesis", f"cause:{cause}", f"D-{sid}#causes", "team_analysis", "hypothesis",
            f"정의서 {sid} 원인 후보 목록. 확정 원인이 아니다.")

# 증상 패턴 → 원인 후보 (RAG 활용정리 8장 이상 패턴 표, 정의서 데이터 신호)
SUGGESTS = {
    "P1": (["H01", "H02", "H03", "H04"], ("R-JUDGEMENT#p_counterbalance", "R-FESTO#patterns")),
    "P2": (["H17", "H18", "H19", "H20"], ("R-JUDGEMENT#p_supply", "D-S05#signals")),
    "P3": (["H09", "H12", "H13", "H14", "H15"], ("R-JUDGEMENT#p_friction", "R-MILCO#judgement")),
    "P4": (["H16", "H17"], ("R-JUDGEMENT#p_drift", "D-S04#signals")),
    "P5": (["H08", "H09", "H10", "H11", "H12", "H22"], ("R-JUDGEMENT#p_position", "D-S08#signals")),
    "P6": (["H05", "H06", "H07", "H21"], ("D-S07#signals", "D-S02#signals")),
    "P7": (["H27"], ("D-S10#signals",)),
    "P8": (["H28", "H29"], ("D-S10#signals",)),
    "P9": (["H23", "H24", "H25", "H26"], ("R-JUDGEMENT#p_peripheral", "D-S09#signals")),
}
for pattern, (causes, evidence) in SUGGESTS.items():
    for cause in causes:
        rel(f"pattern:{pattern}", "suggests", f"cause:{cause}", evidence, "team_analysis", "hypothesis",
            f"센서 패턴 {pattern}는 이 원인 후보를 점검하라는 신호다. 학습 데이터로 확인된 관계가 아니다.")

# 원인 후보 → 고장 분류 ('방치하면 ~로 이어질 수 있다', 모두 매뉴얼 기반 추정)
CONTRIBUTES = [
    ("E01", ["H01", "H02", "H03", "H04"], "D-S01#causes"), ("E01", ["H17", "H18", "H19", "H20"], "D-S05#signals"),
    ("E02", ["H05", "H06", "H07", "H08"], "D-S02#causes"), ("E02", ["H21"], "D-S07#signals"),
    ("E03", ["H09", "H10", "H11", "H12"], "D-S03#causes"), ("E03", ["H13", "H14", "H15"], "D-S06#signals"),
    ("E03", ["H22"], "D-S08#signals"), ("E04", ["H16", "H17"], "D-S04#causes"),
]
for fault, causes, evidence in CONTRIBUTES:
    for cause in causes:
        rel(f"cause:{cause}", "may_contribute_to", f"fault:{fault}", (evidence, "D-SUMMARY#note"), "team_analysis", "hypothesis",
            f"정의서 기준 매뉴얼 기반 추정: 이 원인은 {fault}로 이어질 수 있다. 데이터로 확인된 관계가 아니다.")

# 원인 후보 → 부품
CAUSE_COMPONENTS = {
    "H01": ["air_supply"], "H02": ["mpyd_valve"], "H03": ["compensating_cylinder"], "H04": ["compensating_cylinder"],
    "H05": ["electrode_cap"], "H06": ["electrode_cap"], "H07": ["electrode_cap"], "H08": ["electrode_cap", "controller"],
    "H09": ["gun_arm"], "H10": ["controller"], "H11": ["mpye_valve"], "H12": ["gun_arm"], "H13": ["gun_arm"],
    "H14": ["compensating_cylinder"], "H15": ["main_cylinder", "gun_arm"], "H16": ["blocking_valves"],
    "H17": ["main_cylinder"], "H18": ["air_supply"], "H19": ["air_supply"], "H20": ["air_supply"],
    "H21": ["cooling_water", "electrode_cap"], "H22": ["position_sensor"], "H23": ["cooling_water"],
    "H24": ["transformer"], "H25": ["weld_controller"], "H26": ["controller"], "H27": ["controller"],
    "H28": ["controller"], "H29": ["controller"],
}
COMPONENT_EVIDENCE = {
    "main_cylinder": "D-COMPONENTS#main_cylinder", "blocking_valves": "D-COMPONENTS#main_cylinder",
    "position_sensor": "F-10.1-1#row", "compensating_cylinder": "D-COMPONENTS#compensating_cylinder",
    "mpye_valve": "D-COMPONENTS#mpye", "mpyd_valve": "D-COMPONENTS#mpyd", "electrode_cap": "D-COMPONENTS#cap",
    "gun_arm": "D-COMPONENTS#gun_arm", "controller": "D-COMPONENTS#controller", "air_supply": "D-COMPONENTS#peripheral",
    "cooling_water": "D-COMPONENTS#peripheral", "transformer": "D-COMPONENTS#peripheral",
    "weld_controller": "D-COMPONENTS#peripheral",
}
for cause, components in CAUSE_COMPONENTS.items():
    for component in components:
        rel(f"cause:{cause}", "concerns_component", f"comp:{component}", COMPONENT_EVIDENCE[component], "team_analysis",
            "source_statement", "정의서 구성 부품 표의 부품 역할에 따른 연결.")

# 상황 → 점검 절차, 절차 → 원인 후보·부품·수치
PROCEDURE_COMPONENTS = {
    "S01": ["air_supply", "mpyd_valve", "compensating_cylinder"], "S02": ["electrode_cap", "controller"],
    "S03": ["gun_arm", "mpye_valve", "controller"], "S04": ["blocking_valves", "main_cylinder"],
    "S05": ["air_supply", "main_cylinder"], "S06": ["gun_arm", "main_cylinder", "compensating_cylinder"],
    "S07": ["electrode_cap", "cooling_water"], "S08": ["controller", "position_sensor", "gun_arm"],
    "S09": ["cooling_water", "transformer", "weld_controller"], "S10": ["controller"],
}
PROCEDURE_SPECS = {
    "S01": ["E01-LIMIT", "FESTO-EQ-TIMEOUT", "FESTO-EQ-SUPPLY"], "S02": ["MILCO-CAP-ALIGN"], "S03": ["E03-DEVIATION"],
    "S04": ["E04-DRIFT"], "S05": ["FESTO-FILTER", "MILCO-HOSE"], "S06": ["FESTO-LUBE", "MILCO-BUSHING"],
    "S07": ["MILCO-ARM-WATER", "MILCO-CAP-ALIGN"],
    "S09": ["MILCO-ARM-WATER", "MILCO-DC-TRAFO-WATER", "MILCO-INLET-TEMP", "MILCO-TRAFO-TEMP"],
}
SPEC_SOURCE = {s.entity.ref: s.source for s in SPECS}
for sid in RAISES:
    rel(f"scenario:{sid}", "has_procedure", f"proc:{sid}", (f"D-{sid}#checks", "D-PRINCIPLE#principle"), "team_analysis",
        "source_statement", f"정의서 {sid} 점검 순서.")
    for cause in RAISES[sid]:
        rel(f"proc:{sid}", "checks_hypothesis", f"cause:{cause}", f"D-{sid}#checks", "team_analysis", "source_statement",
            f"{sid} 점검 순서로 확인하는 원인 후보.")
    for component in PROCEDURE_COMPONENTS[sid]:
        rel(f"proc:{sid}", "concerns_component", f"comp:{component}", f"D-{sid}#checks", "team_analysis", "source_statement",
            f"{sid} 점검 순서가 다루는 부품.")
    for spec in PROCEDURE_SPECS.get(sid, []):
        rel(f"proc:{sid}", "uses_spec", f"spec:{spec}", (SPEC_SOURCE[f"spec:{spec}"],), "manual" if not spec.startswith("E0") else "team_analysis",
            "source_statement", f"{sid} 점검에서 비교할 수치 기준.")

# ---------------------------------------------------------------- documented_in (개념 → 원문 절)
DOCUMENTED: list[tuple[str, str, str]] = []  # (개념, 절, 근거 span)
for code, sid in (("E012", "S01"), ("E016", "S02"), ("E028", "S03"), ("E029", "S04")):
    DOCUMENTED += [(f"code:{code}", f"D-{sid}", f"D-{sid}#signals"), (f"code:{code}", "D-SUMMARY", f"D-SUMMARY#{sid}")]
DOCUMENTED += [("code:E029", "D-S10", "D-S10#signals")]
for code in ("E003", "E011"):
    DOCUMENTED += [(f"code:{code}", "D-S10", "D-S10#signals"), (f"code:{code}", "D-LIMITS", "D-LIMITS#minor_codes")]
for n, _, _, section in FESTO_ERRORS:
    DOCUMENTED.append((f"festo:{n}", section, f"{section}#row"))
for fault, sid in (("E01", "S01"), ("E02", "S02"), ("E03", "S03"), ("E04", "S04")):
    DOCUMENTED += [(f"fault:{fault}", f"D-{sid}", f"D-{sid}#definition"),
                   (f"fault:{fault}", "R-FAULT-LINKS", f"R-FAULT-LINKS#{fault}"),
                   (f"fault:{fault}", "R-SCOPE", "R-SCOPE#faults")]

# 상황의 '참고' 목록 (정의서 순서 = 검색 우선순위). FANUC 비정상 저항은 정의서 쪽수(p.225~226, 온도 보정 절)가
# 아니라 실제 내용이 있는 SVGN-261(PDF p.310)로 연결했다.
SCENARIO_REFERENCES = {
    "S01": ["F-5.3", "F-8.11", "F-10.1-12", "A-2.1.6", "Y-1.6.6", "M-6-9"],
    "S02": ["F-8.8", "F-9.14.7", "F-9.14.8", "F-9.14.9", "M-6-2", "M-16", "N-6.1"],
    "S03": ["F-8.5", "F-9.9", "F-10.1-9", "F-10.2-POSITION", "M-12", "M-13", "N-SVGN-261"],
    "S04": ["F-9.14.10", "F-11.3", "M-6-10"],
    "S05": ["F-5.2.1", "F-6", "F-8.6", "M-4", "M-6-9", "Y-1.6.6"],
    "S06": ["M-4", "M-3-1", "M-11", "M-12", "M-13", "F-6", "F-9.15", "N-SVGN-261"],
    "S07": ["F-8.8", "F-9.14.7", "F-9.14.8", "F-9.14.9", "M-6-2", "M-9-1", "M-16", "N-26.1"],
    "S08": ["F-8.5", "F-9.9", "N-6.14", "M-6-7"],
    "S09": ["A-2.1.6", "A-3.6", "Y-1.6.6", "Y-1.6.7", "M-8", "M-9-1"],
    "S10": ["A-2.1.2", "A-3.6", "F-9.15", "Y-1.6.7", "Y-1.1"],
}
for sid, sections in SCENARIO_REFERENCES.items():
    DOCUMENTED.append((f"scenario:{sid}", f"D-{sid}", f"D-{sid}#symptoms"))
    for section in sections:
        DOCUMENTED.append((f"scenario:{sid}", section, f"D-{sid}#refs"))

# 원인 후보 → 근거 절
CAUSE_SECTIONS = {
    "H01": [("F-10.1-12", "row"), ("F-10.2-FORCE", "force"), ("M-6-9", "supply")],
    "H02": [("F-10.1-12", "row")],
    "H03": [("F-5.3", "tubing"), ("R-FESTO", "patterns")],
    "H04": [("F-5.3", "connect"), ("R-JUDGEMENT", "p_counterbalance")],
    "H05": [("F-10.1-29", "row"), ("F-8.8", "life")],
    "H06": [("F-10.1-30", "row"), ("F-9.14.9", "normal")],
    "H07": [("F-9.14.7", "setcaps"), ("F-10.1-31", "row"), ("M-16", "seat")],
    "H08": [("F-9.14.8", "geometry"), ("F-8.5", "newcaps")],
    "H09": [("F-10.1-9", "row"), ("F-10.1-4", "row"), ("M-4", "interference"), ("M-4", "product")],
    "H10": [("F-9.9", "warning"), ("F-10.1-6", "row"), ("F-10.2-POSITION", "position")],
    "H11": [("F-10.1-10", "row"), ("F-10.2-MOVE", "move")],
    "H12": [("M-4", "binding"), ("M-12", "quarter")],
    "H13": [("M-3-1", "link_pins"), ("M-13", "link")],
    "H14": [("M-11", "slide"), ("M-3-1", "slide_bushing")],
    "H15": [("F-6", "lube"), ("F-10.1-14", "row"), ("N-SVGN-261", "disturbance")],
    "H16": [("F-9.14.10", "test"), ("F-10.1-11", "row"), ("F-11.3", "general")],
    "H17": [("F-10.1-11", "row"), ("M-4", "cylinder"), ("M-6-10", "leak_test")],
    "H18": [("M-4", "leaks"), ("M-3-1", "air_leaks")],
    "H19": [("F-5.2.1", "filter"), ("F-6", "filter"), ("M-6-9", "filter")],
    "H20": [("M-6-9", "hoses"), ("F-5.2.2", "hoses")],
    "H21": [("M-9-1", "arm_flow"), ("M-9-1", "why")],
    "H22": [("F-10.1-1", "row"), ("F-9.15", "sensor")],
    "H23": [("M-9-1", "inlet"), ("Y-1.6.6", "gun_cool"), ("A-2.1.6", "water_timeout")],
    "H24": [("M-8", "thermostat"), ("Y-1.6.6", "transthermo"), ("F-10.1-17", "row")],
    "H25": [("Y-1.6.6", "weld_err"), ("A-3.6", "weld_error")],
    "H26": [("R-ABB", "roles"), ("A-3.6", "weld_error")],
    "H27": [("F-9.15", "setvalues"), ("D-S10", "causes")],
    "H28": [("F-9.15", "sensor"), ("D-S10", "causes")],
    "H29": [("A-2.1.2", "ignore"), ("M-3-2", "log"), ("D-S10", "causes")],
}
for cause, items in CAUSE_SECTIONS.items():
    seen: set[str] = set()
    for section, span in items:
        if section in seen:
            continue
        seen.add(section)
        DOCUMENTED.append((f"cause:{cause}", section, f"{section}#{span}"))

for procedure in PROCEDURES:
    sid = procedure.source_section
    DOCUMENTED.append((procedure.entity.ref, sid, f"{sid}#checks"))
    for section in dict.fromkeys(step.source.split("#")[0] for step in procedure.steps):
        if section != sid:
            DOCUMENTED.append((procedure.entity.ref, section, next(s.source for s in procedure.steps if s.source.startswith(section + "#"))))
for spec in SPECS:
    DOCUMENTED.append((spec.entity.ref, spec.source.split("#")[0], spec.source))

for subject, section, evidence in DOCUMENTED:
    kind = "manual" if section[0] in "FMYAN" else "team_analysis"
    rel(subject, "documented_in", f"sec:{section}", evidence, kind, "source_statement", "개념을 설명하거나 근거로 제시하는 원문 절.")

# ---------------------------------------------------------------- 검토된 충돌 기록
# (영향 개념, 왼쪽 절, 오른쪽 절, 설명, 해결)
CONFLICTS = [
    ("fault:E01", "D-S01", "F-10.1-12",
     "E01 정의의 보정 압력 도달 제한(1800 ms, 논문 데이터)과 Festo 오류 12의 이퀄라이저 압력 도달 제한(2 s)이 다르다.",
     "서로 다른 체계의 정의로 보존한다. 1800 ms는 논문 데이터셋 종료 코드 기준, 2 s는 Festo 제어기 오류 12 기준이며 한쪽 값을 다른 쪽에 적용하지 않는다."),
]

# 적재 보고서에 남기는 원문 참조 교정 사항
SOURCE_NOTES = [
    "정의서의 FANUC '비정상 저항 PDF p.225~226'은 실제로 19장 온도 보정 절이다. 비정상 저항(외란) 내용은 SVGN-261 (PDF p.310, 인쇄 -294-)에 있어 그 절로 연결했다.",
    "정의서의 'Festo 10장' 참조는 오류 메시지 표의 해당 행(10.1/N)과 문제 해결 표(10.2) 단위로 나누어 연결했다.",
    "데이터셋 에러 코드(E012 등)와 Festo 제어기 오류 번호(12 등)는 다른 체계이므로 매핑하지 않았다. 예: Festo 28은 Cap dressing, 데이터셋 E028은 원치 않는 이동.",
]
