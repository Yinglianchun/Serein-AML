import copy
import pytest
from aml import runtime
from serein.extensions import pipeline_latest as curator
from serein.extensions.pipeline_policy import editorial_review_scope
from tests_aml.test_curator_track_scope import bridge_request, compact_event
from tests_aml.test_engine import memory, role_output, add
from serein.extensions import pipeline

@pytest.mark.parametrize('roots', [['U1','U2'],[1,2]])
def test_labels_keep_existing_bridge_ownership(monkeypatch, roots):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=bridge_request()
 out={'events':[compact_event('visit',roots)],'skip_unit_roots':[],'defer_unit_roots':[]}
 saved=copy.deepcopy(out)
 normalized=runtime.prepare_curator_output(request,out)
 with editorial_review_scope(False):
  plan=curator.normalize_event_curator_output(normalized,request['component'])
 assert [b['source_message_id'] for b in plan['events'][0]['source_bindings']]==[1,2]
 assert out==saved

@pytest.mark.parametrize('roots', [[81,82],['U9'],['U1','U1'],['U1',1]])
def test_unknown_predecessor_and_duplicate_choices_still_fail(monkeypatch,roots):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=bridge_request()
 out={'events':[compact_event('visit',roots)],'skip_unit_roots':[2],'defer_unit_roots':[]}
 normalized=runtime.prepare_curator_output(request,out)
 with editorial_review_scope(False),pytest.raises(ValueError):
  curator.normalize_event_curator_output(normalized,request['component'])

def test_choices_exclude_readonly_and_partial_units():
 component={'messages':[{'id':83},{'id':84}], 'memberships':[
  {'unit_root_message_id':81,'source_message_ids':[81,82]},
  {'unit_root_message_id':83,'source_message_ids':[83,84]},
  {'unit_root_message_id':85,'source_message_ids':[85]},
  {'unit_root_message_id':84,'source_message_ids':[84,86]}]}
 assert runtime.curator_unit_choices(component)=={'U1':83}

def test_labels_only_in_relaxed_mode(monkeypatch):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','0')
 out={'events':[compact_event('visit',['U1'])],'skip_unit_roots':['U2'],'defer_unit_roots':[]}
 assert runtime.prepare_curator_output(bridge_request(),out)==out

def test_skip_and_defer_labels_resolve_without_inventing_decisions(monkeypatch):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 out={'events':[],'skip_unit_roots':['U1'],'defer_unit_roots':['U2']}
 normalized=runtime.prepare_curator_output(bridge_request(),out)
 assert normalized=={'events':[],'skip_unit_roots':[1],'defer_unit_roots':[2]}


def numbered_request(ids):
 component={'track_ids':['visit'], 'messages':[{'id':i,'content':f'Synthetic fact {i}.',
              'created_at':'2026-01-01T00:00:00+00:00'} for i in ids],
            'context_messages':[], 'parked_context_source_ids':[], 'base_event_candidates':[],
            'memberships':[{'unit_root_message_id':i,'source_message_ids':[i],'track_id':'visit'} for i in ids],
            'context_edges':[]}
 component['context_messages']=copy.deepcopy(component['messages'])
 return {'role':'event_curator','component':component,'rules':'strict',
         'prompt':curator.build_event_track_curator_prompt('2026-01-01',component)}


@pytest.mark.parametrize('split', [False,True])
def test_noncontinuous_actual_roots_preserve_source_accounting(monkeypatch,split):
 import json
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 ids=[1,2,3,4,5,6,8,9,10,11,12]
 request=numbered_request(ids)
 owned=[1,2,3,4,5,6,10,11] if split else ids
 skipped=[8,9,12] if split else []
 raw={'events':[compact_event('visit',owned)],'skip_unit_roots':skipped,'defer_unit_roots':[]}
 normalized=runtime.prepare_curator_output(request,runtime.stage_json(json.dumps(raw),'event_curator'))
 with editorial_review_scope(False):plan=curator.normalize_event_curator_output(normalized,request['component'])
 assert [row['source_message_id'] for row in plan['events'][0]['source_bindings']]==owned
 assert plan['skip_source_message_ids']==skipped


@pytest.mark.parametrize('split,error', [(False,'ownership'),(True,'skip')])
def test_legacy_u12_remains_invalid_and_u8_remains_ordinal(monkeypatch,split,error):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 ids=[1,2,3,4,5,6,8,9,10,11,12]
 request=numbered_request(ids)
 assert runtime.curator_unit_choices(request['component'])['U8']==9
 owned=[f'U{i}' for i in ([1,2,3,4,5,6,10,11] if split else ids)]
 raw={'events':[compact_event('visit',owned)],'skip_unit_roots':['U8','U9','U12'] if split else [],'defer_unit_roots':[]}
 normalized=runtime.prepare_curator_output(request,raw)
 assert 'U12' in normalized['skip_unit_roots'] if split else 'U12' in normalized['events'][0]['owned_unit_roots']
 with editorial_review_scope(False),pytest.raises(ValueError,match=error+' contains an invalid unit root'):
  curator.normalize_event_curator_output(normalized,request['component'])


