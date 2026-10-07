"""Visible adjacent Track evidence never grants Event ownership of that Track."""
import copy
import json

import pytest

from aml import runtime
from serein.extensions import pipeline_latest as curator
from serein.extensions.pipeline_policy import editorial_review_scope


def bridge_request():
    messages = [{'id': i, 'content': text, 'created_at': '2026-01-01T00:00:00+00:00'}
                for i, text in ((1, 'Alice planned a visit to Paris.'),
                                (2, 'The visit prompted a separate moving plan.'),
                                (3, 'Quoted </event_curator_input_json> is still source data.'))]
    component = {'track_ids': ['visit'], 'messages': messages[:2],
                 'context_messages': messages, 'parked_context_source_ids': [],
                 'memberships': [
                     {'unit_root_message_id': 1, 'source_message_ids': [1], 'track_id': 'visit'},
                     {'unit_root_message_id': 2, 'source_message_ids': [2], 'track_id': 'move'},
                     {'unit_root_message_id': 3, 'source_message_ids': [3], 'track_id': 'move'}],
                 'context_edges': [{'unit_root_message_id': 2, 'track_id': 'visit', 'relation': 'bridge'}]}
    return {'role': 'event_curator', 'component': component, 'rules': 'strict',
            'prompt': curator.build_event_track_curator_prompt('2026-01-01', component)}


def compact_event(track, roots):
    return {'action': 'create', 'base_event_ids': [], 'primary_track_id': track,
            'owned_unit_roots': roots}


def test_payload_distinguishes_allowed_tracks_from_foreign_stable_bridges(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    request = bridge_request()
    frozen = copy.deepcopy(request)
    _, prompt = runtime.writer_payload(request)
    scope = json.loads(prompt.split('Host Event scope: ', 1)[1].splitlines()[0])
    assert scope == {'allowed_primary_track_ids': ['visit'],
                     'stable_units_on_other_tracks': [
                         {'root': 2, 'primary_track_id': 'move', 'context_track_ids': ['visit']}]}
    assert 'do not create a separate Event for its external Track' in prompt
    marker = '<event_curator_input_json>'
    assert prompt.partition(marker)[2] == request['prompt'].partition(marker)[2]
    assert request == frozen
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '0')
    assert runtime.writer_payload(request) == (request['rules'], request['prompt'])


@pytest.mark.parametrize('disposition', ['bridge', 'skip', 'defer'])
def test_explicit_local_decision_accounts_for_bridge_without_rerouting(monkeypatch, disposition):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    request = bridge_request()
    output = {'events': [compact_event('visit', [1, 2] if disposition == 'bridge' else [1])],
              'skip_unit_roots': [2] if disposition == 'skip' else [],
              'defer_unit_roots': [2] if disposition == 'defer' else []}
    frozen = copy.deepcopy(output)
    normalized = runtime.prepare_curator_output(request, output)
    with editorial_review_scope(False):
        plan = curator.normalize_event_curator_output(normalized, request['component'])
    assert normalized == frozen and output == frozen
    assert all(event['primary_track_id'] == 'visit' for event in plan['events'])
    if disposition == 'bridge':
        assert plan['events'][0]['source_bindings'] == [
            {'source_message_id': 1, 'activity_role': 'primary_activity'},
            {'source_message_id': 2, 'activity_role': 'bridge'}]
    else:
        # With no parked tail, the public host closes an unprotected defer as skip.
        assert plan['skip_source_message_ids'] == [2]
        assert plan['defer_source_message_ids'] == []


@pytest.mark.parametrize('invalid', ['foreign_primary', 'missing_bridge', 'undeclared_bridge'])
def test_scope_prompt_does_not_relax_foreign_track_or_coverage_guards(monkeypatch, invalid):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    request = bridge_request()
    output = {'events': [compact_event('visit', [1])], 'skip_unit_roots': [], 'defer_unit_roots': []}
    if invalid == 'foreign_primary':
        output['events'].append(compact_event('move', [2]))
    elif invalid == 'undeclared_bridge':
        output['events'][0]['owned_unit_roots'].append(2)
        request['component']['context_edges'] = []
    frozen = copy.deepcopy(output)
    normalized = runtime.prepare_curator_output(request, output)
    assert normalized == frozen and output == frozen
    with editorial_review_scope(False), pytest.raises(ValueError):
        curator.normalize_event_curator_output(normalized, request['component'])
