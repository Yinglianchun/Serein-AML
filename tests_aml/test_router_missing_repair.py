"""Frozen Router coverage repairs; no external calls or production databases."""
import copy
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from test_engine import memory, role_output, add


def packet():
    request = {'role': 'track_router', 'messages': [{'id': i} for i in (3, 4, 7)], 'active_tracks': []}
    return request, role_output('track_router', request)


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


@pytest.mark.parametrize('bad_patch', [False, True])
def test_run_stage_targeted_patch_keeps_literal_attempts(memory, monkeypatch, bad_patch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []

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
    assert add() == receipt
    with Store(engine._paths('user-a').database, read_only=True) as store:
        attempts = store.conn.execute("SELECT output_text,error FROM pipeline_attempts WHERE output_text!='' ORDER BY rowid").fetchall()
        assert len(json.loads(attempts[0][0])['message_assignments']) == 1
        assert attempts[0][1]
        final = json.loads(attempts[len(calls)-1][0])
        assert len(final['message_assignments']) == 1 and final['track_updates'] == []
        assert attempts[len(calls)-1][1] == ''
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 2
