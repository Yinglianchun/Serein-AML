"""Native transport batches respect v4 limits without losing evidence inputs."""
import httpx
import pytest

from serein.adapters.embedding import EmbeddingClient
from serein.recall.index import unit_vector


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
