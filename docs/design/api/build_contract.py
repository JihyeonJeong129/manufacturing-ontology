"""Build the proposed API contract. Does not implement or call an API server."""
from pathlib import Path
import json

ROOT = Path(__file__).parent
S = {}

def ref(name): return {'$ref': f'#/components/schemas/{name}'}
def string(description='', **kw): return {'type': 'string', 'description': description, **kw}
def ident(description='서버 UUID'): return string(description, format='uuid')
def timestamp(description='RFC 3339 시각; 요청에는 시간대 필수, 응답은 UTC'): return string(description, format='date-time')
def enum(values, description=''): return string(description, enum=values.split('|'))
def arr(items, description='', **kw): return {'type':'array','items':items,'description':description,**kw}
def obj(properties, required=(), description='', **kw):
    return {'type':'object','description':description,'properties':properties,'required':list(required),'additionalProperties':False,**kw}
def nullable(schema, description=''):
    return {'anyOf':[schema,{'type':'null'}],**({'description':description} if description else {})}
def free(description='계약별 부가 JSON 객체'): return {'type':'object','description':description,'additionalProperties':True}
def scalar(description='문자열/유한 숫자/boolean'): return {'type':['string','number','boolean'],'description':description}
def add(name, props, required=(), description='', **kw):
    S[name]=obj(props,required,description,**kw)

