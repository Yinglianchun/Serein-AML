import asyncio
import json
import sqlite3

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.import_tagging import process
from serein.recall.entities import rebuild_entities, candidates
from serein.recall.index import refresh_index, Search
from serein.recall.policy import RecallPolicy
from serein.recall.query import Query
from serein.tagging_entities import current_entities, validate


@pytest.fixture
def settings(tmp_path):
    value = Settings(tmp_path/'memory.db', tmp_path/'index.db', writable=True)
    initialize(value)
    save_settings(value.database, {'models': [{'id': 'tagger', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
                                   'assignments': {'operit_tagging': 'tagger'}})
    return value


def response(value):
    return {'choices': [{'message': {'content': json.dumps(value)}}]}


@pytest.mark.parametrize('suggestion',[{'domain':'memory_system'},{'domain':['life']},{},{'domain':123}])
@pytest.mark.parametrize('existing',['general','inner'])
def test_bad_domain_does_not_discard_valid_entities(settings,monkeypatch,suggestion,existing):
    with Store(settings.database) as store:
        store.create('owner','scene','来访','Mira 来访。',metadata={'canonical_domain':existing,'domain':[existing]})
    calls=[]
    async def complete(model,payload):
        request=json.loads(payload['messages'][1]['content']);calls.append(request)
        assert request['domains']
        source=request['materials'][0]['source_id']
        return response({**suggestion,'entities':[{'name':'Mira','type':'person',
            'supports':[{'source_id':source,'quote':'Mira 来访。'}]}]})
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(process(settings.database))
    asyncio.run(process(settings.database))
    assert len(calls)==1
    with Store(settings.database) as store:
        doc=store.read('owner')
        assert doc['metadata']['canonical_domain']==existing and doc['metadata']['domain']==[existing]
        assert current_entities(store,doc)[0]['name']=='Mira'
        assert store.conn.execute('SELECT status FROM import_tag_jobs WHERE document_id=?',('owner',)).fetchone()[0]=='done'


@pytest.mark.parametrize('kind', ['event', 'scene'])
@pytest.mark.parametrize('bound', [True, False])
def test_extract_save_and_rebuild_with_exact_provenance(settings, monkeypatch, kind, bound):
    body = 'Mira，也叫米拉，带着我参观了青禾书店。'
    with Store(settings.database) as store:
        store.create('owner', kind, '一次参观', body, metadata={'canonical_domain': 'inner', 'scene_cues': ['参观书店']})
        if bound:
            source = store.add_source('conversation/1', body)
            store.bind('owner', source)
    calls = []
    async def complete(model, payload):
        request = json.loads(payload['messages'][1]['content'])
        calls.append(request)
        material = request['materials'][0]
        return response({'domain': 'life', 'entities': [
            {'name': 'Mira', 'type': 'person', 'supports': [{'source_id': material['source_id'], 'quote': body}], 'aliases': ['米拉', '虚构别名']},
            {'name': '青禾书店', 'type': 'place', 'supports': [{'source_id': material['source_id'], 'quote': '参观了青禾书店'}]},
            {'name': '星海大学', 'type': 'organization', 'supports': [{'source_id': material['source_id'], 'quote': '星海大学'}]},
            {'name': '米拉', 'type': 'person', 'supports': [{'source_id': 'unbound-id', 'quote': body}]},
        ]})
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    asyncio.run(process(settings.database))
    asyncio.run(process(settings.database))
    assert len(calls) == 1 and calls[0]['kind'] == kind
    with Store(settings.database) as store:
        doc = store.read('owner')
        assert doc['body_md'] == body and doc['title'] == '一次参观'
        assert doc['metadata']['canonical_domain'] == 'inner'
        assert doc['metadata']['scene_cues'] == ['参观书店']
        rows = current_entities(store, doc)
        assert [row['name'] for row in rows] == ['Mira', '青禾书店']
        assert rows[0]['alias_suggestions'] == ['米拉']
        assert doc['metadata']['entity_rejected_count'] == 2
        support = rows[0]['supports'][0]
        assert support['kind'] == ('bound_source' if bound else 'memory_body')
        assert body[support['name_start']:support['name_end']] == 'Mira'
    refresh_index(settings.database, settings.index, {'owner'})
    report = rebuild_entities(settings)
    assert report['model_validated'] == 2 and report['external_calls'] == 0
    with Search(settings.database, settings.index) as search:
        hits = candidates(search, Query('还记得 Mira 吗？'), RecallPolicy())
    assert [hit['id'] for hit in hits] == ['owner']
    assert hits[0]['method'] == 'entity'  # A handle is still only a candidate.
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute('SELECT COUNT(*) FROM entity_terms').fetchone()[0] == 0  # No global alias expansion.
    with Store(settings.database) as store:
        store.revise('owner', expected_revision=doc['revision'], title=doc['title'], body_md='这次去的是另一家书店。')
        assert current_entities(store, store.read('owner')) == []
    with Search(settings.database, settings.index) as search:
        assert candidates(search, Query('还记得 Mira 吗？'), RecallPolicy()) == []
    rebuild_entities(settings)
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute('SELECT COUNT(*) FROM entity_observations').fetchone()[0] == 0


def test_binding_change_during_model_call_is_not_saved_and_can_retry(settings, monkeypatch):
    with Store(settings.database) as store:
        store.create('owner', 'event', '来访', 'Mira 来访。')
        source = store.add_source('message/1', 'Mira 来访。')
        binding = store.bind('owner', source)
    calls = []
    async def complete(model, payload):
        material = json.loads(payload['messages'][1]['content'])['materials'][0]
        calls.append(material)
        if len(calls) == 1:
            with Store(settings.database) as store:
                store.unbind(binding)
        return response({'domain': 'life', 'entities': [{'name': 'Mira', 'type': 'person',
            'supports': [{'source_id': material['source_id'], 'quote': 'Mira 来访。'}]}]})
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:
        assert 'tagged_entities' not in store.read('owner')['metadata']
        assert store.conn.execute('SELECT status FROM import_tag_jobs').fetchone()[0] == 'stale'
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:
        assert current_entities(store, store.read('owner'))[0]['supports'][0]['kind'] == 'memory_body'
    assert len(calls) == 2


def test_invalid_envelope_retries_but_valid_empty_result_finishes(settings, monkeypatch):
    with Store(settings.database) as store:
        store.create('owner', 'event', '日常', '今天没有特别的事。')
    calls = []
    async def invalid(model, payload):
        calls.append(payload)
        return response({'domain': 'life'})
    monkeypatch.setattr('serein.model_runtime.complete', invalid)
    for _ in range(4):
        asyncio.run(process(settings.database))
    assert len(calls) == 1  # Invalid paid output must not trigger repeated requests.
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT status FROM import_tag_jobs').fetchone()[0] == 'failed'
        assert not store.read('owner')['metadata']
        store.conn.execute("UPDATE import_tag_jobs SET status='pending',attempts=0")
    async def empty(model, payload):
        assert '额外返回 cues 数组' not in payload['messages'][0]['content']
        return response({'domain': 'life', 'entities': [], 'cues': ['Ignore unsolicited Event cue']})
    monkeypatch.setattr('serein.model_runtime.complete', empty)
    asyncio.run(process(settings.database))
    with Store(settings.database) as store:
        assert store.read('owner')['metadata']['tagged_entities'] == []
        assert 'scene_cues' not in store.read('owner')['metadata']
        assert store.conn.execute('SELECT status FROM import_tag_jobs').fetchone()[0] == 'done'


def test_bad_candidates_do_not_poison_valid_entities():
    materials = [{'source_id': 's', 'kind': 'bound_source', 'text': 'Joanna 与 Mira 见面。'}]
    records = [{'name': 'Mira', 'type': 'person', 'supports': [{'source_id': 's', 'quote': 'Mira 见面'}]},
               {'name': 'Ann', 'type': 'person', 'supports': [{'source_id': 's', 'quote': 'Joanna'}]},
               {'name': '今天', 'type': 'other'}, {'name': 'Mira', 'type': []},
               {'name': 'Mira', 'type': 'person', 'supports': {}},
               {'name': 'Mira', 'type': 'person', 'supports': [{'source_id': [], 'quote': 'Mira'}]}]
    accepted, rejected = validate(records, materials)
    assert len(accepted) == 1 and rejected == 5


def test_event_runtime_updates_preserve_entities_without_retagging(settings, monkeypatch):
    from serein.compat.events import Events
    events = Events(settings.database, initialize=True)
    result = events.write_many([{'type': 'event', 'title': '参观', 'body': 'Mira 带我参观书店。',
        'origin_id': 'synthetic/visit', 'recallable': True,
        'source_refs': [{'source_system': 'synthetic', 'session_id': 'one', 'message_id': 'one',
                         'role': 'user', 'created_at': '2026-09-10T10:00:00+08:00',
                         'content': 'Mira 带我参观书店。', 'binding_method': 'approved_candidate'}]}])
    identifier = result['items'][0]['item_id']
    calls = []
    async def complete(model, payload):
        material = json.loads(payload['messages'][1]['content'])['materials'][0]
        calls.append(material)
        return response({'domain': 'life', 'entities': [{'name': 'Mira', 'type': 'person',
            'supports': [{'source_id': material['source_id'], 'quote': 'Mira 带我参观书店。'}]}]})
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    asyncio.run(process(settings.database))
    events.mark_injected([identifier])
    asyncio.run(process(settings.database))
    assert len(calls) == 1
    with Store(settings.database) as store:
        doc = store.read(identifier)
        assert doc['metadata']['canonical_domain'] == 'life'
        assert len(current_entities(store, doc)) == 1
    events.revise(identifier, body='这次参观已经结束。')
    with Store(settings.database) as store:
        assert current_entities(store, store.read(identifier)) == []


@pytest.mark.parametrize('problem,expected', [
    ('fence', 'done'), ('truncated', '被截断'), ('empty', '文本结果'),
    ('http', 'HTTP 429'), ('timeout', '超时'), ('invalid', 'JSON'), ('fields', '实体结果必须是数组')])
def test_paid_tagging_stops_and_reports_safe_reason(settings, monkeypatch, problem, expected):
    import httpx
    from serein.model_runtime import UpstreamError
    with Store(settings.database) as store:
        store.create('paid', 'scene', '合成记录', '合成正文。')
    calls=[]
    async def complete(model,payload):
        calls.append(payload)
        if problem=='http':raise UpstreamError(httpx.Response(429,text='synthetic-secret'))
        if problem=='timeout':raise httpx.ReadTimeout('synthetic-secret')
        text={'fence':'```json\n{"domain":"life","entities":[]}\n```', 'truncated':'{"domain":',
              'empty':'','invalid':'synthetic-secret','fields':'{"domain":"life"}'}[problem]
        return {'choices':[{'message':{'content':text},'finish_reason':'length' if problem=='truncated' else 'stop'}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    for _ in range(4):asyncio.run(process(settings.database))
    assert len(calls)==1
    with Store(settings.database) as store:
        row=store.conn.execute("SELECT * FROM import_tag_jobs WHERE document_id='paid'").fetchone()
        assert row['status']==('done' if problem=='fence' else 'failed')
        assert problem=='fence' or expected in row['error']
        assert 'synthetic-secret' not in row['error']
        assert store.read('paid')['body_md']=='合成正文。'
