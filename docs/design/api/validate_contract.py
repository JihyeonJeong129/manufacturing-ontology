"""Static checks for this contract and its JSON Schema subset.

Not an official OpenAPI/JSON Schema conformance validator, API integration test,
database test, HTTP/PDF test, or SDK generation test. Uses no external packages.
"""
from pathlib import Path
from copy import deepcopy
from datetime import datetime
from uuid import UUID
import json, math, re

ROOT=Path(__file__).parent
spec=json.loads((ROOT/'openapi.json').read_text())
examples=json.loads((ROOT/'examples.json').read_text())

def resolve(pointer):
    assert pointer.startswith('#/'), pointer
    current=spec
    for token in pointer[2:].split('/'):
        current=current[token.replace('~1','/').replace('~0','~')]
    return current

class Invalid(ValueError): pass

def validate(value,schema,path='$'):
    def fail(message): raise Invalid(f'{path}: {message}')
    def matches(sub):
        try: validate(value,sub,path); return True
        except Invalid: return False
    if '$ref' in schema: validate(value,resolve(schema['$ref']),path)
    types=schema.get('type')
    def is_type(t):
        return {
          'null':value is None,'boolean':type(value) is bool,'integer':type(value) is int,
          'number':type(value) in (int,float) and (not isinstance(value,float) or math.isfinite(value)),
          'string':isinstance(value,str),'array':isinstance(value,list),'object':isinstance(value,dict),
        }.get(t,False)
    if types and not any(is_type(t) for t in (types if isinstance(types,list) else [types])): fail(f'expected {types}')
    if 'const' in schema and value!=schema['const']: fail('const mismatch')
    if 'enum' in schema and value not in schema['enum']: fail('enum mismatch')
    if 'anyOf' in schema and not any(matches(x) for x in schema['anyOf']): fail('anyOf failed')
    if 'oneOf' in schema and sum(matches(x) for x in schema['oneOf'])!=1: fail('oneOf failed')
    for x in schema.get('allOf',[]): validate(value,x,path)
    if 'if' in schema:
        branch='then' if matches(schema['if']) else 'else'
        if branch in schema: validate(value,schema[branch],path)
    if isinstance(value,dict):
        for key in schema.get('required',[]):
            if key not in value: fail(f'missing {key}')
        props=schema.get('properties',{})
        if schema.get('additionalProperties') is False and any(k not in props for k in value): fail('unknown property')
        for key in value:
            if key in props: validate(value[key],props[key],path+'.'+key)
        if len(value)<schema.get('minProperties',0): fail('minProperties')
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',math.inf): fail('item count')
        for i,item in enumerate(value):
            if 'items' in schema: validate(item,schema['items'],f'{path}[{i}]')
        if schema.get('uniqueItems') and len({json.dumps(x,sort_keys=True) for x in value})!=len(value): fail('duplicate items')
        if 'contains' in schema:
            valid=0
            for i,item in enumerate(value):
                try: validate(item,schema['contains'],f'{path}[{i}]'); valid+=1
                except Invalid: pass
            if valid==0: fail('contains failed')
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',math.inf): fail('string length')
        if 'pattern' in schema and re.search(schema['pattern'],value) is None: fail('pattern mismatch')
        if schema.get('format')=='uuid':
            try: UUID(value)
            except (ValueError,AttributeError): fail('UUID format')
        if schema.get('format')=='date-time':
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                if parsed.tzinfo is None or 'T' not in value: fail('timezone required')
            except ValueError: fail('date-time format')
    if type(value) in (int,float):
        if not math.isfinite(value): fail('non-finite number')
        if value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf): fail('numeric range')

ref_count=0
def check_refs(node):
    global ref_count
    if isinstance(node,dict):
        if '$ref' in node: resolve(node['$ref']); ref_count+=1
        for value in node.values(): check_refs(value)
    elif isinstance(node,list):
        for value in node: check_refs(value)
check_refs(spec)

operation_ids=set()
example_count=0
for path,item in spec['paths'].items():
    for method,operation in item.items():
        assert method in ['get','post']
        assert operation['operationId'] not in operation_ids
        operation_ids.add(operation['operationId'])
        params=[resolve(p['$ref']) if '$ref' in p else p for p in operation.get('parameters',[])]
        assert {p['name'] for p in params if p['in']=='path'}==set(re.findall(r'\{([^}]+)\}',path))
        for p in params:
            if p['in']=='path': assert p['required'] is True
        payloads=[]
        if 'requestBody' in operation: payloads.extend(operation['requestBody']['content'].values())
        for response in operation['responses'].values(): payloads.extend(response.get('content',{}).values())
        for media in payloads:
            for example in media.get('examples',{}).values():
                validate(example['value'],media['schema']); example_count+=1