add('FieldError', {'location':enum('body|query|path|header'), 'path':string('해당 위치의 필드 경로; body는 JSON Pointer'), 'message':string()}, ['location','path','message'])
add('Problem', {
 'type':string('문제 유형 URI; v1은 about:blank',const='about:blank'),
 'title':string('HTTP 상태의 짧은 제목'), 'status':{'type':'integer','minimum':400,'maximum':599},
 'detail':string('사용자에게 공개 가능한 오류 설명'), 'instance':string('오류가 발생한 요청 경로'),
 'code':string('안정된 애플리케이션 오류 코드'), 'request_id':ident('요청 추적 UUID'),
 'errors':arr(ref('FieldError')),
}, ['type','title','status','detail','instance','code','request_id','errors'], 'RFC 9457 Problem Details 및 애플리케이션 확장 필드')
add('WorkError', {'code':string(), 'message':string(), 'retryable':{'type':'boolean'}}, ['code','message','retryable'], '조회 성공 응답 안에 포함되는 백그라운드 작업 실패')
add('Notice', {'code':string(), 'message':string(), 'field':nullable(string())}, ['code','message','field'])
add('ObservationWindow', {'start':timestamp(), 'end':timestamp()}, ['start','end'], 'start <= end; 구간 의미는 양 끝 포함. 원시 시계열 전체를 전송하지 않는다.')
add('CodeInput', {'namespace':string(minLength=1,maxLength=200), 'code':string(minLength=1,maxLength=128), 'observed_at':nullable(timestamp())}, ['namespace','code'])
add('ObservationSummary', {
 'sample_count':{'type':'integer','minimum':0}, 'min':{'type':'number'}, 'max':{'type':'number'},
 'mean':{'type':'number'}, 'stddev':{'type':'number','minimum':0},
}, [], '수치 요약; 적어도 한 필드 필수. min <= max, mean 범위 및 단위 의미는 서비스 검증.', minProperties=1)
add('ObservationInput', {
 'feature_key':string('원시 feature/센서 태그; 의미를 서버가 매핑',minLength=1,maxLength=200),
 'value':nullable(scalar()), 'summary':nullable(ref('ObservationSummary')), 'unit':nullable(string(maxLength=64)),
 'quality':enum('valid|missing|suspect|unknown'), 'observed_at':nullable(timestamp()),
 'baseline':nullable(free('실제 비교 기준선; normalizer 계약으로 별도 검사')), 'raw_series_ref':nullable(string('시계열 저장 참조. 이 값으로 임의 URL을 자동 요청하지 않는다.',maxLength=2000)),
}, ['feature_key','quality'])
S['ObservationInput']['allOf']=[{'if':{'properties':{'quality':{'const':'valid'}},'required':['quality']},'then':{'anyOf':[{'required':['value'],'properties':{'value':scalar()}},{'required':['summary'],'properties':{'summary':ref('ObservationSummary')}}]}}]
add('ModelIdentity', {'name':string(minLength=1,maxLength=200),'version':string(minLength=1,maxLength=200)}, ['name','version'])
add('MlTarget', {'namespace':string(minLength=1,maxLength=200),'code':string(minLength=1,maxLength=128)}, ['namespace','code'])
add('MlScore', {
 'kind':enum('probability|logit|anomaly_score|rule_score|other'), 'value':{'type':'number','description':'NaN/Infinity 불가'},
 'calibration_status':enum('unknown|uncalibrated|calibrated'), 'calibration_ref':nullable(string(minLength=1,maxLength=2000)),
}, ['kind','value','calibration_status'])
S['MlScore']['allOf']=[
 {'if':{'properties':{'kind':{'const':'probability'}},'required':['kind']},'then':{'properties':{'value':{'minimum':0,'maximum':1}}}},
 {'if':{'properties':{'calibration_status':{'const':'calibrated'}},'required':['calibration_status']},'then':{'required':['calibration_ref'],'properties':{'kind':{'const':'probability'},'calibration_ref':string(minLength=1)}}},
]
add('MlAssessmentInput', {
 'kind':enum('classification|hypothesis|rule_trigger|anomaly'), 'model':ref('ModelIdentity'),
 'run_key':nullable(string(maxLength=200)), 'target':nullable(ref('MlTarget')), 'score':nullable(ref('MlScore')),
 'details':free(), 'caveats':arr(string(maxLength=500),maxItems=20),
}, ['kind','model'], 'classifications는 fault_type, hypothesis는 hypothesis, rule_trigger는 diagnostic_rule로 매핑. anomaly는 원인 엔티티에 직접 매핑하지 않는다.')
S['MlAssessmentInput']['allOf']=[{'if':{'properties':{'kind':{'enum':['classification','hypothesis','rule_trigger']}},'required':['kind']},'then':{'required':['target'],'properties':{'target':ref('MlTarget')}}}]
add('OperatingContext', {
 'cycle_state':enum('welding|non_welding|unknown'), 'workpiece_type':nullable(string(maxLength=200)),
 'shift':nullable(string(maxLength=100)), 'extensions':free('송신자별 맥락; 자동 추론에는 승인된 필드만 사용'),
}, [], '운전 맥락. 누락된 값은 unknown이며 false/0으로 추정하지 않는다.')
add('AlertInput', {
 'schema_version':string('API 입력 envelope 버전',const='1.0'),
 'producer_schema_version':string('ML 송신 데이터 계약 버전',minLength=1,maxLength=100),
 'event_id':string('생산자 내부 사건 ID; 다른 사건에는 재사용 금지',minLength=1,maxLength=128),
 'producer_key':string('등록된 생산자 키; 인증 주체와 일치',minLength=1,maxLength=100),
 'asset_key':string('현장 장비 키. MVP는 assets.asset_code와 매칭',minLength=1,maxLength=128),
 'occurred_at':timestamp(), 'observation_window':nullable(ref('ObservationWindow')),
 'observed_codes':arr(ref('CodeInput'),maxItems=64), 'observations':arr(ref('ObservationInput'),maxItems=128),
 'ml_assessments':arr(ref('MlAssessmentInput'),maxItems=64), 'operating_context':ref('OperatingContext'),
}, ['schema_version','producer_schema_version','event_id','producer_key','asset_key','occurred_at','observed_codes','observations','ml_assessments'], '필수 배열은 데이터가 없으면 []. 미등록 장비/코드 자체는 수신 거절 사유가 아니다.')
add('Processing', {'status':enum('queued|running|succeeded|failed'),'updated_at':timestamp(),'error':nullable(ref('WorkError'))}, ['status','updated_at','error'], '작업 상태이며 context의 mapped/partial 등 해석 결과와 다르다.')
S['Processing']['allOf']=[{'if':{'properties':{'status':{'const':'failed'}},'required':['status']},'then':{'properties':{'error':ref('WorkError')}},'else':{'properties':{'error':{'type':'null'}}}}]
add('AlertAccepted', {
 'alert_id':ident(),'event_id':string(),'producer_key':string(),'received_at':timestamp(),'duplicate':{'type':'boolean'},
 'normalization':ref('Processing'),'status_url':string('GET /api/v1/alerts/{alert_id} 상대 경로'),
}, ['alert_id','event_id','producer_key','received_at','duplicate','normalization','status_url'])
add('AssetResolution', {
 'reported_key':string(),'asset_id':nullable(ident()),'asset_config_id':nullable(ident()),
 'verification_status':enum('unverified|verified|unresolved'),
}, ['reported_key','asset_id','asset_config_id','verification_status'])
add('CodeMapping', {'id':ident('observed_codes.id'),'input_index':{'type':'integer','minimum':0},'entity_id':nullable(ident()),'status':enum('mapped|unmapped|ambiguous')}, ['id','input_index','entity_id','status'])
add('ObservationMapping', {'id':ident('observations.id'),'input_index':{'type':'integer','minimum':0},'feature_id':nullable(ident()),'binding_id':nullable(ident()),'status':enum('mapped|unmapped|invalid')}, ['id','input_index','feature_id','binding_id','status'])
add('MlMapping', {'id':ident('ml_assessments.id'),'input_index':{'type':'integer','minimum':0},'target_entity_id':nullable(ident()),'target_kind':nullable(enum('fault_type|hypothesis|diagnostic_rule'))}, ['id','input_index','target_entity_id','target_kind'])
add('AlertContext', {
 'context_id':ident(),'knowledge_release_id':ident(),'knowledge_version':string(),'normalizer_version':string(),
 'status':enum('mapped|partial|unmapped|invalid'),'asset':ref('AssetResolution'),
 'code_mappings':arr(ref('CodeMapping')),'observation_mappings':arr(ref('ObservationMapping')),'ml_mappings':arr(ref('MlMapping')),
 'warnings':arr(ref('Notice')),'sealed_at':timestamp(),
}, ['context_id','knowledge_release_id','knowledge_version','normalizer_version','status','asset','code_mappings','observation_mappings','ml_mappings','warnings','sealed_at'])
add('ScenarioMatch', {
 'scenario_id':ident(),'namespace':string(),'code':string(),'label':string(),
 'match_methods':arr(enum('observed_code|ml_classification|rule_trigger|pattern|operating_context'),minItems=1,uniqueItems=True),
 'relation_ids':arr(ident()),'explanation':string(),
}, ['scenario_id','namespace','code','label','match_methods','relation_ids','explanation'], '관련 시나리오 후보이며 원인 확정이나 발생 확률을 뜻하지 않는다.')
add('ScenarioMatches', {'status':enum('not_evaluated|matched|no_match|failed'),'matcher_version':nullable(string()),'items':arr(ref('ScenarioMatch'),maxItems=10),'error':nullable(ref('WorkError'))}, ['status','matcher_version','items','error'])
S['ScenarioMatches']['allOf']=[
 {'if':{'properties':{'status':{'const':'matched'}},'required':['status']},'then':{'properties':{'items':{'minItems':1}}},'else':{'properties':{'items':{'maxItems':0}}}},
 {'if':{'properties':{'status':{'const':'failed'}},'required':['status']},'then':{'properties':{'error':ref('WorkError')}},'else':{'properties':{'error':{'type':'null'}}}},
 {'if':{'properties':{'status':{'const':'not_evaluated'}},'required':['status']},'then':{'properties':{'matcher_version':{'type':'null'}}},'else':{'properties':{'matcher_version':string(minLength=1)}}},
]
add('AlertDetails', {
 'alert_id':ident(),'received_at':timestamp(),'submitted_event':ref('AlertInput'),'normalization':ref('Processing'),
 'context':nullable(ref('AlertContext')),'related_scenarios':ref('ScenarioMatches'),
}, ['alert_id','received_at','submitted_event','normalization','context','related_scenarios'], 'submitted_event는 최초 수신 JSON 객체. 원본 bytea 및 내부 파일 경로는 반환하지 않는다.')
S['AlertDetails']['allOf']=[{'if':{'properties':{'normalization':{'properties':{'status':{'const':'succeeded'}},'required':['status']}},'required':['normalization']},'then':{'properties':{'context':ref('AlertContext')}},'else':{'properties':{'context':{'type':'null'},'related_scenarios':{'properties':{'status':{'const':'not_evaluated'}}}}}}]
add('ContextAnswer', {'question_key':string(minLength=1,maxLength=100),'value':scalar(),'unit':nullable(string(maxLength=64))}, ['question_key','value'], '이전 답변이 요청한 구조화 질문의 응답. 유형·선택지·단위는 그 질문 계약에 맞춰 검사한다.')
add('QuestionInput', {
 'context_id':ident('GET alerts에서 받은 봉인된 context ID'), 'question':string('공백만 있는 질문 불가',minLength=1,maxLength=2000),
 'reply_to_answer_id':nullable(ident()),'context_answers':arr(ref('ContextAnswer'),maxItems=20),
}, ['context_id','question'])
S['QuestionInput']['allOf']=[{'if':{'required':['context_answers'],'properties':{'context_answers':{'minItems':1}}},'then':{'required':['reply_to_answer_id'],'properties':{'reply_to_answer_id':ident()}}}]
add('QuestionAccepted', {
 'answer_id':ident('assistant_runs.id'),'alert_id':ident(),'context_id':ident(),'knowledge_release_id':ident(),
 'status':enum('pending'),'status_url':string(),'created_at':timestamp(),
}, ['answer_id','alert_id','context_id','knowledge_release_id','status','status_url','created_at'], '최초 접수 응답을 멱등 재전송한다. 현재 상태는 status_url로 조회한다.')
add('FollowUpOption', {'value':scalar(),'label':string()}, ['value','label'])
add('FollowUpQuestion', {
 'question_key':string(),'field':string('승인된 맥락 필드 키; 장비 구성/적용성 승인 정보를 덮어쓸 수 없음'),
 'prompt':string(),'value_type':enum('string|number|boolean'),'required':{'type':'boolean'},
 'options':arr(ref('FollowUpOption')),'unit':nullable(string()),
}, ['question_key','field','prompt','value_type','required','options','unit'])
add('Locator', {
 'kind':enum('pdf_page|logical_block'),'page_number':{'type':'integer','minimum':1},
 'printed_page_label':nullable(string()),'char_start':nullable({'type':'integer','minimum':0}),
 'char_end':nullable({'type':'integer','minimum':1}),
 'bbox':nullable(arr({'type':'number','minimum':0,'maximum':1},minItems=4,maxItems=4)),
}, ['kind','page_number','printed_page_label','char_start','char_end','bbox'], 'page_number는 실제 PDF 페이지 또는 논리 블록. PDF 페이지는 1부터 시작. offset은 페이지 Unicode code point 기준 [start,end).')
add('Citation', {
 'citation_id':ident(),'claim_key':string(),'section_id':ident(),'section_title':string(),'section_number':nullable(string()),
 'span_id':ident(),'document_revision_id':ident(),'document_title':string(),'document_url':nullable(string('PDF인 경우에만 이 API의 상대 경로')),
 'quote':string('span 원문에서 서버가 추출한 인용문'), 'reference_scope':enum('applicable|analogous|comparison|unknown'),
 'locator':ref('Locator'),
}, ['citation_id','claim_key','section_id','section_title','section_number','span_id','document_revision_id','document_title','document_url','quote','reference_scope','locator'])
S['Citation']['allOf']=[{'if':{'properties':{'locator':{'properties':{'kind':{'const':'logical_block'}},'required':['kind']}},'required':['locator']},'then':{'properties':{'document_url':{'type':'null'}}}}]
add('EventEvidenceRef', {'kind':enum('observed_code|observation|ml_assessment'),'id':ident()}, ['kind','id'])
add('Claim', {
 'claim_key':string(),'type':enum('observation|hypothesis|manual_fact|procedure_guidance|limitation'),
 'text':string(),'citation_ids':arr(ident()),'event_evidence':arr(ref('EventEvidenceRef')),
}, ['claim_key','type','text','citation_ids','event_evidence'])
add('ProcedureStep', {
 'step_number':{'type':'integer','minimum':1},'claim_key':string('claims의 procedure_guidance 항목 키'), 'instruction':string(),'expected_result':nullable(string()),
 'condition_summary':nullable(string('원문 분기 조건의 검토된 한국어 표현')), 'condition_state':enum('unconditional|satisfied'),
 'citation_ids':arr(ident(),minItems=1),
}, ['step_number','claim_key','instruction','expected_result','condition_summary','condition_state','citation_ids'])
S['ProcedureStep']['allOf']=[{'if':{'properties':{'condition_state':{'const':'satisfied'}},'required':['condition_state']},'then':{'properties':{'condition_summary':string(minLength=1)}}}]
add('ProcedureRecommendation', {
 'procedure_id':ident(),'title':string(),'required_role':string(),'preconditions':arr(string()),
 'applicability_ids':arr(ident(),minItems=1),'steps':arr(ref('ProcedureStep'),minItems=1),
}, ['procedure_id','title','required_role','preconditions','applicability_ids','steps'], '검토된 applicable 근거와 조건 충족이 확인된 절차만 포함. 자동 장비 제어 명령은 포함하지 않는다.')
add('AnswerResult', {
 'summary':string(),'claims':arr(ref('Claim')),'procedures':arr(ref('ProcedureRecommendation')),
 'follow_up_questions':arr(ref('FollowUpQuestion')),'citations':arr(ref('Citation')),'notices':arr(ref('Notice')),
}, ['summary','claims','procedures','follow_up_questions','citations','notices'], '문서 절·페이지는 citations 안에 포함. needs_context/insufficient_evidence에서는 procedures=[]; 근거 부족을 시스템 오류로 취급하지 않는다.')
add('AnswerDetails', {
 'answer_id':ident(),'alert_id':ident(),'context_id':ident(),'knowledge_release_id':ident(),'reply_to_answer_id':nullable(ident()),
 'status':enum('pending|running|answered|needs_context|insufficient_evidence|failed'),
 'question':string(),'started_at':timestamp(),'finished_at':nullable(timestamp()),
 'result':nullable(ref('AnswerResult')),'error':nullable(ref('WorkError')),
}, ['answer_id','alert_id','context_id','knowledge_release_id','reply_to_answer_id','status','question','started_at','finished_at','result','error'])
S['AnswerDetails']['allOf']=[
 {'if':{'properties':{'status':{'enum':['pending','running']}},'required':['status']},'then':{'properties':{'result':{'type':'null'},'error':{'type':'null'},'finished_at':{'type':'null'}}}},
 {'if':{'properties':{'status':{'enum':['answered','needs_context','insufficient_evidence']}},'required':['status']},'then':{'properties':{'result':ref('AnswerResult'),'error':{'type':'null'},'finished_at':timestamp()}}},
 {'if':{'properties':{'status':{'const':'failed'}},'required':['status']},'then':{'properties':{'result':{'type':'null'},'error':ref('WorkError'),'finished_at':timestamp()}}},
 {'if':{'properties':{'status':{'enum':['needs_context','insufficient_evidence']}},'required':['status']},'then':{'properties':{'result':{'properties':{'procedures':{'maxItems':0}}}}}},
 {'if':{'properties':{'status':{'const':'needs_context'}},'required':['status']},'then':{'properties':{'result':{'properties':{'follow_up_questions':{'minItems':1}}}}}},
]
categories=enum('wrong_equipment|wrong_code_mapping|wrong_source|unsupported_claim|missing_context|incorrect_procedure|incorrect_citation|other')
add('FeedbackInput', {
 'verdict':enum('acceptable|needs_revision|incorrect'),'error_categories':arr(categories,maxItems=8,uniqueItems=True),
 'comment':nullable(string(maxLength=2000)),'supersedes_id':nullable(ident('동일 답변에 작성자가 이전에 남긴 피드백')),
}, ['verdict','error_categories'])
S['FeedbackInput']['allOf']=[
 {'if':{'properties':{'verdict':{'const':'acceptable'}},'required':['verdict']},'then':{'properties':{'error_categories':{'maxItems':0}}}},
 {'if':{'properties':{'verdict':{'enum':['needs_revision','incorrect']}},'required':['verdict']},'then':{'properties':{'error_categories':{'minItems':1}}}},
 {'if':{'properties':{'error_categories':{'contains':{'const':'other'}}},'required':['error_categories']},'then':{'required':['comment'],'properties':{'comment':string(minLength=1,maxLength=2000)}}},
]
add('FeedbackCreated', {'feedback_id':ident(),'answer_id':ident(),'reviewer_id':ident('인증 principal에서 결정'),'verdict':enum('acceptable|needs_revision|incorrect'),'error_categories':arr(categories),'comment':nullable(string()),'supersedes_id':nullable(ident()),'created_at':timestamp()}, ['feedback_id','answer_id','reviewer_id','verdict','error_categories','comment','supersedes_id','created_at'])

