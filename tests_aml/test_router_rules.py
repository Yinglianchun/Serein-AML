from serein.extensions import pipeline_latest as latest
from test_engine import memory, add
import test_engine


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