mapping={
 'alert_request':'AlertInput','alert_created':'AlertAccepted','alert_duplicate':'AlertAccepted','alert_details':'AlertDetails',
 'question_request':'QuestionInput','question_accepted':'QuestionAccepted','answer_pending':'AnswerDetails',
 'answer_needs_context':'AnswerDetails','answer_answered':'AnswerDetails','answer_failed':'AnswerDetails','followup_request':'QuestionInput',
 'citation_example':'Citation','feedback_request':'FeedbackInput','feedback_created':'FeedbackCreated','conflict_problem':'Problem',
}
assert set(mapping)==set(examples)
for name,schema in mapping.items(): validate(examples[name],resolve('#/components/schemas/'+schema))

negative=[]
def reject(name,example,schema,mutate):
    value=deepcopy(examples[example]); mutate(value)
    try: validate(value,resolve('#/components/schemas/'+schema))
    except Invalid: negative.append(name); return
    raise AssertionError('Invalid fixture accepted: '+name)
reject('missing producer','alert_request','AlertInput',lambda v:v.pop('producer_key'))
reject('unexpected source authority','alert_request','AlertInput',lambda v:v.update(release_id='forbidden'))
reject('probability > 1','alert_request','AlertInput',lambda v:v['ml_assessments'][0]['score'].update(value=1.1))
reject('classification without target','alert_request','AlertInput',lambda v:v['ml_assessments'][0].pop('target'))
reject('calibration without reference','alert_request','AlertInput',lambda v:v['ml_assessments'][0]['score'].update(calibration_status='calibrated'))
reject('valid observation without value','alert_request','AlertInput',lambda v:v['observations'][0].pop('value'))
reject('naive timestamp','alert_request','AlertInput',lambda v:v.update(occurred_at='2026-09-26T01:00:00'))
reject('followup missing parent','followup_request','QuestionInput',lambda v:v.pop('reply_to_answer_id'))
reject('question bad UUID','question_request','QuestionInput',lambda v:v.update(context_id='event-id'))
reject('needs_context empty questions','answer_needs_context','AnswerDetails',lambda v:v['result'].update(follow_up_questions=[]))
reject('failure missing error','answer_failed','AnswerDetails',lambda v:v.update(error=None))
reject('pending with result','answer_pending','AnswerDetails',lambda v:v.update(result=examples['answer_needs_context']['result']))
reject('acceptable with errors','feedback_request','FeedbackInput',lambda v:v.update(verdict='acceptable'))
reject('other without comment','feedback_request','FeedbackInput',lambda v:v.update(error_categories=['other'],comment=None))
reject('client reviewer injection','feedback_request','FeedbackInput',lambda v:v.update(reviewer_id='forbidden'))
reject('job failed without error','alert_created','AlertAccepted',lambda v:v['normalization'].update(status='failed'))
reject('matched without scenarios','alert_details','AlertDetails',lambda v:v['related_scenarios'].update(status='matched'))
reject('invalid PDF page','citation_example','Citation',lambda v:v['locator'].update(page_number=0))
reject('completed normalization missing context','alert_details','AlertDetails',lambda v:v.update(context=None))
reject('logical block with PDF URL','citation_example','Citation',lambda v:v['locator'].update(kind='logical_block'))

doc=(ROOT/'API_SPEC.md').read_text()
assert '{{' not in doc
assert len(re.findall(r'^```',doc,re.M))%2==0
for name in spec['components']['schemas']: assert f'### `{name}`' in doc
assert len(spec['paths'])==6
print(f'PASS: {len(operation_ids)} operations; {len(spec["components"]["schemas"])} schemas; {ref_count} local references resolved.')
print(f'PASS: {len(examples)} standalone and {example_count} embedded examples; {len(negative)} negative fixtures rejected.')
print('PASS: document schema coverage and code-fence balance.')
print('LIMIT: custom subset validation only; no official OAS conformance, HTTP server, PostgreSQL, worker, PDF or LLM integration executed.')