U={name:f'00000000-0000-4000-8000-{i:012d}' for i,name in enumerate(['alert','context','release','answer','actor','feedback','asset','asset_config','section','span','revision','citation','code','observation','ml','next_answer'],1)}
T='2026-09-26T01:00:00Z'
examples={}
examples['alert_request']={
 'schema_version':'1.0','producer_schema_version':'demo-ml-v1','event_id':'demo-event-0001','producer_key':'demo-ml',
 'asset_key':'RSW-DEMO-001','occurred_at':T,'observation_window':{'start':'2026-09-26T00:59:50Z','end':T},
 'observed_codes':[{'namespace':'demo.dataset.raw_alarm','code':'DEMO-E01','observed_at':T}],
 'observations':[{'feature_key':'demo.cycle_duration','value':1.92,'unit':'s','quality':'valid','observed_at':T}],
 'ml_assessments':[{'kind':'classification','model':{'name':'demo-classifier','version':'demo-v1'},'target':{'namespace':'demo.dataset.fault_class','code':'DEMO-F01'},'score':{'kind':'probability','value':0.82,'calibration_status':'uncalibrated'}}],
 'operating_context':{'cycle_state':'unknown'},
}
processing={'status':'queued','updated_at':T,'error':None}
examples['alert_created']={'alert_id':U['alert'],'event_id':'demo-event-0001','producer_key':'demo-ml','received_at':T,'duplicate':False,'normalization':processing,'status_url':f"/api/v1/alerts/{U['alert']}"}
examples['alert_duplicate']={**examples['alert_created'],'duplicate':True}
examples['alert_details']={
 'alert_id':U['alert'],'received_at':T,'submitted_event':examples['alert_request'],
 'normalization':{'status':'succeeded','updated_at':'2026-09-26T01:00:01Z','error':None},
 'context':{'context_id':U['context'],'knowledge_release_id':U['release'],'knowledge_version':'demo-kb-v1','normalizer_version':'demo-normalizer-v1','status':'partial',
 'asset':{'reported_key':'RSW-DEMO-001','asset_id':U['asset'],'asset_config_id':None,'verification_status':'unresolved'},
 'code_mappings':[{'id':U['code'],'input_index':0,'entity_id':None,'status':'unmapped'}],
 'observation_mappings':[{'id':U['observation'],'input_index':0,'feature_id':None,'binding_id':None,'status':'unmapped'}],
 'ml_mappings':[{'id':U['ml'],'input_index':0,'target_entity_id':None,'target_kind':None}],
 'warnings':[{'code':'ASSET_CONFIG_UNRESOLVED','message':'예시 장비의 구성 정보를 확인해야 합니다.','field':'asset_key'}],
 'sealed_at':'2026-09-26T01:00:01Z'},
 'related_scenarios':{'status':'no_match','matcher_version':'demo-matcher-v1','items':[],'error':None},
}
examples['question_request']={'context_id':U['context'],'question':'이 경보가 의미하는 것과 먼저 확인할 정보를 알려주세요.'}
examples['question_accepted']={'answer_id':U['answer'],'alert_id':U['alert'],'context_id':U['context'],'knowledge_release_id':U['release'],'status':'pending','status_url':f"/api/v1/answers/{U['answer']}",'created_at':'2026-09-26T01:00:05Z'}
base_answer={'answer_id':U['answer'],'alert_id':U['alert'],'context_id':U['context'],'knowledge_release_id':U['release'],'reply_to_answer_id':None,'status':'pending','question':examples['question_request']['question'],'started_at':'2026-09-26T01:00:05Z','finished_at':None,'result':None,'error':None}
examples['answer_pending']=base_answer
examples['answer_needs_context']={**base_answer,'status':'needs_context','finished_at':'2026-09-26T01:00:10Z','result':{
 'summary':'현재 정보로는 장비에 맞는 정비 절차를 확정할 수 없습니다. 운전 상태와 등록된 장비 구성을 추가로 확인해야 합니다.',
 'claims':[{'claim_key':'observed-code','type':'observation','text':'수신 데이터에 예시 코드 DEMO-E01이 포함되어 있습니다.','citation_ids':[],'event_evidence':[{'kind':'observed_code','id':U['code']}]}],
 'procedures':[], 'follow_up_questions':[{'question_key':'cycle_state','field':'operating_context.cycle_state','prompt':'경보 발생 당시 용접 중이었나요?','value_type':'string','required':True,'options':[{'value':'welding','label':'용접 중'},{'value':'non_welding','label':'비용접 상태'}],'unit':None}],
 'citations':[],'notices':[{'code':'ASSET_CONFIG_UNRESOLVED','message':'장비 구성 확인은 도메인 담당자의 별도 검토가 필요합니다.','field':'asset_key'}],
}}
examples['answer_failed']={**base_answer,'status':'failed','finished_at':'2026-09-26T01:01:05Z','error':{'code':'LLM_TIMEOUT','message':'답변 생성 시간이 초과되었습니다.','retryable':True}}
examples['followup_request']={'context_id':U['context'],'question':'비용접 상태에서 발생했습니다. 이 정보를 반영해 설명해주세요.','reply_to_answer_id':U['answer'],'context_answers':[{'question_key':'cycle_state','value':'non_welding','unit':None}]}
examples['citation_example']={
 'citation_id':U['citation'],'claim_key':'example-manual-scope','section_id':U['section'],'section_title':'예시 적용 범위','section_number':'2.1',
 'span_id':U['span'],'document_revision_id':U['revision'],'document_title':'형식 설명용 가상 매뉴얼','document_url':f"/api/v1/documents/{U['revision']}",
 'quote':'이 인용문은 API 형식 설명용 가상 데이터이며 실제 매뉴얼의 내용이 아닙니다.',
 'reference_scope':'analogous','locator':{'kind':'pdf_page','page_number':12,'printed_page_label':'10','char_start':None,'char_end':None,'bbox':None},
}
examples['answer_answered']={**base_answer,'answer_id':U['next_answer'],'question':'형식 설명용 가상 매뉴얼의 예시 적용 범위 절을 보여주세요.','status':'answered','finished_at':'2026-09-26T01:00:10Z','result':{
 'summary':'가상 매뉴얼의 예시 절을 표시합니다. 해당 자료는 유사 참고 자료로 분류되어 정비 절차 안내에 사용하지 않습니다.',
 'claims':[{'claim_key':'example-manual-scope','type':'manual_fact','text':examples['citation_example']['quote'],'citation_ids':[U['citation']],'event_evidence':[]}],
 'procedures':[],'follow_up_questions':[],'citations':[examples['citation_example']],
 'notices':[{'code':'ANALOGOUS_REFERENCE','message':'API 형식 설명용 가상 자료입니다.','field':None}],
}}
examples['feedback_request']={'verdict':'needs_revision','error_categories':['missing_context'],'comment':'제어기 모델 확인도 먼저 요청하면 좋겠습니다.','supersedes_id':None}
examples['feedback_created']={'feedback_id':U['feedback'],'answer_id':U['answer'],'reviewer_id':U['actor'],**examples['feedback_request'],'created_at':'2026-09-26T01:02:00Z'}
examples['conflict_problem']={'type':'about:blank','title':'Conflict','status':409,'detail':'같은 생산자의 사건 ID에 다른 내용이 이미 등록되어 있습니다.','instance':'/api/v1/alerts','code':'EVENT_ID_CONFLICT','request_id':'00000000-0000-4000-8000-000000000099','errors':[]}

