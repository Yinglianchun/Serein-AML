"""Frozen Router coverage repairs; no external calls or production databases."""
import copy
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from serein.extensions.pipeline_rules import normalize_event_track_message_output
from test_engine import memory, role_output, add


def packet():
    request = {'role': 'track_router', 'messages': [{'id': i} for i in (3, 4, 7)], 'active_tracks': []}
    return request, role_output('track_router', request)


def existing_packet():
    request, output = packet()
    card = output['track_updates'][0]
    card['track_ref'] = 'track-existing'
    card['event_policy'] = 'rolling_engineering'
    request['active_tracks'] = [{'track_id': card['track_ref'],
        **{key: value for key, value in card.items() if key != 'track_ref'}}]
    for row in output['message_assignments']:
        row['primary_track_ref'] = card['track_ref']
    return request, output


@pytest.mark.parametrize('change', [{'throughline': 'Download verified'}, {'status': 'parked'}])
def test_existing_card_accepts_only_changed_fields(monkeypatch, change):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = existing_packet()
    previous = output['track_updates'][0].copy()
    output['track_updates'] = [{'track_ref': 'track-existing', **change}]
    original = copy.deepcopy(output)
    prepared = runtime.prepare_router_output(request, output)
    _, cards, _ = normalize_event_track_message_output(prepared, request['messages'],
        request['active_tracks'], session_id=0, next_track_ordinal=1)
    assert cards == [{'track_id': 'track-existing',
        **{key: value for key, value in previous.items() if key != 'track_ref'}, **change}]
    assert output == original
    assert request['active_tracks'][0]['throughline'] == previous['throughline']
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '0')
    assert runtime.prepare_router_output(request, output) is output


@pytest.mark.parametrize('change', [{'subject': None}, {'throughline': ''}, {'status': 'completed'}])
def test_explicit_invalid_fields_do_not_inherit_old_values(monkeypatch, change):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = existing_packet()
    output['track_updates'] = [{'track_ref': 'track-existing', **change}]
    prepared = runtime.prepare_router_output(request, output)
    with pytest.raises(ValueError):
        normalize_event_track_message_output(prepared, request['messages'],
            request['active_tracks'], session_id=0, next_track_ordinal=1)


