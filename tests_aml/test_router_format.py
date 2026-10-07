"""Declared route representation repairs and durable paid validation retry limits."""
import copy
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from test_engine import memory, role_output, add


def test_lite_router_repairs_self_bridge_by_model_decision(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []

    async def complete(model, payload, **kwargs):
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        role = request['role']
        calls.append(role)
        output = role_output(role, request)
        if role == 'track_router':
            rules, prompt = [row['content'] for row in payload['messages']]
            assert rules == runtime.LITE_ROUTER_RULES and len(rules) < len(request['rules'])
            marker = '<active_tracks_json>'
            frozen = marker + request['prompt'].split(marker, 1)[1]
            assert frozen in prompt
            assert 'HOST ROUTING COVERAGE' not in prompt
            if calls.count(role) == 1:
                output['message_assignments'][0].update(
                    routing_role='bridge', context_track_refs=['new:1'])
            else:
                assert 'A same-Track reply is not a bridge' in prompt
                assert 'choose its non-bridge role' in prompt
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    receipt = add()
    assert calls == ['track_router', 'track_router', 'event_curator', 'event_writer']
    assert add() == receipt
    with Store(engine._paths('user-a').database, read_only=True) as store:
        attempts = store.conn.execute("SELECT output_text,error FROM pipeline_attempts WHERE output_text!='' ORDER BY rowid").fetchall()
        assert json.loads(attempts[0][0])['message_assignments'][0]['routing_role'] == 'bridge'
        assert 'bridge role must match' in attempts[0][1]
        assert attempts[1][1] == ''
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2


def test_lite_router_preserves_frozen_packet_and_defaults_off(monkeypatch):
    task = {'role': 'track_router', 'rules': 'original rules', 'messages': [{'id': 1}],
            'prompt': 'Date\nRange\nLong instructions\n<active_tracks_json>\n[]\n</active_tracks_json>\n'
                      '<raw_messages_json>[{"text":"<active_tracks_json> is quoted data"}]</raw_messages_json>\nTAIL'}
    original = copy.deepcopy(task)
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.delenv('SEREIN_AML_RELAX_CONTENT_REVIEW', raising=False)
    assert runtime.writer_payload(task) == (task['rules'], task['prompt'])
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    rules, prompt = runtime.writer_payload(task)
    assert rules == runtime.LITE_ROUTER_RULES
    assert prompt.split('<active_tracks_json>', 1)[1] == task['prompt'].split('<active_tracks_json>', 1)[1]
    assert task == original


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


@pytest.mark.parametrize('padding', [False, True])
def test_existing_primary_card_omission_runs_real_pipeline_once(memory, monkeypatch, padding):
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
            assert card['track_id'] in payload['messages'][1]['content']
            assert 'copy each ID exactly, including zero padding' in payload['messages'][1]['content']
            prefix, ordinal = card['track_id'].rsplit('_', 1)
            ref = prefix + '_' + ordinal.lstrip('0') if padding else card['track_id']
            for row in output['message_assignments']:
                row['primary_track_ref'] = ref
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
        audit = json.loads(store.conn.execute('SELECT decision_json FROM aml_router_normalizations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        assert audit['policy'] == 'declared_route_format'
        assert audit['original']['track_updates'] == []
        assert len(audit['normalized']['track_updates']) == 1
        stored_id = store.conn.execute('SELECT id FROM pipeline_tracks').fetchone()[0]
        assert audit['normalized']['message_assignments'][0]['primary_track_ref'] == stored_id
        if padding:
            assert audit['original']['message_assignments'][0]['primary_track_ref'] != stored_id
        literal = json.loads(store.conn.execute("SELECT a.output_text FROM pipeline_attempts a JOIN aml_router_normalizations n ON n.job_id=a.job_id WHERE a.output_text!='' AND a.error='' ORDER BY a.rowid DESC LIMIT 1").fetchone()[0])
        assert literal == audit['original']


def numbered_router_task():
    task = frozen_router_task()
    task['active_tracks'][0]['track_id'] = 'session_abc123_track_0002'
    return task


@pytest.mark.parametrize('suffix', ['2', '02', '002', '000002'])
@pytest.mark.parametrize('field', ['primary', 'context', 'update'])
def test_only_zero_padding_changes_resolve_to_a_frozen_exact_id(monkeypatch, suffix, field):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task, output = numbered_router_task(), bridge_output('landing')
    exact = task['active_tracks'][0]['track_id']
    malformed = 'session_abc123_track_' + suffix
    for row in output['message_assignments']:
        row['context_track_refs'] = [malformed if field == 'context' else exact]
        if field == 'primary':
            row.update(primary_track_ref=malformed, context_track_refs=[], routing_role='primary_activity')
    if field == 'primary':
        output['track_updates'] = []
    elif field == 'update':
        output['track_updates'].append({**task['active_tracks'][0], 'track_ref': malformed})
        output['track_updates'][-1].pop('track_id')
    literal = copy.deepcopy(output)
    normalized = runtime.prepare_router_output(task, output)
    assignments, updates, _ = pipeline.normalize_event_track_message_output(
        normalized, task['messages'], task['active_tracks'], session_id='test', next_track_ordinal=1)
    assert output == literal
    assert [row['source_message_id'] for row in assignments] == [1, 2]
    assert next(row for row in updates if row['track_id'] == exact) == task['active_tracks'][0]
    assert all((row['primary_track_id'] == exact if field == 'primary' else row['context_track_ids'] == [exact])
               for row in assignments)
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING')
    assert runtime.prepare_router_output(task, output) is output
    with pytest.raises(ValueError):
        pipeline.normalize_event_track_message_output(output, task['messages'], task['active_tracks'],
                                                     session_id='test', next_track_ordinal=1)


@pytest.mark.parametrize('ref', ['session_other_track_002', 'session_abc123_track_003',
                               'session_abc123_track_000', 'abc123_track_002',
                               'session_abc123_track_2x', 'the garden pump'])
def test_unknown_and_non_number_track_refs_are_not_guessed(monkeypatch, ref):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task, output = numbered_router_task(), bridge_output('landing')
    for row in output['message_assignments']:
        row['context_track_refs'] = [ref]
    normalized = runtime.prepare_router_output(task, output)
    assert normalized['message_assignments'][0]['context_track_refs'] == [ref]
    with pytest.raises(ValueError):
        pipeline.normalize_event_track_message_output(normalized, task['messages'], task['active_tracks'],
                                                     session_id='test', next_track_ordinal=1)


@pytest.mark.parametrize('invalid', ['ambiguous', 'duplicate_context', 'duplicate_update', 'missing_source'])
def test_number_format_recovery_does_not_hide_route_errors(monkeypatch, invalid):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task, output = numbered_router_task(), bridge_output('landing')
    exact = task['active_tracks'][0]['track_id']
    malformed = 'session_abc123_track_002'
    for row in output['message_assignments']:
        row['context_track_refs'] = [malformed]
    if invalid == 'ambiguous':
        task['active_tracks'].append({**task['active_tracks'][0], 'track_id': 'session_abc123_track_02'})
    elif invalid == 'duplicate_context':
        output['message_assignments'][0]['context_track_refs'].append(exact)
    elif invalid == 'duplicate_update':
        update = {key: value for key, value in task['active_tracks'][0].items() if key != 'track_id'}
        output['track_updates'].extend([{**update, 'track_ref': ref} for ref in (malformed, exact)])
    else:
        output['message_assignments'].pop()
    normalized = runtime.prepare_router_output(task, output)
    with pytest.raises(ValueError):
        pipeline.normalize_event_track_message_output(normalized, task['messages'], task['active_tracks'],
                                                     session_id='test', next_track_ordinal=1)


def test_exact_ids_win_over_ambiguous_numeric_aliases(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    task, output = numbered_router_task(), bridge_output('bridge')
    task['active_tracks'].append({**task['active_tracks'][0], 'track_id': 'session_abc123_track_02'})
    for row in output['message_assignments']:
        row['context_track_refs'] = ['session_abc123_track_02']
    normalized = runtime.prepare_router_output(task, output)
    assignments, _, _ = pipeline.normalize_event_track_message_output(
        normalized, task['messages'], task['active_tracks'], session_id='test', next_track_ordinal=1)
    assert all(row['context_track_ids'] == ['session_abc123_track_02'] for row in assignments)


def test_explicit_card_update_wins_and_default_policy_does_not_carry_cards(monkeypatch):
    task, output = frozen_router_task(), bridge_output('bridge')
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING', raising=False)
    assert runtime.prepare_router_output(task, output) is output
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    supplied = {'track_ref': 'existing-a', 'subject': 'Repair the garden pump',
                'throughline': 'The replacement seal was fitted.', 'status': 'active', 'event_policy': 'rolling_engineering'}
    output['track_updates'].append(supplied)
    assert runtime.prepare_router_output(task, output)['track_updates'][-1] == supplied
