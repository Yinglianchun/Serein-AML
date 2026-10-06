"""Formatting failures avoid paid rewrites; source and settlement guards remain."""
import asyncio
import json

import pytest

from aml import engine, narratives, runtime
from serein import model_runtime
from serein.core.store import Store
from serein.extensions import pipeline
from tests_aml.test_engine import add, memory, role_output
from tests_aml.test_narrative_authoring import author_memory, current


def test_minimal_event_writer_finishes_once_through_public_settlement(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []

    async def complete(model, payload):
        paths = engine._paths('user-a')
        with Store(paths.database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        role = request['role']
        calls.append(role)
        output = role_output(role, request)
        if role == 'event_writer':
            marker = '<event_reading_block_json>'
            assert payload['messages'][1]['content'].split(marker, 1)[1] == request['prompt'].split(marker, 1)[1]
            assert len(payload['messages'][0]['content']) < len(request['rules'])
            output = {key: output[key] for key in ('evidence_sufficient', 'title', 'event_draft')}
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr(model_runtime, 'complete', complete)
    add()
    assert calls == ['track_router', 'event_curator', 'event_writer']
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
        written = json.loads(store.conn.execute("SELECT output_json FROM pipeline_jobs WHERE json_extract(request_json,'$.role')='event_writer'").fetchone()[0])
        assert written['self_review'] == {}  # Host supplies no invented assessment.
    assert add() and calls == ['track_router', 'event_curator', 'event_writer']


def test_lite_writer_keeps_append_budget_and_context_requests(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request = {'role': 'event_writer', 'identity': {'ai_name': 'Observer', 'user_name': 'User'},
               'rules': 'Long style rules', 'prompt': 'Date\nHint\n<event_reading_block_json>FROZEN\nRemaining: 17',
               'writer_mode': 'append', 'append_remaining_chars': 17}
    assert runtime.writer_payload(request)[1].endswith('FROZEN\nRemaining: 17')
    output = runtime.prepare_writer_output(request, {
        'evidence_sufficient': True, 'title': 'Facts', 'event_draft': 'x' * 18})
    with pytest.raises(ValueError, match='17'):
        pipeline.validate(request, output)
    context = {'context_request': {'track_id': 'actual-track', 'before_message_id': 1, 'reason': 'missing_subject'}}
    assert runtime.prepare_writer_output(request, context) == context
    request['transcription_only'] = True
    assert runtime.writer_payload(request) == (request['rules'], request['prompt'])


def test_lite_discards_invalid_optional_event_citations_without_certifying_them(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    from serein.extensions.pipeline_latest import validate_event_writer_result
    request = {'role': 'event_writer', 'messages': [{'id': 1, 'content': 'Alice moved to Paris.'}]}
    output = role_output('event_writer', request)
    output['claim_groups'][0]['source_spans'][0]['quote'] = 'Invented quotation'
    original = json.dumps(output)
    output = runtime.prepare_writer_output(request, output)
    assert 'claim_groups' not in output and 'sentence_evidence' not in output
    assert validate_event_writer_result(output, request['messages']) == []
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING')
    strict = runtime.prepare_writer_output(request, json.loads(original))
    assert validate_event_writer_result(strict, request['messages'])


@pytest.mark.parametrize('shape', ['body', 'paragraphs'])
def test_lite_narrative_publishes_without_citation_retry_or_false_receipt(author_memory, monkeypatch, shape):
    settings, _, _ = author_memory
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    calls = []

    async def complete(model, payload, **kwargs):
        data = json.loads(payload['messages'][1]['content'])
        calls.append(data)
        source = next(row for row in data['sources'] if 'CopperBay' in row['text'])
        assert 'No citations' in payload['messages'][0]['content']
        prose = '## Manufacturing\n' + source['text']
        result = {'body': prose} if shape == 'body' else {'paragraphs': [
            {'text': prose, 'evidence': [{'ref': 'invented', 'quote': 'An unverified model quotation'}]}]}
        return {'choices': [{'message': {'content': json.dumps(result)}}]}

    monkeypatch.setattr(model_runtime, 'complete', complete)
    result = asyncio.run(narratives.author(settings))
    assert len(result['written']) == len(calls) == 1
    row = current(settings)
    assert row['body'].startswith('Manufacturing\n') and 'CopperBay' in row['body']
    assert row['publication_status'] == 'reviewed'
    assert row['linked_scene_ids'] == ['scene_purchase', 'scene_factory']
    with Store(settings.database, read_only=True) as store:
        audit = json.loads(store.conn.execute('SELECT evidence_json FROM aml_narrative_receipts').fetchone()[0])
        assert all(item['evidence'] == [] and item['citation_validation'] == 'not_performed' for item in audit)
    assert asyncio.run(narratives.author(settings))['unchanged'] == ['narrative_device']
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['unknown_ref', 'wrong_source', 'invented_quote', 'no_evidence'])
def test_lite_narrative_ignores_unverified_citations(change):
    sources = [{'ref': 'a', 'material': 'event:a', 'text': 'Alice joined Lumen.'},
               {'ref': 'b', 'material': 'event:b', 'text': 'Lumen is in Stonebridge.'}]
    paragraph = {'text': 'Alice joined Lumen.', 'evidence': [{'ref': 'a', 'quote': sources[0]['text']}]}
    if change == 'unknown_ref': paragraph['evidence'][0]['ref'] = 'invented'
    if change == 'wrong_source': paragraph['evidence'][0]['ref'] = 'b'
    if change == 'invented_quote': paragraph['evidence'][0]['quote'] = 'Alice moved to Stonebridge.'
    if change == 'no_evidence': paragraph['evidence'] = []
    body, audit = narratives.validate({'paragraphs': [paragraph]}, sources, simplified=True)
    assert body == paragraph['text'] and audit[0]['evidence'] == []
    assert audit[0]['citation_validation'] == 'not_performed'


@pytest.mark.parametrize('output', [{}, {'body': ''}, {'body': 5}, {'paragraphs': []},
                                   {'paragraphs': [{'text': ''}]}, {'body': 'x' * 100001}])
def test_lite_still_rejects_unusable_prose(output):
    with pytest.raises(ValueError):
        narratives.validate(output, [], simplified=True)


def test_authoring_policy_cannot_change_mid_database(memory, monkeypatch):
    paths = engine._paths('user-a')
    runtime.bootstrap(paths)
    marker = runtime.configuration()[1]
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    assert runtime.configuration()[1] != marker
    with pytest.raises(runtime.ProfileConflict):
        runtime.bootstrap(paths)
