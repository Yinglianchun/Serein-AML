"""Self-context normalization and durable paid validation retry limits."""
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from test_engine import memory, role_output, add


@pytest.mark.parametrize('kind', ['self', 'unknown', 'bridge', 'empty'])
def test_router_self_context_is_redundant_not_a_new_relation(memory, monkeypatch, kind):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    current, calls = {}, []
    stage = runtime.run_stage

    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)

    async def complete(model, payload, **kwargs):
        role, request = current['role'], current['request']
        calls.append(role)
        if role == 'track_router' and kind == 'empty':
            return {'choices': [{'message': {'content': ''}}]}
        output = role_output(role, request)
        if role == 'track_router':
            for row in output['message_assignments']:
                row['context_track_refs'] = [row['primary_track_ref']]
                if kind == 'unknown':
                    row['context_track_refs'].append('not-a-declared-track')
                elif kind == 'bridge':
                    row['routing_role'] = 'bridge'
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    if kind == 'self':
        result = add()
        assert calls == ['track_router', 'event_curator', 'event_writer']
        assert add() == result
        with Store(engine._paths('user-a').database, read_only=True) as store:
            routes = [json.loads(row[0]) for row in store.conn.execute('SELECT route_json FROM pipeline_routes')]
            assert len(routes) == 2 and all(row['context_track_ids'] == [] for row in routes)
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
        return
    with pytest.raises(RuntimeError):
        add()
    assert calls == ['track_router'] * 3
    # Platform retries used to multiply three corrections by three host rounds.
    # They now spend no further model calls until an explicit public batch retry.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            add()
    assert calls == ['track_router'] * 3
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'pending'
        batch = store.conn.execute("SELECT id FROM pipeline_batches WHERE status='paused_failure'").fetchone()[0]
    pipeline.retry_batch(engine._paths('user-a').database, batch)
    with pytest.raises(RuntimeError):
        add()
    assert calls == ['track_router'] * 6  # Only explicit recovery resets the budget.


def test_router_format_preserves_foreign_duplicate_refs_and_defaults_off(monkeypatch):
    request = {'role': 'track_router'}
    output = {'message_assignments': [{'primary_track_ref': 'track-a',
                                      'context_track_refs': ['track-a', 'track-b', 'track-b', 7]}]}
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING', raising=False)
    assert runtime.prepare_router_output(request, output) is output
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    normalized = runtime.prepare_router_output(request, output)
    assert normalized['message_assignments'][0]['context_track_refs'] == ['track-b', 'track-b', 7]
    assert output['message_assignments'][0]['context_track_refs'][0] == 'track-a'
