"""Benchmark formatting tolerance preserves public ownership and original tails."""
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from test_engine import memory, role_output, add


@pytest.mark.parametrize('shape', ['skip', 'defer', 'object', 'unknown', 'ownership', 'overlap'])
def test_readonly_tail_disposition_settles_stable_sources_once(memory, monkeypatch, shape):
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
        output = role_output(role, request)
        if role == 'event_curator':
            component = request['component']
            stable = {row['id'] for row in component['messages']}
            roots = [unit['unit_root_message_id'] for unit in component['memberships']
                     if set(unit['source_message_ids']) & stable]
            readonly = [unit['unit_root_message_id'] for unit in component['memberships']
                        if not set(unit['source_message_ids']) & stable]
            assert readonly
            output['events'][0]['owned_unit_roots'] = roots
            disposition = {'disposition': 'defer' if shape == 'defer' else 'skip',
                'unit_roots': readonly, 'reason': 'This is only a pending context tail.',
                'parked_source_message_ids': readonly if shape == 'defer' else []}
            key = disposition['disposition'] + '_unit_roots'
            output[key] = [disposition] if shape == 'object' else readonly
            if shape != 'object':
                output['decision_review']['dispositions'] = [disposition]
            if shape == 'unknown':
                output['skip_unit_roots'].append(999)
            elif shape == 'ownership':
                output['events'][0]['owned_unit_roots'].extend(readonly)
            elif shape == 'overlap':
                output['skip_unit_roots'].extend(roots)
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    messages = [{'role': 'user', 'content': 'Alice moved to Paris.'},
                {'role': 'assistant', 'content': 'You moved to Paris.'},
                {'role': 'user', 'content': 'A pending question about the next trip.'}]
    if shape in ('unknown', 'ownership', 'overlap'):
        with pytest.raises(RuntimeError, match='pipeline did not complete'):
            add(messages)
        assert calls.count('event_curator') == 3
        assert calls.count('event_writer') == 0
        with Store(engine._paths('user-a').database, read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 3
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
            assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'pending'
        return
    result = add(messages)
    assert calls.count('event_curator') == 1
    assert calls.count('event_writer') == 1
    before = len(calls)
    assert add(messages) == result
    assert len(calls) == before
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 3
        assert {row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing')} == {1, 2}
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'complete'
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
        raw = store.conn.execute("SELECT output_text FROM pipeline_attempts WHERE job_id LIKE '%event_curator:0'").fetchone()[0]
        assert json.loads(raw)[('defer' if shape == 'defer' else 'skip') + '_unit_roots']


def task():
    return {'role': 'event_curator', 'component': {'messages': [{'id': 1}],
        'memberships': [{'unit_root_message_id': 1, 'source_message_ids': [1]},
                        {'unit_root_message_id': 2, 'source_message_ids': [2]}]}}


def test_misplaced_stable_disposition_preserves_reason_and_parked_ids(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    receipt = {'disposition': 'defer', 'unit_roots': ['1'], 'reason': 'A later correction.',
               'parked_source_message_ids': ['2']}
    output = {'events': [], 'skip_unit_roots': [], 'defer_unit_roots': [receipt],
              'decision_review': {'events': [], 'boundaries': [], 'dispositions': []}}
    normalized = runtime.prepare_curator_output(task(), runtime.stage_json(json.dumps(output), 'event_curator'))
    assert normalized['defer_unit_roots'] == [1]
    assert normalized['decision_review']['dispositions'] == [{**receipt, 'unit_roots': [1], 'parked_source_message_ids': [2]}]
    assert output['defer_unit_roots'] == [receipt]


def test_unknown_ids_ownership_and_conflicting_dispositions_are_not_repaired(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    output = {'events': [{'owned_unit_roots': [2, 999]}], 'skip_unit_roots': [1, 999],
        'defer_unit_roots': [1, 2], 'decision_review': {'dispositions': [
            {'disposition': 'defer', 'unit_roots': [1, 2, 999], 'reason': 'literal',
             'parked_source_message_ids': [2]}]}}
    normalized = runtime.prepare_curator_output(task(), output)
    assert normalized['events'] == output['events']
    assert normalized['skip_unit_roots'] == [1, 999]
    assert normalized['defer_unit_roots'] == [1]
    assert normalized['decision_review']['dispositions'][0]['unit_roots'] == [1, 999]


def test_tolerance_requires_opt_in_and_leaves_context_requests_alone(monkeypatch):
    output = {'events': [], 'skip_unit_roots': [2], 'defer_unit_roots': []}
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING', raising=False)
    assert runtime.prepare_curator_output(task(), output) is output
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    context = {'context_request': {'reason': 'missing_subject'}}
    assert runtime.prepare_curator_output(task(), context) is context
