"""Protected Event deferrals finish indexing, without revisiting a shorter frame."""
import json

import pytest

from aml import engine, originals, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from tests_aml.test_engine import memory, add, role_output, search


@pytest.mark.parametrize('pending_question', [False, True])
def test_protected_tail_does_not_block_complete_add_or_lose_original(memory, monkeypatch, pending_question):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
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
        if role == 'track_router' and request['messages'][0]['id'] > 2:
            old = request['active_tracks'][0]
            output['track_updates'].append({'track_ref': old['track_id'],
                **{key: old[key] for key in ('subject', 'throughline', 'event_policy', 'status')}})
            for row in output['message_assignments']:
                if row['source_message_id'] in (3, 4):
                    row['primary_track_ref'] = old['track_id']
        elif role == 'event_curator':
            component = request['component']
            stable = {row['id'] for row in component['messages']}
            roots = [unit['unit_root_message_id'] for unit in component['memberships']
                     if set(unit['source_message_ids']) & stable]
            output['events'][0]['owned_unit_roots'] = roots
            bases = component['base_event_candidates']
            if bases:
                output['events'][0].update(action='extend', base_event_ids=[bases[0]['event_id']])
            output.pop('decision_review')
        elif role == 'event_writer':
            output = {key: output[key] for key in ('title', 'event_draft', 'evidence_sufficient')}
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    add()
    database = engine._paths('user-a').database
    with Store(database) as store:
        old_id, old_body = store.conn.execute('SELECT item_id,body FROM fact_events').fetchone()
        source_id = store.conn.execute('SELECT source_id FROM evidence_bindings WHERE document_id=?', (old_id,)).fetchone()[0]
        store.create('synthetic_scene', 'scene', 'Move sketch', 'A sketch of the old move.')
        store.bind('synthetic_scene', source_id)
    calls.clear()
    messages = [{'role': 'user', 'timestamp': 1704067320000, 'content': 'Alice added a red desk to the Paris move.'},
                {'role': 'assistant', 'timestamp': 1704067380000, 'content': 'The red desk is part of the move.'},
                {'role': 'user', 'timestamp': 1704067440000, 'content': 'The cafe in Paris opens tomorrow.'},
                {'role': 'assistant', 'timestamp': 1704067500000, 'content': 'The cafe opens tomorrow.'}]
    if pending_question:
        messages.append({'role': 'user', 'timestamp': 1704067560000, 'content': 'What happens next?'})
    result = add(messages, request_id='req-2')
    assert calls == ['track_router', 'event_curator', 'event_curator', 'event_writer']
    with Store(database, read_only=True) as store:
        assert tuple(store.conn.execute('SELECT body,status FROM fact_events WHERE item_id=?', (old_id,)).fetchone()) == (old_body, 'active')
        assert store.conn.execute("SELECT count(*) FROM pipeline_batches WHERE status!='done'").fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM aml_add_receipts WHERE status='pending'").fetchone()[0] == 0
        assert {row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing')} == {1, 2, 5, 6}
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == len(messages) + 2
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
    assert {3, 4}.issubset({row['id'] for row in originals.pending(database)})
    assert any('red desk' in item['content'] for item in search())
    count = len(calls)
    assert add(messages, request_id='req-2') == result and len(calls) == count


def test_protected_tail_check_keeps_unfinished_batches_and_other_ready_sources(memory):
    add()
    database = engine._paths('user-a').database
    result = {'protected_deferrals': [{'defer_source_message_ids': [1]}]}
    with Store(database) as store:
        store.conn.execute('DELETE FROM raw_processing')
    assert runtime.only_protected_tails_pending(database, result) is False
    all_held = {'protected_deferrals': [{'defer_source_message_ids': [1, 2]}]}
    assert runtime.only_protected_tails_pending(database, all_held) is True
    with Store(database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('blocked','test','{}','needs_repair')")
    assert runtime.only_protected_tails_pending(database, all_held) is False
