import json

import pytest

from aml import arc_planning


def test_numbered_gap_anchor_resolves_to_original_without_copying(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    evidence=[{'ref':'a','text':'Earlier work ended. 林雨加入青禾研究所。','title':'Career'}]
    def model(prompt):
        packet=json.loads(prompt.split('CURRENT_EVIDENCE:\n',1)[1].split('\nMENUS:',1)[0])
        assert 'text' not in packet[0]
        assert packet[0]['units'][1]=={'id':'1','text':'林雨加入青禾研究所。'}
        assert '"anchor_quote"' not in prompt
        return {'sufficient':False,'supports':[{'ref':'a','unit':1}],
            'missing':['研究所的所在地'],'selections':[],
            'searches':[{'anchor':'a','anchor_unit':'1','entity':'青禾研究所','query':'青禾研究所 所在地','gap':0}]}
    result=arc_planning.assess(model,'在哪里工作',None,evidence,{},allow_search=True)
    assert result['searches'][0]['anchor_quote']=='林雨加入青禾研究所。'


@pytest.mark.parametrize('key,expected',[('0',True),(0,True),('99',False),(True,False),([],False)])
def test_numbered_joint_review_uses_local_ids_and_rejects_invented_units(monkeypatch,key,expected):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    reads=[{'selection':'s0','anchor':{'ref':'a','text':'Lin joined Oriole.'},
            'material':{'ref':'b','text':'Oriole operates in Valencia.'},'gap':'Location'}]
    def model(prompt):
        packet=json.loads(prompt.split('READS:\n',1)[1])
        assert packet[0]['anchor']['units'][0]['text']=='Lin joined Oriole.'
        assert '"anchor_quote"' not in prompt
        return {'accepted':[{'selection':'s0','anchor_unit':key,'material_unit':'0',
                             'anchor_quote':'Lin joined Oriole.'}]}
    result=arc_planning.review(model,'Where?',None,[],reads)
    assert bool(result)==expected
    if expected:
        assert result['s0']=={'anchor_quote':'Lin joined Oriole.','quote':'Oriole operates in Valencia.'}


def test_long_units_omitted_without_fabricated_quote(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    record=arc_planning.numbered({'text':'x'*801+'\nUsable fact.'})
    assert record['units_partial'] and record['units']==[{'id':'1','text':'Usable fact.'}]
