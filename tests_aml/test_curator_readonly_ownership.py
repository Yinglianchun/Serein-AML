"""Invalid context-only proposals cannot block indexed, retained originals."""
import json

import pytest

from aml import engine, originals, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from tests_aml.test_engine import memory, add, role_output


def test_readonly_only_proposal_on_visible_other_track_is_discarded(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    request = {'role': 'event_curator', 'component': {
        'track_ids': ['track-1'], 'messages': [{'id': i} for i in (8, 9, 10)],
        'memberships': [{'unit_root_message_id': i, 'source_message_ids': [i],
                         'track_id': 'track-2'} for i in (8, 9, 10, 11)]}}
    output = {'events': [{'action': 'create', 'base_event_ids': [], 'primary_track_id': 'track-2',
                         'owned_unit_roots': [11]}], 'skip_unit_roots': [8, 9, 10], 'defer_unit_roots': []}
    normalized = runtime.prepare_curator_output(request, output)
    assert normalized == {**output, 'events': []}
    assert output['events'][0]['owned_unit_roots'] == [11]
    output['events'][0]['primary_track_id'] = 'unknown-track'
    assert runtime.prepare_curator_output(request, output)['events'] == output['events']


def test_shared_bridge_scope_continues_into_its_other_valid_component(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls, current, phase = [], {}, {'new': False}
    stage = runtime.run_stage
    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)
    async def complete(model, payload, **kwargs):
        role, request = current['role'], current['request']
        calls.append(role)
        output = role_output(role, request)
        if role == 'track_router' and phase['new']:
            for row in output['message_assignments']:
                row.update(context_track_refs=[request['active_tracks'][0]['track_id']], routing_role='bridge')
        elif role == 'event_curator':
            component = request['component']
            stable = {row['id'] for row in component['messages']}
            roots = [u['unit_root_message_id'] for u in component['memberships'] if set(u['source_message_ids']) & stable]
            readonly = [u for u in component['memberships'] if not set(u['source_message_ids']) & stable]
            output['events'][0]['owned_unit_roots'] = roots
            output.pop('decision_review')
            if phase['new'] and calls.count('event_curator') == 1:
                assert readonly and readonly[0]['track_id'] not in component['track_ids']
                output['events'][0].update(primary_track_id=readonly[0]['track_id'],
                                          owned_unit_roots=[readonly[0]['unit_root_message_id']])
                output['skip_unit_roots'] = roots
        elif role == 'event_writer':
            output = {key: output[key] for key in ('title', 'event_draft', 'evidence_sufficient')}
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    add()
    calls.clear()
    phase['new'] = True
    add([{'role': 'user', 'timestamp':1704067320000, 'content': 'Alice moved to Paris.'},
         {'role': 'assistant', 'timestamp':1704067380000, 'content': 'The move is complete.'},
         {'role': 'user', 'timestamp':1704067440000, 'content': 'Will Alice move again?'}], request_id='req-2')
    assert calls == ['track_router', 'event_curator', 'event_curator', 'event_writer']
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 5
        assert {r[0] for r in store.conn.execute("SELECT raw_id FROM raw_processing WHERE outcome='settled'")} == {1, 2, 3, 4}
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 2
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'complete'


@pytest.mark.parametrize('shape', ['context_only', 'mixed', 'duplicate_stable', 'foreign', 'foreign_track', 'old_policy'])
def test_readonly_create_proposals_are_audited_without_owning_context(memory, monkeypatch, shape):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '0' if shape == 'old_policy' else '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls, current, literal = [], {}, []
    stage = runtime.run_stage
    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)
    async def complete(model, payload, **kwargs):
        role, request = current['role'], current['request']
        calls.append(role)
        output = role_output(role, request)
        if role == 'event_curator':
            component = request['component']
            stable = {row['id'] for row in component['messages']}
            writable = [unit['unit_root_message_id'] for unit in component['memberships'] if set(unit['source_message_ids']) & stable]
            readonly = [unit['unit_root_message_id'] for unit in component['memberships'] if not set(unit['source_message_ids']) & stable]
            assert readonly and writable
            output['events'][0]['owned_unit_roots'] = readonly
            output['skip_unit_roots'] = writable
            output['decision_review']['dispositions'] = [{'disposition': 'skip', 'unit_roots': writable,
                                                         'reason': 'Original evidence remains searchable.',
                                                         'parked_source_message_ids': []}]
            if shape in {'mixed', 'duplicate_stable'}:
                output['events'][0]['owned_unit_roots'] = [*writable, *readonly]
                output['skip_unit_roots'] = []
                if shape == 'duplicate_stable':
                    output['events'][0]['owned_unit_roots'].append(writable[0])
            if shape == 'foreign':
                output['events'][0]['owned_unit_roots'].append(999999)
            if shape == 'foreign_track':
                output['events'][0]['primary_track_id'] = 'not-an-allowed-track'
            literal.append(json.loads(json.dumps(output)))
        if role == 'event_writer':
            assert {row['id'] for row in request['messages']} == {1, 2}
            output = {key: output[key] for key in ('title', 'event_draft', 'evidence_sufficient')}
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    messages = [{'role': 'user', 'content': 'Alice moved to Paris.'},
                {'role': 'assistant', 'content': 'The move is complete.'},
                {'role': 'user', 'content': 'Will Alice move again?'}]
    if shape in {'foreign', 'foreign_track', 'duplicate_stable', 'old_policy'}:
        with pytest.raises(RuntimeError):add(messages)
        assert calls.count('event_curator') == 3 and 'event_writer' not in calls
        with Store(engine._paths('user-a').database, read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
            assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'pending'
        return
    first = add(messages)
    assert calls.count('event_curator') == 1
    assert calls.count('event_writer') == (1 if shape == 'mixed' else 0)
    assert add(messages) == first and calls.count('event_curator') == 1
    path = engine._paths('user-a').database
    with Store(path, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 3
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'complete'
        rows = list(store.conn.execute('SELECT raw_id,outcome FROM raw_processing ORDER BY raw_id'))
        assert [(r['raw_id'], r['outcome']) for r in rows] == [(1, 'settled' if shape == 'mixed' else 'skipped'), (2, 'settled' if shape == 'mixed' else 'skipped')]
        audit = json.loads(store.conn.execute('SELECT decision_json FROM aml_curator_normalizations').fetchone()[0])
        assert audit['original'] == literal[0] and audit['policy'] == 'safe_subset'
        if shape == 'context_only':assert audit['normalized']['events'] == []
        else:assert audit['normalized']['events'][0]['owned_unit_roots'] == [1, 2]
    pending = {row['id'] for row in originals.pending(path)}
    assert pending == ({3} if shape == 'mixed' else {1, 2, 3})
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: {'queries': ['Alice', 'Paris'], 'entities': ['Alice']})
    data = engine.search_memory(query='Where did Alice move?', options=None, user_id='user-a', top_k=100)
    assert any('Paris' in row['content'] for row in data)
