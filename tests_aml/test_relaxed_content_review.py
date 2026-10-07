"""AML editorial opt-out runs through actual public jobs and durable settlement."""
import asyncio
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from serein.extensions.pipeline_policy import editorial_review_scope
from tests_aml.test_engine import memory, role_output, add
from tests_aml.test_arc_format import candidate, propose


@pytest.mark.parametrize('variant', ['missing_review', 'invalid_review', 'no_primary_activity', 'round_and_material_gate', 'defer_review'])
def test_editorial_failures_do_not_buy_rewrites_or_fake_reviews(memory, monkeypatch, variant):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    configuration = runtime.configuration
    if variant == 'round_and_material_gate':
        def configured():
            changes, marker = configuration()
            changes['pipeline'].update(material_review_enabled=True, round_gate_enabled=True)
            return changes, marker
        monkeypatch.setattr(runtime, 'configuration', configured)
    current, calls = {}, []
    stage = runtime.run_stage
    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)
    async def complete(model, payload, **kwargs):
        role, request = current['role'], current['request']
        calls.append(role)
        output = role_output(role, request)
        if role == 'track_router' and variant == 'no_primary_activity':
            for row in output['message_assignments']:
                row['routing_role'] = 'origin'
        if role == 'event_curator':
            assert 'No decision_review' in payload['messages'][0]['content']
            marker = '<event_curator_input_json>'
            assert payload['messages'][1]['content'].split(marker)[1].split('</event_curator_input_json>')[0] == request['prompt'].split(marker)[1].split('</event_curator_input_json>')[0]
            if variant == 'round_and_material_gate':
                assert request['component']['writer_round_gate']
                assert request['component']['writer_material_review']
            output.pop('decision_review')
            if variant == 'invalid_review':
                output['decision_review'] = 'Malformed editorial notes are archived, not certified.'
            if variant == 'defer_review':
                stable = {row['id'] for row in request['component']['messages']}
                roots = [unit['unit_root_message_id'] for unit in request['component']['memberships']
                         if set(unit['source_message_ids']) & stable]
                output.update(events=[], defer_unit_roots=roots,
                    decision_review={'events': [], 'boundaries': [], 'dispositions': [{
                        'disposition': 'defer', 'unit_roots': roots, 'reason': '',
                        'parked_source_message_ids': [999999]}]})
            with editorial_review_scope(True), pytest.raises(ValueError):
                pipeline.latest.normalize_event_curator_output(output, request['component'])
        if role == 'event_writer':
            output = {key: output[key] for key in ('evidence_sufficient', 'title', 'event_draft')}
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr(runtime, 'run_stage', run_stage)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    messages = None
    if variant == 'defer_review':
        messages = [{'role': 'user', 'content': 'Alice moved to Paris.'},
                    {'role': 'assistant', 'content': 'You moved to Paris.'},
                    {'role': 'user', 'content': 'A pending clarification.'}]
    result = add(messages)
    assert calls.count('event_curator') == 1
    assert calls.count('event_writer') == (0 if variant == 'defer_review' else 1)
    assert add(messages) == result and calls.count('event_curator') == 1
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 0
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'complete'
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == (3 if messages else 2)
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == (0 if variant == 'defer_review' else 1)
        if variant == 'defer_review':
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0


@pytest.mark.parametrize('error', ['foreign_root', 'read_only_owner', 'overlap'])
def test_source_guards_still_block_settlement(memory, monkeypatch, error):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    async def runner(role, request):
        output = role_output(role, request)
        if role == 'event_curator':
            component = request['component']
            stable = {row['id'] for row in component['messages']}
            roots = [u['unit_root_message_id'] for u in component['memberships'] if set(u['source_message_ids']) & stable]
            output['events'][0]['owned_unit_roots'] = roots
            output.pop('decision_review')
            if error == 'overlap':
                output['skip_unit_roots'] = roots
            else:
                output['events'][0]['owned_unit_roots'].append(999999 if error == 'foreign_root' else 3)
        return output
    monkeypatch.setattr(runtime, 'run_stage', lambda settings, role, request: runner(role, request))
    with pytest.raises(RuntimeError):
        add([{'role': 'user', 'content': 'Alice moved.'}, {'role': 'assistant', 'content': 'Paris.'},
             {'role': 'user', 'content': 'Pending clarification.'}])
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 3
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute('SELECT status FROM aml_add_receipts').fetchone()[0] == 'pending'


def test_policy_is_task_local_and_strict_by_default():
    from serein.extensions.pipeline_latest import validate_event_writer_result
    output = {'evidence_sufficient': True, 'title': 'Move', 'event_draft': 'Alice moved.'}
    async def check():
        entered, inspected = asyncio.Event(), asyncio.Event()
        async def relaxed():
            with editorial_review_scope(False):
                entered.set()
                await inspected.wait()
                assert validate_event_writer_result(output) == []
            assert validate_event_writer_result(output)
        async def strict():
            await entered.wait()
            assert validate_event_writer_result(output)
            inspected.set()
        await asyncio.gather(relaxed(), strict())
    asyncio.run(check())


def test_content_policy_requires_new_profile(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    runtime.bootstrap(engine._paths('user-a'))
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    with pytest.raises(runtime.ProfileConflict):
        runtime.bootstrap(engine._paths('user-a'))
    monkeypatch.delenv('SEREIN_AML_SIMPLIFY_AUTHORING')
    assert not runtime.relaxed_content_review()


@pytest.mark.parametrize('row', [candidate(target_narrative_id='invented'),
    candidate(materials=[{'source_type': 'event', 'source_id': key} for key in ('ev-2', 'ev-1', 'foreign')])])
def test_scout_accepts_safe_subset_without_paid_correction(tmp_path, monkeypatch, row):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    result, calls, attempts = propose(tmp_path, monkeypatch, [json.dumps({'candidates': [row]})])
    assert len(calls) == 1 and not attempts[0]['error']
    if row['target_narrative_id']:
        assert result == []
    else:
        assert result[0]['source_event_ids'] == ['ev-2', 'ev-1']
    with Store(tmp_path/'memory.sqlite', read_only=True) as store:
        audit = json.loads(store.conn.execute('SELECT validation_json FROM aml_scout_attempts').fetchone()[0])
        assert audit['policy'] == 'safe_subset' and audit['candidates'] == result
