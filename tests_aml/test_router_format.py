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


def frozen_router_task():
    return {'role': 'track_router', 'messages': [{'id': 1}, {'id': 2}],
            'active_tracks': [{'track_id': 'existing-a', 'subject': 'Repair the garden pump',
                'throughline': 'The outlet seal still leaks.', 'status': 'parked',
                'event_policy': 'rolling_engineering'}]}


def bridge_output(role):
    return {'message_assignments': [{'source_message_id': source_id, 'primary_track_ref': 'new:1',
                'context_track_refs': ['existing-a'], 'routing_role': role} for source_id in (1, 2)],
            'track_updates': [{'track_ref': 'new:1', 'subject': 'Buy a replacement seal',
                'throughline': 'The replacement is available at the local store.', 'status': 'active',
                'event_policy': 'default'}]}


@pytest.mark.parametrize('role', ['origin', 'primary_activity', 'landing', 'routine'])
def test_declared_bridge_marker_and_frozen_context_card_are_canonicalized(monkeypatch, role):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task = frozen_router_task()
    output = bridge_output(role)
    normalized = runtime.prepare_router_output(task, output)
    assignments, updates, ordinal = pipeline.normalize_event_track_message_output(
        normalized, task['messages'], task['active_tracks'], session_id='test', next_track_ordinal=1)
    assert all(row['routing_role'] == 'bridge' and row['context_track_ids'] == ['existing-a']
               and row['primary_track_id'] == 'session_test_track_0001' for row in assignments)
    assert ordinal == 2
    carried = next(row for row in updates if row['track_id'] == 'existing-a')
    assert carried == task['active_tracks'][0]  # No new throughline, policy or status.
    assert len(normalized['track_updates']) == 2
    assert [row['context_track_refs'] for row in normalized['message_assignments']] == [['existing-a'], ['existing-a']]
    assert len(output['track_updates']) == 1 and output['message_assignments'][0]['routing_role'] == role


@pytest.mark.parametrize('invalid', ['foreign', 'duplicate', 'missing_new_card', 'bad_role', 'unused_update'])
def test_bridge_tolerance_still_rejects_invalid_routes_and_new_card_omissions(monkeypatch, invalid):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task, output = frozen_router_task(), bridge_output('landing')
    if invalid == 'foreign':
        output['message_assignments'][0]['context_track_refs'] = ['foreign-track']
    elif invalid == 'duplicate':
        output['message_assignments'][0]['context_track_refs'] = ['existing-a', 'existing-a']
    elif invalid == 'missing_new_card':
        output['track_updates'] = []
    elif invalid == 'bad_role':
        output['message_assignments'][0]['routing_role'] = 'invented-role'
    else:
        output['track_updates'].append({**output['track_updates'][0], 'track_ref': 'new:2'})
    normalized = runtime.prepare_router_output(task, output)
    with pytest.raises(ValueError):
        pipeline.normalize_event_track_message_output(normalized, task['messages'], task['active_tracks'],
                                                     session_id='test', next_track_ordinal=1)


def test_existing_primary_card_omission_runs_real_pipeline_once(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    add()  # Freeze a real existing card in this synthetic user's database.
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    current, calls = {}, []
    stage = runtime.run_stage

    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)

    async def complete(model, payload, **kwargs):
        role, request = current['role'], current['request']
        calls.append(role)
        output = role_output(role, request)
        if role == 'track_router':
            card = request['active_tracks'][0]
            for row in output['message_assignments']:
                row['primary_track_ref'] = card['track_id']
            output['track_updates'] = []
        elif role == 'event_curator':
            stable = {row['id'] for row in request['component']['messages']}
            output['events'][0]['owned_unit_roots'] = [u['unit_root_message_id']
                for u in request['component']['memberships'] if set(u['source_message_ids']) & stable]
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    payload = [{'role': 'user', 'timestamp': 1704067320000, 'content': 'The Paris apartment lease is now signed.'},
               {'role': 'assistant', 'timestamp': 1704067380000, 'content': 'The apartment lease is signed.'}]
    result = add(payload, request_id='req-2')
    assert calls == ['track_router', 'event_curator', 'event_writer']
    assert add(payload, request_id='req-2') == result
    assert calls == ['track_router', 'event_curator', 'event_writer']
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 4
        assert store.conn.execute('SELECT count(*) FROM pipeline_tracks').fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM aml_add_receipts WHERE status='complete'").fetchone()[0] == 2


def test_explicit_card_update_wins_and_default_policy_does_not_carry_cards(monkeypatch):
    task, output = frozen_router_task(), bridge_output('bridge')
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING', raising=False)
    assert runtime.prepare_router_output(task, output) is output
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    supplied = {'track_ref': 'existing-a', 'subject': 'Repair the garden pump',
                'throughline': 'The replacement seal was fitted.', 'status': 'active', 'event_policy': 'rolling_engineering'}
    output['track_updates'].append(supplied)
    assert runtime.prepare_router_output(task, output)['track_updates'][-1] == supplied