response_headers={'X-Request-ID':{'description':'서버 요청 추적 UUID','schema':ident()},'Cache-Control':{'schema':string(),'description':'JSON 응답은 no-store'}}
def json_response(name, desc, example_names=(), extra_headers=None):
    media={'schema':ref(name)}
    if example_names: media['examples']={n:{'value':examples[n]} for n in example_names}
    return {'description':desc,'headers':{**response_headers,**(extra_headers or {})},'content':{'application/json':media}}
def error_response(desc, headers=None):
    return {'description':desc,'headers':{**response_headers,**(headers or {})},'content':{'application/problem+json':{'schema':ref('Problem')}}}
retry_header={'Retry-After':{'description':'재조회/재시도까지 권장 대기 초','schema':{'type':'integer','minimum':1}}}
replay_header={'Idempotency-Replayed':{'description':'서버 자체 재생 표시 헤더; 표준 헤더라는 의미가 아님','schema':enum('true|false')}}
params={
 'AlertId':{'name':'id','in':'path','required':True,'schema':ident('서버 alert_id; 송신 event_id가 아님')},
 'AnswerId':{'name':'id','in':'path','required':True,'schema':ident('answer_id = assistant_runs.id')},
 'RevisionId':{'name':'revision_id','in':'path','required':True,'schema':ident('document_revisions.id')},
 'IdempotencyKey':{'name':'Idempotency-Key','in':'header','required':True,'schema':string(minLength=1,maxLength=128,pattern='^[A-Za-z0-9._:-]+$'),'description':'논리적 요청마다 새 키. 동일 재시도는 같은 키/같은 JSON. 보관 기간 최소 24시간.'},
 'ContextQuery':{'name':'context_id','in':'query','required':False,'schema':ident(),'description':'이 경보에 속한 특정 봉인 context. 생략 시 기본 context. 선택한 context를 질문에 반드시 다시 전달.'},
 'Range':{'name':'Range','in':'header','required':False,'schema':string(),'description':'단일 bytes 범위만 지원. 다중/해석 불가 Range는 무시하고 200 전체 파일, 만족 불가한 단일 byte 범위는 416.'},
 'IfNoneMatch':{'name':'If-None-Match','in':'header','required':False,'schema':string(),'description':'ETag 검증. 일치하면 304. 인증 후 평가하며 Range보다 먼저 평가.'},
 'IfRange':{'name':'If-Range','in':'header','required':False,'schema':string(),'description':'서버가 발급한 강한 ETag만 지원. 불일치/날짜/약한 validator는 Range 무시 후 200.'},
}
def p(name): return {'$ref':f'#/components/parameters/{name}'}
def body(name, ex): return {'required':True,'content':{'application/json':{'schema':ref(name),'examples':{n:{'value':examples[n]} for n in ex}}}}
def common_errors(write=False):
    e={'401':error_response('인증 필요',{'WWW-Authenticate':{'schema':string(),'description':'Bearer'}}),'403':error_response('이 작업 또는 리소스에 대한 권한 부족'),'429':error_response('요청 제한',retry_header),'500':error_response('내부 오류'),'503':error_response('일시적으로 서비스 사용 불가',retry_header)}
    if write: e.update({'400':error_response('잘못된 JSON/중복 JSON 키/필수 헤더 누락'),'413':error_response('요청 크기 초과'),'415':error_response('application/json이 아닌 요청 또는 지원하지 않는 Content-Encoding'),'422':error_response('필드 타입·범위·의미 검증 실패')})
    else: e['422']=error_response('경로/쿼리 형식 검증 실패')
    return e

