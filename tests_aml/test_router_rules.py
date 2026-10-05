from serein.extensions import pipeline_latest as latest
from test_engine import memory, add
import test_engine


def test_aml_uses_larger_public_defaults_without_overriding_saved_limits(memory):
    from serein.deployment import read_settings, save_settings
    from serein.extensions.pipeline_limits import blocks
    import aml.engine as engine
    add()
    database = engine._paths('user-a').database
    policy = read_settings(database)['pipeline']
    assert policy['max_input_chars'] == 40000
    assert policy['max_prompt_chars'] == 200000
    save_settings(database, {'pipeline': {'max_input_chars': 8000, 'max_prompt_chars': 50000}})
    add(request_id='req-2')
    policy = read_settings(database)['pipeline']
    assert policy['max_input_chars'] == 8000
    assert policy['max_prompt_chars'] == 50000
    messages = [{'id': i, 'role': role, 'content': '原' * 6000,
                 'created_at': '2026-10-04T00:00:00Z', 'metadata': {'timestamp_source': 'import_time'}}
                for i, role in enumerate(['user', 'assistant'] * 4, 1)]
    chunks = blocks(messages)
    assert [len(chunk) for chunk in chunks] == [6, 2]
    assert [message for chunk in chunks for message in chunk] == messages


def test_add_router_request_has_one_complete_rule_copy(memory, monkeypatch):
    original = test_engine.role_output
    captured = []
    def role_output(role, request):
        if role == 'track_router':
            captured.append(request)
        return original(role, request)
    monkeypatch.setattr(test_engine, 'role_output', role_output)
    add()
    assert captured
    for request in captured:
        with latest.identity_scope(request['identity']):
            rules = latest.materialize_agent_rules('track_router')
        assert request['rules'] == rules
        assert rules not in request['prompt']
        assert (request['rules'] + request['prompt']).count(rules) == 1
