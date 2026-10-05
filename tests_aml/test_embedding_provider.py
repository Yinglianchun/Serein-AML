"""Native transport batches respect v4 limits without losing evidence inputs."""
import httpx
import pytest

from serein.adapters.embedding import EmbeddingClient
from serein.recall.index import unit_vector
from test_engine import memory


def embedding(model='text-embedding-v4'):
    client = EmbeddingClient.__new__(EmbeddingClient)
    client.profile = {'model':model, 'document_instruction':'', 'query_instruction':'', 'max_chars':12000}
    client.dimension = 2
    client.endpoint = 'https://embedding.invalid/embeddings'
    client.api_key = 'synthetic-key'
    client.api_key_env = None
    return client


@pytest.mark.parametrize('count', [0, 1, 10, 11, 23])
def test_v4_splits_requests_and_restores_all_input_positions(count):
    batches = []
    def handle(request):
        import json
        body = json.loads(request.content)
        batches.append(body['input'])
        assert len(body['input']) <= 10
        rows = [{'index':i, 'embedding':[int(text[1:])+1,1.]}
                for i,text in enumerate(body['input'])]
        return httpx.Response(200, json={'model':'text-embedding-v4', 'data':list(reversed(rows))})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        vectors = embedding().documents(['t'+str(i) for i in range(count)], client=http)
        assert not http.is_closed
    assert [text for batch in batches for text in batch] == ['t'+str(i) for i in range(count)]
    assert vectors == [unit_vector([i+1,1.],2) for i in range(count)]
    assert len(batches) == (count+9)//10


def test_other_embedding_keeps_its_existing_batch():
    seen = []
    def handle(request):
        import json
        body = json.loads(request.content)
        seen.append(len(body['input']))
        return httpx.Response(200, json={'model':body['model'], 'data':[
            {'index':i,'embedding':[1.,0.]} for i in range(len(body['input']))]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert len(embedding('synthetic-other').documents(['text']*23, client=http)) == 23
    assert seen == [23]


@pytest.mark.parametrize('invalid', ['positions', 'model', 'http'])
def test_bad_later_batch_fails_instead_of_returning_partial_vectors(invalid):
    calls = []
    def handle(request):
        import json
        body = json.loads(request.content)
        calls.append(body['input'])
        rows = [{'index':i,'embedding':[1.,0.]} for i in range(len(body['input']))]
        model = body['model']
        if len(calls) == 2:
            if invalid == 'http':return httpx.Response(400, text='sensitive-provider-body')
            if invalid == 'positions':rows[-1]['index'] = 0
            if invalid == 'model':model = 'different-model'
        return httpx.Response(200, json={'model':model,'data':rows})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http, pytest.raises(ValueError) as error:
        embedding().documents(['text']*23, client=http)
    assert len(calls) == 2
    assert 'sensitive-provider-body' not in str(error.value)


@pytest.mark.parametrize('count', [0, 1, 11, 23])
def test_v4_query_batches_keep_query_instruction_and_positions(count):
    import json
    client = embedding()
    client.profile.update(query_instruction='query-only', document_instruction='document-only')
    batches = []
    def handle(request):
        inputs = json.loads(request.content)['input']
        batches.append(inputs)
        assert all(text.startswith('Instruct: query-only\nQuery: t') for text in inputs)
        rows = [{'index':i, 'embedding':[int(text.rsplit('t',1)[1])+1,1.]}
                for i,text in enumerate(inputs)]
        return httpx.Response(200,json={'model':'text-embedding-v4','data':list(reversed(rows))})
    texts = ['t'+str(i) for i in range(count)]
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = client.queries(texts,client=http)
    assert [item['query'] for item in result] == texts
    assert [item['embedding'] for item in result] == [unit_vector([i+1,1.],2) for i in range(count)]
    assert [len(batch) for batch in batches] == [min(10,count-i) for i in range(0,count,10)]


@pytest.mark.parametrize('model', ['text-embedding-v4', 'synthetic-other'])
def test_route_preparation_preserves_example_boundary_order_and_disabled_routes(memory, tmp_path, monkeypatch, model):
    import json
    from dataclasses import replace
    from aml import engine, runtime
    from serein.semantic_setup import prepare
    settings = runtime.bootstrap(engine._paths('user-a'))
    target = tmp_path/'routes.json'
    settings = replace(settings,embedding={'endpoint':'https://embedding.invalid/embeddings'},
                       recall={**settings.recall,'routing_file':str(target)})
    profile = {'model':model,'provider_host':'embedding.invalid','dimension':2,
               'query_instruction':'query-only','document_instruction':'document-only','max_chars':12000}
    profile_path = tmp_path/'profile.json';profile_path.write_text(json.dumps(profile))
    examples = {'generation':7,'policy':{'min_score':0.7},'routes':[
        {'name':'ignored','enabled':False,'action':'bad','examples':['disabled']},
        {'name':'r1','action':'recall','threshold':0.6,'examples':['{user_name} t'+str(i) for i in range(12)]},
        {'name':'r2','action':'skip','examples':['{ai_name} final']}],
        'boundaries':[{'action':'skip','text':'boundary1'},{'action':'recall','text':'boundary2'}]}
    examples_path = tmp_path/'examples.json';examples_path.write_text(json.dumps(examples))
    batches, expected_vectors = [], []
    def request(self, inputs, count, **kwargs):
        batch = inputs if isinstance(inputs,list) else [inputs]
        batches.append(batch)
        vectors = [unit_vector([len(expected_vectors)+i+1,1.],2) for i in range(count)]
        expected_vectors.extend(vectors)
        return vectors
    monkeypatch.setattr(EmbeddingClient,'_request',request)
    result = prepare(settings,profile_path,examples_path)
    saved = json.loads(target.read_text())
    assert result['routes'] == 2
    assert saved['generation'] == 7 and saved['policy'] == examples['policy']
    assert [row['name'] for row in saved['routes']] == ['r1','r2']
    actual = [v for row in saved['routes'] for v in row['vectors']]+[row['vector'] for row in saved['boundaries']]
    assert actual == expected_vectors and len(actual) == 15
    assert [len(batch) for batch in batches] == ([10,5] if model=='text-embedding-v4' else [1]*15)
    assert [text for batch in batches for text in batch] == [
        'Instruct: query-only\nQuery: '+text for text in [*['User t'+str(i) for i in range(12)],'AI final','boundary1','boundary2']]
