"""Relax only card verbosity; retain routing and durable pipeline contracts."""
import copy
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from serein.extensions.pipeline_policy import editorial_review_scope
from test_engine import memory, role_output, add


def normalize(output):
    return pipeline.normalize_event_track_message_output(
        output, [{'id': 1}], [], session_id='length', next_track_ordinal=1)


def packet(length):
    return {'message_assignments': [{'source_message_id': 1, 'primary_track_ref': 'new:1',
            'context_track_refs': [], 'routing_role': 'primary_activity'}],
            'track_updates': [{'track_ref': 'new:1', 'subject': 'Repair the pump',
                'throughline': 'x' * length, 'event_policy': 'default', 'status': 'active'}]}


@pytest.mark.parametrize('length', [600, 601, 692, 751, 2400, 2401])
def test_scoped_length_bound_preserves_full_text_and_default(length):
    output = packet(length)
    original = copy.deepcopy(output)
    with editorial_review_scope(False):
        if length > 2400:
            with pytest.raises(ValueError, match='bounded subject'):
                normalize(output)
        else:
            _, cards, _ = normalize(output)
            assert cards[0]['throughline'] == 'x' * length
    assert output == original
    if length > 600:
        with pytest.raises(ValueError, match='bounded subject'):
            normalize(output)
    else:
        normalize(output)


@pytest.mark.parametrize('invalid', ['empty', 'long_subject', 'unknown_track', 'missing_source', 'bridge'])
def test_relaxed_length_retains_structural_rejections(invalid):
    output = packet(751)
    if invalid == 'empty':
        output['track_updates'][0]['throughline'] = ''
    elif invalid == 'long_subject':
        output['track_updates'][0]['subject'] = 'x' * 161
    elif invalid == 'unknown_track':
        output['message_assignments'][0]['primary_track_ref'] = 'foreign'
    elif invalid == 'missing_source':
        output['message_assignments'] = []
    else:
        output['message_assignments'][0]['routing_role'] = 'bridge'
    with editorial_review_scope(False), pytest.raises(ValueError):
        normalize(output)


def test_long_card_finishes_real_synthetic_add_without_retries(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []

    async def complete(model, payload, **kwargs):
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        calls.append(request['role'])
        output = role_output(request['role'], request)
        if request['role'] == 'track_router':
            assert '2400' in payload['messages'][0]['content']
            output['track_updates'][0]['throughline'] = 'x' * 751
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    receipt = add()
    assert calls == ['track_router', 'event_curator', 'event_writer']
    assert add() == receipt
    with Store(engine._paths('user-a').database, read_only=True) as store:
        literal = json.loads(store.conn.execute(
            "SELECT a.output_text FROM pipeline_attempts a JOIN pipeline_jobs j ON j.id=a.job_id "
            "WHERE j.role LIKE 'track_router%' AND a.output_text!='' AND a.error='' LIMIT 1").fetchone()[0])
        assert literal['track_updates'][0]['throughline'] == 'x' * 751
        assert not store.conn.execute("SELECT 1 FROM pipeline_batches WHERE status='paused_failure'").fetchone()