def test_prompt_uses_high_real_root_and_excludes_partial_readonly(monkeypatch):
 import json
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=numbered_request([83,84])
 request['component']['memberships']=[
  {'unit_root_message_id':81,'source_message_ids':[81,82],'track_id':'visit'},
  {'unit_root_message_id':83,'source_message_ids':[83,84],'track_id':'visit'},
  {'unit_root_message_id':84,'source_message_ids':[84,86],'track_id':'visit'}]
 frozen=copy.deepcopy(request)
 _,prompt=runtime.writer_payload(request)
 roots=json.loads(prompt.split('Writable stable unit roots: ',1)[1].splitlines()[0])
 example=json.loads(prompt.split('Return JSON: ',1)[1].split('. Choose actual',1)[0])
 assert roots==[83] and example['events'][0]['owned_unit_roots']==[83]
 assert '"before_message_id":83' in prompt
 assert 'U1' not in prompt and 'U-label' not in prompt
 assert request==frozen


@pytest.mark.parametrize('owned', [[83,83],[999],['U99'],[84]])
def test_new_prompt_does_not_relax_duplicate_unknown_or_readonly_roots(monkeypatch,owned):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=numbered_request([83])
 request['component']['memberships'].append({'unit_root_message_id':84,'source_message_ids':[84],'track_id':'visit'})
 raw={'events':[compact_event('visit',owned)],'skip_unit_roots':[],'defer_unit_roots':[]}
 normalized=runtime.prepare_curator_output(request,raw)
 with editorial_review_scope(False),pytest.raises(ValueError):
  curator.normalize_event_curator_output(normalized,request['component'])


def test_no_complete_writable_unit_has_no_invented_example_root(monkeypatch):
 import json
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=numbered_request([83])
 request['component']['memberships']=[{'unit_root_message_id':83,'source_message_ids':[83,84],'track_id':'visit'}]
 _,prompt=runtime.writer_payload(request)
 assert 'Writable stable unit roots: []' in prompt
 example=json.loads(prompt.split('Return JSON: ',1)[1].split('. Choose actual',1)[0])
 assert example=={'events':[],'skip_unit_roots':[],'defer_unit_roots':[]}


def test_partial_unit_public_validation_is_not_relaxed(monkeypatch):
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=numbered_request([83])
 request['component']['memberships'][0]['source_message_ids']=[83,84]
 output={'events':[compact_event('visit',[83])],'skip_unit_roots':[],'defer_unit_roots':[]}
 normalized=runtime.prepare_curator_output(request,output)
 with editorial_review_scope(False),pytest.raises(ValueError,match='compact stable unit is invalid'):
  curator.normalize_event_curator_output(normalized,request['component'])


def test_host_ids_and_retry_correction_use_only_real_roots(memory,monkeypatch):
 import json
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 monkeypatch.setattr(pipeline,'advance',pipeline.advance.original)
 current,prompts={},[]
 original=runtime.run_stage
 async def observed(settings,role,request):
  current.update(role=role,request=request)
  return await original(settings,role,request)
 async def complete(model,payload,**kwargs):
  role,request=current['role'],current['request']
  output=role_output(role,request)
  if role=='event_curator':
   roots=list(runtime.curator_unit_choices(request['component']).values())
   prompt=payload['messages'][1]['content']
   prompts.append(prompt)
   constraints=json.loads(prompt.split('HOST_IDS:\n',1)[1].splitlines()[0])
   assert constraints['stable_unit_roots']==roots
   assert 'writable_unit_choices' not in constraints
   assert 'U-label' not in prompt and 'Writable unit choices' not in prompt
   if len(prompts)==1:output['events'][0]['owned_unit_roots']=[999999]
   else:
    assert 'Select only these actual integer writable roots for owned/skip/defer: '+runtime.encode(roots) in prompt
    output['events'][0]['owned_unit_roots']=roots
  return {'choices':[{'message':{'content':json.dumps(output)}}]}
 monkeypatch.setattr(runtime,'run_stage',observed)
 monkeypatch.setattr('serein.model_runtime.complete',complete)
 assert add() and len(prompts)==2


def test_current_units_follow_history_without_changing_payload(monkeypatch):
 import json
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request=numbered_request([83,84])
 frozen=copy.deepcopy(request)
 before=json.JSONDecoder().raw_decode(request['prompt'].split('<event_curator_input_json>',1)[1].lstrip())[0]
 _,prompt=runtime.writer_payload(request)
 after=json.JSONDecoder().raw_decode(prompt.split('<event_curator_input_json>',1)[1].lstrip())[0]
 assert after==before and list(after)[-1]=='units'
 assert prompt.endswith('Allowed owned/skip/defer roots: [83,84]')
 assert request==frozen
