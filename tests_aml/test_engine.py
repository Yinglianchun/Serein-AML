import json

import pytest
import httpx

import aml.engine as engine
from aml import runtime, originals
from serein.core.store import Store
from serein.deployment import read_settings, save_settings
from serein.extensions import pipeline
from serein.recall.index import refresh_index


def role_output(role, request):
    if role == 'track_router':
        return {'message_assignments': [{'source_message_id': r['id'], 'primary_track_ref': 'new:1',
                'context_track_refs': [], 'routing_role': 'primary_activity'} for r in request['messages']],
                'track_updates': [{'track_ref': 'new:1', 'subject': 'Move', 'throughline': 'A move to Paris',
                    'event_policy': 'default', 'status': 'active'}]}
    if role == 'event_curator':
        component = request['component']
        return {'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': component['track_ids'][0],
                    'owned_unit_roots': [u['unit_root_message_id'] for u in component['memberships']]}],
                'skip_unit_roots': [], 'defer_unit_roots': [],
                'decision_review': {'events': [{'event_index': 0, 'reason': 'Alice moved'}],
                    'boundaries': [], 'dispositions': []}}
    from serein.extensions.pipeline_latest import _SELF_REVIEW_KEYS
    source = request['messages'][0]
    sentence = source['content']
    spans = [{'source_message_id': source['id'], 'quote': sentence}]
    return {'title': 'Alice moved', 'event_draft': sentence, 'recallable': True, 'evidence_sufficient': True,
            'kept_details': ['Alice moved'], 'discarded_details': [],
            'claim_groups': [{'claim_group_id': 'g1', 'claim_type': 'fact', 'owner': '外部', 'render_mode': 'direct',
                'focus_role': 'core', 'summary': sentence, 'source_spans': spans}],
            'sentence_evidence': [{'sentence_index': 0, 'sentence': sentence, 'claim_group_ids': ['g1'], 'source_spans': spans}],
            'self_review': {key: True for key in _SELF_REVIEW_KEYS}}


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, '_DATA_ROOT', tmp_path/'data')
    monkeypatch.setenv('SEREIN_AML_PROFILE', 'development')
    monkeypatch.setenv('SEREIN_AML_ORGANIZE_ARCS', '0')
    config = tmp_path/'models.json'
    config.write_text(json.dumps({'models': [{'id': 'test', 'model': 'synthetic-other',
        'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'synthetic-key', 'protocol': 'openai'}],
        'upstreams': [], 'assignments': {role: 'test' for role in runtime.GENERATIVE_ROLES}}))
    monkeypatch.setenv('SEREIN_AML_MODEL_CONFIG', str(config))
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: {'queries': ['Alice', 'Paris'], 'entities': ['Alice']})
    advance = pipeline.advance
    calls = []
    async def runner(role, request):
        calls.append(role)
        return role_output(role, request)
    async def advance_with_fixture(database, **kwargs):
        kwargs.pop('runner', None)
        return await advance(database, **kwargs, runner=runner)
    advance_with_fixture.original = advance
    monkeypatch.setattr(pipeline, 'advance', advance_with_fixture)
    return calls


def add(messages=None, **changes):
    return engine.add_memory(**{'request_id': 'req-1', 'user_id': 'user-a', 'session_id': 'session-1',
        'messages': messages or [{'role': 'user', 'timestamp': 1704067200000, 'content': 'Alice moved to Paris.'},
                                {'role': 'assistant', 'timestamp': 1704067260000, 'content': 'You moved to Paris.'}], **changes})


def search(**changes):
    return engine.search_memory(**{'query': 'Where did Alice move?', 'options': None, 'user_id': 'user-a', 'top_k': 100, **changes})


def select_competition_embedding():
    filename = runtime.os.environ['SEREIN_AML_MODEL_CONFIG']
    with open(filename, encoding='utf-8') as stream:
        source = json.load(stream)
    source['models'].append({'id':'embedding', 'model':'text-embedding-v4',
                            'base_url':'https://embedding.invalid/v1', 'protocol':'openai'})
    source['assignments']['embedding'] = 'embedding'
    with open(filename, 'w', encoding='utf-8') as stream:
        json.dump(source, stream)


def test_add_then_search_uses_real_public_pipeline_and_exact_evidence(memory):
    add()
    assert memory == ['track_router', 'event_curator', 'event_writer']
    data = search()
    assert len(data) == 1 and 'Alice moved to Paris.' in data[0]['content']
    paths = engine._paths('user-a')
    with Store(paths.database) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
    from serein.core.reader import Reader
    with Reader(paths.database) as reader:
        result = reader.read(data[0]['id'], with_evidence=True)
        assert len(result['evidence']) == 2


def test_user_id_is_a_hard_retrieval_boundary(memory):
    add()
    assert search(user_id='user-b') == []


def test_incomplete_tail_is_searchable_without_fake_assistant_or_event(memory):
    add([{'role': 'user', 'content': 'Alice moved to Paris.'}])
    assert memory == []
    data = search()
    assert data[0]['id'] == 'raw:1'
    with Store(engine._paths('user-a').database) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 1


def test_retry_is_idempotent_and_conflicting_content_is_rejected(memory):
    first = add()
    assert add() == first
    assert len(memory) == 3
    with pytest.raises(engine.AddConflict):
        add([{'role': 'user', 'content': 'Alice moved to Berlin.'}])
    with Store(engine._paths('user-a').database) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2


def test_failed_pipeline_retry_retains_originals_without_duplicates(memory, monkeypatch):
    original = runtime.ingest_pipeline
    async def fail(_):
        raise RuntimeError('synthetic temporary failure')
    monkeypatch.setattr(runtime, 'ingest_pipeline', fail)
    with pytest.raises(RuntimeError):
        add()
    monkeypatch.setattr(runtime, 'ingest_pipeline', original)
    add()
    with Store(engine._paths('user-a').database) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute("SELECT status FROM aml_add_receipts").fetchone()[0] == 'complete'


def test_invalid_provider_vector_keeps_add_retryable_after_public_settlement(memory, monkeypatch):
    fill = originals.fill
    monkeypatch.setattr(originals, 'fill', lambda *_: (_ for _ in ()).throw(ValueError('synthetic invalid vector')))
    with pytest.raises(RuntimeError, match='indexing'):
        add()
    with Store(engine._paths('user-a').database) as store:
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'pending'
    monkeypatch.setattr(originals, 'fill', fill)
    add()
    assert memory == ['track_router','event_curator','event_writer']
    with Store(engine._paths('user-a').database) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'complete'


def test_development_cannot_be_reused_as_competition(memory, monkeypatch):
    add()
    monkeypatch.setenv('SEREIN_AML_PROFILE', 'competition')
    monkeypatch.setenv('OR_key', 'synthetic-key')
    select_competition_embedding()
    assert runtime.configuration()[1]['profile'] == 'competition'
    with pytest.raises(runtime.ProfileConflict):
        add()
    with pytest.raises(runtime.ProfileConflict):
        search()


def test_paused_public_batch_keeps_add_pending_instead_of_false_success(memory):
    add([{'role': 'user', 'content': 'Alice moved to Paris.'}])
    paths = engine._paths('user-a')
    with Store(paths.database) as store:
        frozen = json.dumps({'contract':pipeline.CONTRACT, 'runtime_revision':pipeline.runtime_revision()})
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('blocked','test',?,'paused_failure')", (frozen,))
    with pytest.raises(RuntimeError, match='paused batch'):
        add([{'role': 'user', 'content': 'Another incomplete message.'}], request_id='req-2')
    with Store(paths.database) as store:
        assert store.conn.execute("SELECT status FROM aml_add_receipts WHERE id=?", ('aml:'+engine.hashlib.sha256(b'req-2').hexdigest(),)).fetchone()[0] == 'pending'


def test_original_vector_retry_fills_missing_chunks_and_draft_visibility(memory, monkeypatch):
    add([{'role': 'user', 'content': 'Alice Paris weather station. ' * 3000}])
    paths = engine._paths('user-a')
    calls = []
    class Embeddings:
        dimension = 2
        def __init__(self, *args, **kwargs):pass
        def documents(self, texts):
            calls.append(len(texts))
            if len(calls) == 2:
                raise ValueError('synthetic interrupted second batch')
            return [[1.,0.] for _ in texts]
        def query(self, text):return {'embedding':[1.,0.]}
    monkeypatch.setattr(originals, 'EmbeddingClient', Embeddings)
    settings = engine.Settings(paths.database, paths.index, embedding={'endpoint':'https://mock.invalid/embeddings'})
    with pytest.raises(ValueError):originals.fill(settings)
    assert originals.fill(settings)['embedded'] > 0
    assert originals.fill(settings)['embedded'] == 0
    assert originals.hits(settings, 'coastal instrument', [], limit=1)
    with Store(paths.database) as store:
        store.conn.execute("UPDATE raw_events SET metadata_json=json_set(metadata_json,'$.draft',1)")
    assert originals.hits(settings, 'coastal instrument', [], limit=1) == []


def test_stage_runner_tolerates_format_but_retries_missing_ownership(memory, monkeypatch):
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    from serein import model_runtime
    calls = []
    async def complete(model, payload):
        paths = engine._paths('user-a')
        with Store(paths.database, read_only=True) as store:
            request = json.loads(store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        role = request['role']
        calls.append(role)
        output = role_output(role, request)
        if role == 'event_curator':
            assert 'Do NOT add an' in payload['messages'][1]['content']
            output['decision_review']['events'][0]['evidence'] = []
            output['decision_review']['events'][0]['event_index'] = '0'
            if calls.count(role) == 1:
                output['events'][0]['owned_unit_roots'].pop()
        assert model['model'] == 'synthetic-other'
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr(model_runtime, 'complete', complete)
    add()
    assert calls == ['track_router', 'event_curator', 'event_curator', 'event_writer']
    with Store(engine._paths('user-a').database, read_only=True) as store:
        rejected = store.conn.execute("SELECT job_id,error FROM pipeline_attempts WHERE error!=''").fetchone()
        assert rejected and rejected['job_id'].endswith('event_curator:0')
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2


def test_format_compatibility_preserves_evidence_and_does_not_invent_ids():
    result = {'events':[{'action':'merge','base_event_ids':[], 'owned_unit_roots':['1']}],
        'decision_review':{'events':[{'event_index':'0','reason':'Activity','evidence':[{'quote':'extra audit'}]}],
            'boundaries':[{'left_event_index':'0','right_event_index':'1','evidence':[{'source_message_id':'2','quote':'EXACT quote'}]}]}}
    parsed = runtime.stage_json('```json\n'+json.dumps({'result':result})+'\n```', 'event_curator')
    assert parsed['events'] == [{'action':'merge','base_event_ids':[], 'owned_unit_roots':[1]}]
    assert parsed['decision_review']['events'] == [{'event_index':0,'reason':'Activity'}]
    assert parsed['decision_review']['boundaries'][0]['evidence'] == [{'source_message_id':2,'quote':'EXACT quote'}]


def test_competition_overrides_every_generative_role(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_PROFILE', 'competition')
    monkeypatch.setenv('OR_key', 'synthetic-key')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    select_competition_embedding()
    config, marker = runtime.configuration()
    assert marker['profile'] == 'competition'
    assert {config['assignments'][role] for role in runtime.GENERATIVE_ROLES} == {'aml-mini'}
    assert config['assignments']['embedding'] == 'embedding'
    assert next(m for m in config['models'] if m['id'] == 'aml-mini')['model'] == 'openai/gpt-4o-mini'
    assert 'synthetic-key' not in str(marker)


def test_active_development_search_cannot_fall_back_to_mini(memory, monkeypatch):
    add()
    paths = engine._paths('user-a')
    save_settings(paths.database, {'assignments': {'writer': ''}})
    monkeypatch.setattr(engine, '_client', lambda: pytest.fail('Must never fall back across profiles'))
    with runtime.active(paths.database), pytest.raises(RuntimeError, match='not configured'):
        engine._model_json('Return the retrieval plan')


@pytest.mark.parametrize('role', ['writer','narrative_scout','embedding'])
def test_changed_database_model_assignment_is_rejected_before_search(memory, monkeypatch, role):
    add()
    paths = engine._paths('user-a')
    save_settings(paths.database, {'models':read_settings(paths.database)['models'] + [
        {'id':'other', 'model':'other-test-model', 'protocol':'openai','api_key':'test',
         'base_url':'http://127.0.0.1:9/v1'}], 'assignments':{role:'other'}})
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: pytest.fail('No model call after profile drift'))
    with pytest.raises(runtime.ProfileConflict, match='assignments'):
        search()


def test_search_json_format_tolerance_keeps_menu_ids_exact():
    parsed = engine._json_from_model('```json\n'+json.dumps({'result':{'selections':[
        {'arc_key':'exact-arc','picks':['1','999']}], 'unused':'note'}})+'\n```')
    assert parsed['selections'] == [{'arc_key':'exact-arc','picks':[1,999]}]


def test_settled_excluded_event_cannot_leak_through_original_fallback(memory):
    add()
    paths = engine._paths('user-a')
    with Store(paths.database) as store:
        doc = store.read(search()[0]['id'])
        store.revise(doc['id'], expected_revision=doc['revision'], title=doc['title'], body_md=doc['body_md'],
                     metadata={**doc['metadata'], 'domain': 'work'})
    refresh_index(paths.database, paths.index, [doc['id']])
    save_settings(paths.database, {'recall': {'domains': {'work': 'excluded'}}})
    assert originals.pending(paths.database) == []
    assert search() == []


def test_real_public_semantic_lookup_and_reranker_find_paraphrase(memory, monkeypatch):
    filename = runtime.os.environ['SEREIN_AML_MODEL_CONFIG']
    with open(filename) as stream: config = json.load(stream)
    config['models'].extend([
        {'id': 'embed', 'model': 'synthetic-embedding', 'base_url': 'https://mock.invalid/v1', 'protocol': 'openai', 'api_key': 'test'},
        {'id': 'rerank', 'model': 'synthetic-reranker', 'base_url': 'https://mock.invalid/v1', 'protocol': 'openai', 'api_key': 'test'}])
    config['assignments'].update({'embedding':'embed', 'reranker':'rerank'})
    with open(filename, 'w') as stream: json.dump(config, stream)
    requests = []
    def handle(request):
        data = json.loads(request.content)
        requests.append((request.url.path, data))
        if request.url.path.endswith('/rerank'):
            return httpx.Response(200, json={'results': [{'index': i, 'relevance_score': .9} for i in range(len(data['documents']))]})
        values = data['input'] if isinstance(data['input'], list) else [data['input']]
        return httpx.Response(200, json={'model': data['model'], 'data': [{'index': i, 'embedding': [1.,0.,0.,0.]} for i in range(len(values))]})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    add()
    paths = engine._paths('user-a')
    runtime.prepare_memory(engine.Settings(paths.database, paths.index))
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: {'queries': ['residence'], 'entities': []})
    query = 'Where is her new home?'
    result = search(query=query)
    assert len(result) == 1 and 'Paris' in result[0]['content']
    assert any(data.get('query') == query for path, data in requests if path.endswith('/rerank'))
    assert any(query in str(data.get('input')) for path, data in requests if path.endswith('/embeddings'))