def test_sparse_new_card_still_requires_full_fields(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    output['track_updates'] = [{'track_ref': 'new:1', 'throughline': 'Download verified'}]
    with pytest.raises(ValueError, match='fields missing'):
        normalize_event_track_message_output(runtime.prepare_router_output(request, output),
            request['messages'], [], session_id=0, next_track_ordinal=1)


def test_sparse_patch_inherits_accepted_card_without_reverting_it(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = existing_packet()
    retained = copy.deepcopy(output)
    retained['message_assignments'] = retained['message_assignments'][::2]
    retained['track_updates'][0].update(throughline='Download verified', status='parked')
    patch = {'message_assignments': [output['message_assignments'][1]],
             'track_updates': [{'track_ref': 'track-existing', 'throughline': 'Download verified'}]}
    prepared = runtime.prepare_router_output(request, patch, fill_track_updates=False)
    merged = runtime.merge_router_patch(request, retained, prepared)
    assert merged['track_updates'] == retained['track_updates']
    assert merged['message_assignments'] == output['message_assignments']
    normalize_event_track_message_output(merged, request['messages'],
        request['active_tracks'], session_id=0, next_track_ordinal=1)
    patch['track_updates'][0]['status'] = 'active'
    with pytest.raises(ValueError, match='cannot change retained'):
        runtime.merge_router_patch(request, retained,
            runtime.prepare_router_output(request, patch, fill_track_updates=False))


def test_complete_unique_routes_are_reordered_without_changing_decisions(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    output['message_assignments'].reverse()
    original = copy.deepcopy(output)
    result = runtime.prepare_router_output(request, output)
    assert [row['source_message_id'] for row in result['message_assignments']] == [3, 4, 7]
    assert result['message_assignments'] == original['message_assignments'][::-1]
    assert output == original
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '0')
    assert runtime.prepare_router_output(request, output) is output


@pytest.mark.parametrize('ids', [[3, 3, 7], [3, 4, 99], [True, 4, 7], [3.0, 4, 7]])
def test_invalid_ids_are_never_reordered_or_targeted(monkeypatch, ids):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    for row, identifier in zip(output['message_assignments'], ids):
        row['source_message_id'] = identifier
    assert runtime.prepare_router_output(request, output) == output
    assert runtime.router_missing_ids(request, output) is None


def test_patch_preserves_routes_and_rejects_changes(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    retained = copy.deepcopy(output)
    retained['message_assignments'] = retained['message_assignments'][::2]
    original = copy.deepcopy(retained)
    assert runtime.router_missing_ids(request, retained) == [4]
    patch = {'message_assignments': [output['message_assignments'][1]], 'track_updates': []}
    assert runtime.merge_router_patch(request, retained, patch) == output
    assert retained == original
    patch['message_assignments'] = [output['message_assignments'][0]]
    with pytest.raises(ValueError, match='only assign missing'):
        runtime.merge_router_patch(request, retained, patch)
    patch['message_assignments'] = [output['message_assignments'][1]]
    patch['track_updates'] = copy.deepcopy(output['track_updates'])
    patch['track_updates'][0]['subject'] = 'Changed'
    with pytest.raises(ValueError, match='cannot change retained'):
        runtime.merge_router_patch(request, retained, patch)
    retained['message_assignments'][0]['routing_role'] = 'invalid'
    assert runtime.router_missing_ids(request, retained) is None


def test_multiple_partial_patches_accumulate_without_guessing(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    retained = copy.deepcopy(output)
    retained['message_assignments'] = retained['message_assignments'][:1]
    assert runtime.router_missing_ids(request, retained) == [4, 7]
    second = runtime.merge_router_patch(request, retained,
        {'message_assignments': [output['message_assignments'][2]], 'track_updates': []})
    assert runtime.router_missing_ids(request, second) == [4]
    final = runtime.merge_router_patch(request, second,
        {'message_assignments': [output['message_assignments'][1]], 'track_updates': []})
    assert final == output


def test_missing_patch_does_not_reintroduce_stale_frozen_card(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    request, output = packet()
    card = output['track_updates'][0]
    card['track_ref'] = 'track-existing'
    for row in output['message_assignments']:
        row['primary_track_ref'] = 'track-existing'
    request['active_tracks'] = [{'track_id': 'track-existing',
        **{k: v for k, v in card.items() if k != 'track_ref'}}]
    retained = copy.deepcopy(output)
    retained['message_assignments'] = retained['message_assignments'][::2]
    retained['track_updates'][0]['throughline'] = 'Updated stage'
    retained['track_updates'][0]['status'] = 'parked'
    patch = {'message_assignments': [output['message_assignments'][1]], 'track_updates': []}
    stale = runtime.prepare_router_output(request, patch)
    with pytest.raises(ValueError, match='cannot change retained'):
        runtime.merge_router_patch(request, retained, stale)
    prepared = runtime.prepare_router_output(request, patch, fill_track_updates=False)
    merged = runtime.merge_router_patch(request, retained, prepared)
    assert prepared['track_updates'] == []
    assert merged['track_updates'] == retained['track_updates']
    assert merged['message_assignments'] == output['message_assignments']
    explicit_change = copy.deepcopy(stale)
    with pytest.raises(ValueError, match='cannot change retained'):
        runtime.merge_router_patch(request, retained,
            runtime.prepare_router_output(request, explicit_change, fill_track_updates=False))


@pytest.mark.parametrize('bad_patch', [False, True])
def test_run_stage_targeted_patch_keeps_literal_attempts(memory, monkeypatch, bad_patch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []
    normalization_modes = []
    prepare = runtime.prepare_router_output

    def observe_prepare(request, output, **kwargs):
        if request['role'] == 'track_router':
            normalization_modes.append(kwargs.get('fill_track_updates', True))
        return prepare(request, output, **kwargs)

    monkeypatch.setattr(runtime, 'prepare_router_output', observe_prepare)

    async def complete(model, payload, **kwargs):
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        role = request['role']
        output = role_output(role, request)
        if role == 'track_router':
            calls.append(payload['messages'][1]['content'])
            if len(calls) == 1:
                output['message_assignments'] = output['message_assignments'][:1]
            else:
                assert 'MISSING-MESSAGE PATCH' in calls[-1]
                assert 'Retained routing JSON (read-only)' in calls[-1]
                output['message_assignments'] = output['message_assignments'][1:]
                output['track_updates'] = []
                if bad_patch and len(calls) == 2:
                    output['message_assignments'][0]['source_message_id'] = request['messages'][0]['id']
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    receipt = add()
    assert len(calls) == (3 if bad_patch else 2)
    assert normalization_modes[0] is True and False in normalization_modes
    assert add() == receipt
    with Store(engine._paths('user-a').database, read_only=True) as store:
        attempts = store.conn.execute("SELECT output_text,error FROM pipeline_attempts WHERE output_text!='' ORDER BY rowid").fetchall()
        assert len(json.loads(attempts[0][0])['message_assignments']) == 1
        assert attempts[0][1]
        final = json.loads(attempts[len(calls)-1][0])
        assert len(final['message_assignments']) == 1 and final['track_updates'] == []
        assert attempts[len(calls)-1][1] == ''
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2


def test_sparse_update_survives_missing_row_repair_and_next_batch(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    router_calls, writers = [], []
    changing_batch = None
    new_cue = 'Mobile download verified'

    async def complete(model, payload):
        nonlocal changing_batch
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1'
            ).fetchone()[0])
        role = request['role']
        output = role_output(role, request)
        if role == 'track_router':
            router_calls.append(request)
            if not request['active_tracks']:
                output['track_updates'][0].update(subject='Repair download', throughline='Investigating download')
            else:
                ref = request['active_tracks'][0]['track_id']
                for row in output['message_assignments']:
                    row['primary_track_ref'] = ref
                if changing_batch is None:
                    changing_batch = request['batch_id']
                if request['batch_id'] == changing_batch:
                    output['track_updates'] = [{'track_ref': ref, 'throughline': new_cue}]
                    attempt = sum(r['batch_id'] == changing_batch for r in router_calls)
                    output['message_assignments'] = (output['message_assignments'][:1] if attempt == 1
                                                     else output['message_assignments'][1:])
                else:
                    assert request['active_tracks'][0]['throughline'] == new_cue
                    output['track_updates'] = []
        elif role == 'event_writer':
            writers.append(request)
            output['title'] = 'Download verification'
            if len(writers) > 1:
                assert request['component']['track_cards'][0]['throughline'] == new_cue
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    rounds = [('Alice is repairing download authentication.', 'Download verification is pending.'),
              ('Alice verified mobile downloads.', 'Mobile download verification passed.'),
              ('Alice checked desktop downloads too.', 'Desktop verification passed too.')]
    for i, texts in enumerate(rounds):
        messages = [{'role': role, 'timestamp': 1704067200000 + i * 120000 + j * 60000, 'content': text}
                    for j, (role, text) in enumerate(zip(('user', 'assistant'), texts))]
        add(messages, request_id=f'sparse-update-{i}')
    assert len(router_calls) == 4 and len(writers) == 3
    with Store(engine._paths('user-a').database, read_only=True) as store:
        cards = [json.loads(row[0]) for row in store.conn.execute('SELECT card_json FROM pipeline_tracks')]
        assert len(cards) == 1 and cards[0]['throughline'] == new_cue
        assert cards[0]['subject'] == 'Repair download'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 6
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 3
        assert store.conn.execute("SELECT count(*) FROM pipeline_attempts WHERE error!=''").fetchone()[0] == 1
