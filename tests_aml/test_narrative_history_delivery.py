"""Existing volume authoring -> menu -> review -> final Search, synthetic only."""
import asyncio
import json
import pytest
from aml import engine, narratives
from serein.core.store import Store
from tests_aml.test_narrative_read_guards import published_volume


@pytest.fixture
def history(published_volume, monkeypatch):
    paths, settings = published_volume
    monkeypatch.setattr(engine, '_PLAN_ARCS', True)
    monkeypatch.setattr(engine, '_EXPAND_GAPS', False)
    for name in ('SEREIN_AML_SOURCE_EVIDENCE','SEREIN_AML_DELIVERY_FIXES','SEREIN_AML_TEMPORAL_NOTES'):
        monkeypatch.setenv(name,'0')
    with Store(paths.database) as store:
        doc=store.read('scene_factory')
        store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],
                     body_md='On 2024-08-01 the application was submitted. On 2024-09-01 missing documents were supplied. Approval followed on 2024-10-01 because the missing evidence was supplied.',metadata=doc['metadata'])
    assert asyncio.run(narratives.author(settings))['written']
    # Force only the anchor to be a direct hit: the volume must earn admission.
    with Store(paths.database) as store:
        doc=store.read('scene_anchor')
    monkeypatch.setattr(engine,'_recall_hits',lambda *a,**k:[{'id':doc['id'],'kind':'scene','document':doc}])
    return paths,settings


def decision(prompt):
    menus=json.loads(prompt.split('MENUS:\n',1)[1])
    return {'sufficient':False,'supports':[{'ref':'scene_anchor','quote':'Dana installed the AsterBridge meter.'}],
            'missing':['The dated application stages and why approval followed'],
            'selections':[{'arc_key':menus[0]['arc_key'],'picks':[0],'gap':0}]}


def search():
    return engine.search_memory(query='What were the dated stages and why was the application approved?',
                                options=None,user_id='synthetic-user',top_k=10)


def test_existing_authored_volume_delivers_sequence_and_cause(history,monkeypatch):
    prompts=[]
    def model(prompt):
        prompts.append(prompt)
        if 'READS:\n' not in prompt:
            assert 'missing documents were supplied' not in prompt
            return decision(prompt)
        reads=json.loads(prompt.split('READS:\n',1)[1])
        assert 'because the missing evidence was supplied' in reads[0]['material']['text']
        return {'accepted':[{'selection':reads[0]['selection'],
            'anchor_quote':'Dana installed the AsterBridge meter.',
            'quote':'Approval followed on 2024-10-01 because the missing evidence was supplied.'}]}
    monkeypatch.setattr(engine,'_model_json',model)
    result=search()
    volume=next(row for row in result if row['id']=='narrative_device')
    assert all(x in volume['content'] for x in ['2024-08-01','2024-09-01','2024-10-01','because'])
    assert len(prompts)==2


def test_long_volume_tail_reaches_review_and_final_delivery(history,monkeypatch):
    paths,settings=history
    with Store(paths.database) as store:
        doc=store.read('scene_factory')
        store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],
                     body_md='Background equipment detail. '*330 + 'Approval followed on 2024-10-01 because the missing evidence was supplied.',metadata=doc['metadata'])
    assert asyncio.run(narratives.author(settings))['written']
    observed=[]
    audit=[]
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT','1')
    monkeypatch.setattr(engine.delivery,'_emit',audit.append)
    def model(prompt):
        if 'READS:\n' not in prompt:return decision(prompt)
        reads=json.loads(prompt.split('READS:\n',1)[1])
        observed.extend(reads)
        assert not reads[0]['material']['truncated']
        assert 'Approval followed' in reads[0]['material']['text']
        return {'accepted':[{'selection':reads[0]['selection'],
                            'anchor_quote':'Dana installed the AsterBridge meter.',
                            'quote':'Approval followed on 2024-10-01 because the missing evidence was supplied.'}]}
    monkeypatch.setattr(engine,'_model_json',model)
    result=search()
    assert observed and any(row['id']=='narrative_device' and 'Approval followed' in row['content'] for row in result)
    assert not any(row.get('reason')=='ARC_READ_TRUNCATED' for row in audit)
    assert any(row['stage']=='arc_review' and row['count']==1 for row in audit)
    assert not any(row.get('reason')=='PARTIAL_EXCERPT' for row in audit)
    serialized=json.dumps(audit)
    assert 'Approval followed' not in serialized and 'narrative_device' not in serialized
    with Store(paths.database) as store:
        assert 'Approval followed' in store.read('narrative_device')['body_md']
