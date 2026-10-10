import json
from aml import arc_planning, delivery


def item(index, text):
    return {'selection':f's{index}', 'gap':'Need sequence', 'bridge_entity':None,
            'anchor':{'ref':'anchor','text':'Anchor fact.','truncated':False},
            'material':{'ref':f'm{index}','text':text,'truncated':False}}


def test_whole_materials_split_at_serialized_utf8_budget(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','0')
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES','24000')
    reads=[item(i,'前文。'*1400+f'Tail {i}.') for i in range(3)]
    calls=[]
    def model(prompt):
        assert len(prompt.encode('utf-8')) <= 24000
        packet=json.loads(prompt.split('READS:\n',1)[1])
        calls.append(packet)
        for row in packet:
            assert row['material']['text']==reads[int(row['selection'][1:])]['material']['text']
        return {'accepted':[{'selection':row['selection'],'anchor_quote':'Anchor fact.',
                             'quote':f"Tail {row['selection'][1:]}."} for row in packet]}
    result=arc_planning.review(model,'Sequence?',None,[],reads)
    assert set(result)=={'s0','s1','s2'} and len(calls)==3


def test_oversized_single_material_is_observable_and_other_material_survives(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','0')
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES','4000')
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT','1')
    events=[]
    monkeypatch.setattr(delivery,'_emit',events.append)
    calls=[]
    def model(prompt):
        assert len(prompt.encode())<=4000
        calls.append(prompt)
        return {'accepted':[{'selection':'s1','anchor_quote':'Anchor fact.','quote':'Short fact.'}]}
    with delivery.request():
        result=arc_planning.review(model,'Sequence?',None,[],[item(0,'长'*5000),item(1,'Short fact.')])
    assert set(result)=={'s1'} and len(calls)==1
    assert any(e.get('reason')=='INPUT_BUDGET' for e in events)
