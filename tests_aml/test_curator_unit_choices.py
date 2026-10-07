import copy
import pytest
from aml import runtime
from serein.extensions import pipeline_latest as curator
from serein.extensions.pipeline_policy import editorial_review_scope
from tests_aml.test_curator_track_scope import bridge_request, compact_event

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