paths={
 '/api/v1/alerts':{'post':{
  'operationId':'createAlert','summary':'ML 경보 원본 접수','tags':['Alerts'],'x-required-role':['producer_service'],
  'description':'신규 경보 및 normalization 작업을 한 DB 트랜잭션으로 저장한다. 최초 접수에는 활성 published release가 필요. 중복은 producer_key+event_id와 원본 JSON 구조로 비교한다. 같은 내용 재수신은 작업을 추가하지 않는다.',
  'requestBody':body('AlertInput',['alert_request']),
  'responses':{**common_errors(True),'201':json_response('AlertAccepted','신규 수신 완료; 해석 대기',['alert_created'],{'Location':{'schema':string()},**retry_header}),'200':json_response('AlertAccepted','같은 사건·같은 내용 재수신',['alert_duplicate']),'409':error_response('EVENT_ID_CONFLICT')},
 }},
 '/api/v1/alerts/{id}':{'get':{
  'operationId':'getAlert','summary':'경보와 정규화 결과·관련 시나리오 조회','tags':['Alerts'],'parameters':[p('AlertId'),p('ContextQuery')],
  'description':'context_id 지정 시 해당 봉인 결과를 조회. 생략 시 기본 정규화 작업 결과. 대기/작업 실패도 리소스 조회는 200. 제출 원본과 해석 결과를 분리한다.',
  'responses':{**common_errors(),'200':json_response('AlertDetails','경보 상세',['alert_details'],retry_header),'404':error_response('경보 또는 해당 경보의 context를 찾을 수 없음')},
 }},
 '/api/v1/alerts/{id}/questions':{'post':{
  'operationId':'createQuestion','summary':'작업자 질문 접수 및 답변 실행 생성','tags':['Answers'],'parameters':[p('AlertId'),p('IdempotencyKey')],
  'requestBody':body('QuestionInput',['question_request','followup_request']),
  'description':'봉인 context와 지식 release를 고정한다. 실행/작업/멱등 기록을 한 트랜잭션으로 생성한다. 반환 answer_id는 assistant_runs.id. 실제 답변은 status_url에서 조회한다.',
  'responses':{**common_errors(True),'202':json_response('QuestionAccepted','답변 실행 접수; 멱등 재시도 시 최초 응답 재생',['question_accepted'],{'Location':{'schema':string()},**retry_header,**replay_header}),'404':error_response('경보/context/이전 답변 없음'),'409':error_response('ALERT_NOT_READY, CONTEXT_ALERT_MISMATCH, CONTEXT_NOT_USABLE, REPLY_CONTEXT_MISMATCH, ANSWER_NOT_READY, IDEMPOTENCY_KEY_REUSED, REQUEST_IN_PROGRESS',retry_header)},
 }},
 '/api/v1/answers/{id}':{'get':{
  'operationId':'getAnswer','summary':'추가 필수 API: 비동기 답변 상태·결과 조회','tags':['Answers'],'parameters':[p('AnswerId')],
  'description':'pending/running은 result=null. needs_context/insufficient_evidence는 정상 완료. 실패 상태도 HTTP 200이며 error에 실패 사유를 제공한다.',
  'responses':{**common_errors(),'200':json_response('AnswerDetails','답변 실행 상태와 결과',['answer_pending','answer_answered','answer_needs_context','answer_failed'],retry_header),'404':error_response('답변 실행 없음')},
 }},
 '/api/v1/documents/{revision_id}':{'get':{
  'operationId':'getDocumentPdf','summary':'봉인된 판본의 등록 원본 PDF 제공','tags':['Documents'],'parameters':[p('RevisionId'),p('Range'),p('IfNoneMatch'),p('IfRange')],
  'description':'원본 PDF에 한정한다. #page=N은 브라우저 fragment이며 서버 페이지 파라미터가 아니다. logical_block에는 PDF 점프를 만들지 않는다. 인증을 ETag/Range 처리보다 먼저 한다.',
  'responses':{**common_errors(),
   '200':{'description':'PDF 전체 바이트','headers':{'Content-Disposition':{'schema':string(),'description':'inline; 안전한 filename/filename*'},'Content-Length':{'schema':{'type':'integer','minimum':0}},'ETag':{'schema':string(),'description':'실제 제공 파일 SHA-256으로 만든 강한 ETag'},'Accept-Ranges':{'schema':string(const='bytes')},'Cache-Control':{'schema':string(const='private, no-cache')}},'content':{'application/pdf':{}}},
   '206':{'description':'만족 가능한 단일 byte 범위','headers':{'Content-Range':{'schema':string(),'description':'bytes START-END/TOTAL'},'Content-Length':{'schema':{'type':'integer','minimum':1}},'ETag':{'schema':string()},'Accept-Ranges':{'schema':string(const='bytes')},'Cache-Control':{'schema':string(const='private, no-cache')}},'content':{'application/pdf':{}}},
   '304':{'description':'ETag 일치; 응답 body 없음','headers':{'ETag':{'schema':string()},'Cache-Control':{'schema':string(const='private, no-cache')}}},
   '404':error_response('DOCUMENT_NOT_FOUND'),'409':error_response('DOCUMENT_NOT_PDF 또는 DOCUMENT_NOT_SEALED'),
   '416':error_response('RANGE_NOT_SATISFIABLE',{'Content-Range':{'schema':string(),'description':'bytes */TOTAL'}}),
  },
 }},
 '/api/v1/answers/{id}/feedback':{'post':{
  'operationId':'createFeedback','summary':'답변 적절성 피드백 추가','tags':['Feedback'],'parameters':[p('AnswerId'),p('IdempotencyKey')],
  'requestBody':body('FeedbackInput',['feedback_request']),
  'description':'reviewer는 인증 principal로 결정. 완료된 answered/needs_context/insufficient_evidence만 평가 가능. 수정은 동일 작성자·동일 답변의 supersedes_id로 새 행 생성.',
  'responses':{**common_errors(True),'201':json_response('FeedbackCreated','피드백 생성; 멱등 재시도는 최초 응답 재생',['feedback_created'],replay_header),'404':error_response('답변 또는 정정 대상 피드백 없음'),'409':error_response('ANSWER_NOT_REVIEWABLE, FEEDBACK_ALREADY_SUPERSEDED, FEEDBACK_TARGET_MISMATCH, IDEMPOTENCY_KEY_REUSED, REQUEST_IN_PROGRESS',retry_header)},
 }},
}
spec={
 'openapi':'3.1.1','info':{'title':'RSW Maintenance MVP Integration API','version':'1.0.0-draft','description':'구현 전 제안 계약. README 5.10의 실제 payload와 별도 대조 필요. 기존 5개 API와 비동기 결과 조회 1개를 명세한다. 모든 예시는 합성 데이터다.'},
 'servers':[{'url':'/','description':'API 서비스와 같은 origin'}],
 'security':[{'bearerAuth':[]}],
 'tags':[{'name':x} for x in ['Alerts','Answers','Documents','Feedback']],
 'paths':paths,
 'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer','description':'ML 생산자용 서비스 토큰 또는 사용자 토큰. 토큰 발급 방식은 이 계약 범위 밖이다.'}},'parameters':params,'schemas':S},
}
ROOT.mkdir(parents=True,exist_ok=True)
(ROOT/'openapi.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
(ROOT/'examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2)+'\n')
template = ROOT/'API_SPEC.template.md'
if template.exists():
    def pretty_type(schema):
        if '$ref' in schema: return schema['$ref'].split('/')[-1]
        if 'anyOf' in schema: return ' 또는 '.join(pretty_type(x) for x in schema['anyOf'])
        t = schema.get('type', '조건 스키마')
        if isinstance(t,list): return '/'.join(t)
        if t=='array': return 'array<' + pretty_type(schema['items']) + '>'
        return t + (f" ({schema['format']})" if 'format' in schema else '')
    def cell(value): return str(value).replace('|','\\|').replace('\n',' ')
    dictionary=[]
    for name,schema in S.items():
        dictionary.extend([f'\n### `{name}`\n',schema.get('description','')+'\n','| 필드 | 타입 | 필수 | 의미·범위 |','|---|---|---|---|'])
        for name_,field in schema['properties'].items():
            notes=[field.get('description','')]
            if 'enum' in field: notes.append('값: '+', '.join(map(str,field['enum'])))
            if 'const' in field: notes.append('고정값: '+str(field['const']))
            for key,label in [('minLength','최소 길이'),('maxLength','최대 길이'),('minItems','최소 개수'),('maxItems','최대 개수'),('minimum','최솟값'),('maximum','최댓값')]:
                if key in field: notes.append(f'{label}: {field[key]}')
            dictionary.append(f"| `{name_}` | {cell(pretty_type(field))} | {'O' if name_ in schema['required'] else '선택'} | {cell('; '.join(x for x in notes if x))} |")
    doc=template.read_text().replace('{{schema_dictionary}}','\n'.join(dictionary))
    for name,value in examples.items():
        doc=doc.replace('{{example.'+name+'}}','```json\n'+json.dumps(value,ensure_ascii=False,indent=2)+'\n```')
    if '{{' in doc: raise ValueError('Unexpanded documentation placeholder')
    (ROOT/'API_SPEC.md').write_text(doc)
print(f'Generated OpenAPI: {len(paths)} operations, {len(S)} schemas, {len(examples)} synthetic examples.')
