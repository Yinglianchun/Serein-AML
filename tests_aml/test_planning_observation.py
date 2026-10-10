import copy
import json
import logging
import pytest
from aml import arc_planning, delivery
from serein.extensions import pipeline_latest as projection

@pytest.mark.parametrize('origin', ['source','ingestion',None])
@pytest.mark.parametrize('render', [projection.transcript_payload,projection.writer_transcript_payload,projection.event_track_message_payload])
def test_source_time_distinguishes_receipt_without_mutating_sources(origin,render):
 item={'id':1,'role':'user','content':'I applied last Wednesday.','created_at':'2024-09-10T00:00:00+00:00','metadata':{}}
 if origin:item['metadata']['aml_time_origin']=origin
 before=copy.deepcopy(item)
 row=render([item])[0]
 assert item==before and row['text']==item['content']
 if origin=='ingestion':
  assert row['created_at'] is None and row['message_time_origin']=='unknown'
 elif origin=='source':
  assert row['created_at'] and row['message_time_origin']=='source'
 else:assert 'message_time_origin' not in row and row['created_at']

@pytest.mark.parametrize('variant,stage', [('sufficient','arc_plan_sufficient'),('invalid','arc_plan_invalid'),('empty','arc_plan_no_selection'),('selected','arc_plan_selected'),('call_error','arc_plan_model_error')])
def test_planner_statuses_are_content_free(monkeypatch,caplog,variant,stage):
 monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT','1')
 monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','0')
 evidence=[{'ref':'private-id','text':'Lin applied.'}]
 menu={'arc':{'title':'PRIVATE TITLE','materials':[{'index':0}]}}
 result={'sufficient':variant=='sufficient','supports':[{'ref':'private-id','quote':'Lin applied.'}], 'missing':[] if variant=='sufficient' else ['approval date'],'selections':[]}
 if variant=='selected':result['selections']=[{'arc_key':'arc','picks':[0],'gap':0}]
 if variant=='invalid':result.pop('supports')
 def model(prompt):
  if variant=='call_error':raise ValueError('SECRET MODEL ERROR')
  return result
 with caplog.at_level(logging.WARNING),delivery.request():
  if variant=='call_error':
   with pytest.raises(ValueError):arc_planning.assess(model,'SECRET QUESTION',None,evidence,menu)
  else:arc_planning.assess(model,'SECRET QUESTION',None,evidence,menu)
 events=[json.loads(r.message.split('aml_delivery ',1)[1]) for r in caplog.records if 'aml_delivery ' in r.message]
 assert stage in [e['stage'] for e in events]
 assert all(token not in caplog.text for token in ['SECRET','Lin applied','PRIVATE TITLE','private-id'])


def test_mixed_known_unknown_times_keep_internal_order():
 from tests_aml.test_curator_unit_choices import numbered_request
 request=numbered_request([1,2])
 rows=request['component']['context_messages']
 rows[0]['metadata']={'aml_time_origin':'ingestion'}
 rows[1]['metadata']={'aml_time_origin':'source'}
 before=copy.deepcopy(request)
 result=projection.event_curator_model_input(request['component'])
 assert [u['root'] for u in result['units']]==[1,2]
 assert result['units'][0]['messages'][0]['created_at'] is None
 assert result['units'][1]['messages'][0]['created_at']
 assert request==before


@pytest.mark.parametrize('text,stamp,expected', [
 ('last Wednesday','2024-09-10T12:00:00+08:00','2024-09-04'),
 ('上周三','2024-09-10T12:00:00+08:00','2024-09-04'),
 ('两个月前','2024-03-31T12:00:00+08:00','2024-01-31'),
 ('1 month ago','2024-03-31T12:00:00+08:00','2024-02-29'),
 ('昨天','2024-01-01T00:01:00+08:00','2023-12-31'),
 ('last Wednesday',None,None)])
def test_calendar_hints_use_source_day_only(text,stamp,expected):
 from aml.message_time import annotate
 row={'text':text,'created_at':stamp,'message_time_origin':'source' if stamp else 'unknown'}
 result=annotate([row])[0]['relative_date_notes'][0]
 assert result['date']==expected


def test_writer_unknown_note_is_grounded_and_idempotent(monkeypatch):
 from aml import runtime
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 row={'text':'I applied last Wednesday.','created_at':None,'message_time_origin':'unknown','evidence_role':'owned'}
 request={'role':'event_writer','prompt':'<event_reading_block_json>'+json.dumps([row])+'</event_reading_block_json>'}
 original={'event_draft':'Lin applied last Wednesday.'}
 out=runtime.prepare_writer_output(request,original)
 assert 'Exact date unknown' in out['event_draft']
 assert original['event_draft']=='Lin applied last Wednesday.'
 assert runtime.prepare_writer_output(request,out)==out
 row['message_time_origin']='source';row['created_at']='2024-09-10T12:00:00+08:00'
 request['prompt']='<event_reading_block_json>'+json.dumps([row])+'</event_reading_block_json>'
 assert runtime.prepare_writer_output(request,original)['event_draft']==original['event_draft']


def test_aml_source_timestamp_is_not_shifted_to_public_writer_timezone():
 row={'id':1,'role':'user','content':'Yesterday.','created_at':'2024-09-10T23:30:00+00:00','metadata':{'aml_time_origin':'source'}}
 assert projection.writer_transcript_payload([row])[0]['created_at']==row['created_at']

@pytest.mark.parametrize('block', ['FROZEN', '{}', '[1]'])
def test_optional_time_note_leaves_invalid_packets_to_normal_validation(monkeypatch, block):
 from aml import runtime
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 request={'role':'event_writer','prompt':'<event_reading_block_json>'+block}
 assert runtime.prepare_writer_output(request,{'event_draft':'Yesterday.'})['event_draft']=='Yesterday.'


def test_unknown_time_note_respects_append_budget(monkeypatch):
 from aml import runtime
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 request={'role':'event_writer','prompt':'<event_reading_block_json>'+json.dumps([{'message_time_origin':'unknown'}]),'writer_mode':'append','append_remaining_chars':10}
 assert runtime.prepare_writer_output(request,{'event_draft':'Yesterday.'})['event_draft']=='Yesterday.'


def test_time_rules_preserve_transcription_only(monkeypatch):
 from aml import runtime
 monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING','1')
 monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW','1')
 request={'role':'event_writer','rules':'Original rules','prompt':'Original prompt','transcription_only':True}
 assert runtime.writer_payload(request)==('Original rules','Original prompt')
