import json

import pytest

from aml import arc_planning, delivery, engine
from tests_aml.test_arc_planning import planned, search, chooser
from serein.core.store import Store


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLE_PLANNING', '1')


def assess(result, **kwargs):
    return arc_planning.assess(lambda _: result, 'Where?', None,
        [{'ref': 'a', 'text': 'Mira joined Cedar.'}],
        {'story': {'title': 'History', 'materials': [{'index': 0}, {'index': 1}]}}, **kwargs)


def test_legal_choices_survive_bad_siblings_and_missing_explanation():
    result = assess({'selections': [None, {'arc_key': 'unknown', 'picks': [0]},
        {'arc_key': 'story', 'picks': [True, 1.0, -1, '0', 0, 99, 1]}]})
    assert result['choices'] == [{'arc_key': 'story', 'picks': [0, 1],
                                 'missing': 'Check the requested relationship or time condition.'}]


@pytest.mark.parametrize('result', [None, [], {'selections': 'bad'}, {'selections': []}])
def test_invalid_or_empty_never_forces_reads(result):
    assert assess(result) == {'choices': [], 'searches': []}


def test_support_quotes_and_sufficiency_are_not_read_permissions():
    assert assess({'sufficient': True, 'supports': [{'quote': 'rewritten'}],
                   'selections': [{'arc_key': 'story', 'picks': [0]}]})['choices']


def test_search_entity_still_grounded_and_quote_selected_by_host():
    result = assess({'searches': [
        {'anchor': 'a', 'entity': 'Cedar', 'query': 'Cedar location', 'gap': 'Location'},
        {'anchor': 'a', 'entity': 'Invented', 'query': 'Invented site', 'gap': 'Site'}]}, allow_search=True)
    assert len(result['searches']) == 1
    assert result['searches'][0]['anchor_quote'] == 'Mira joined Cedar.'


def test_review_uses_real_text_and_keeps_bridge_check(monkeypatch):
    events = []
    monkeypatch.setattr(delivery, 'observe', lambda stage, *args, **kw: events.append(stage))
    reads = [{'selection': 's0', 'gap': 'location', 'bridge_entity': 'Cedar',
              'anchor': {'text': 'Mira joined Cedar.'}, 'material': {'ref': 'b', 'text': 'Cedar moved to Rome.'}},
             {'selection': 's1', 'gap': 'location', 'bridge_entity': 'Cedar',
              'anchor': {'text': 'Mira joined Cedar.'}, 'material': {'ref': 'c', 'text': 'Other moved to Rome.'}}]
    result = arc_planning.review(lambda _: {'accepted': ['s0', 's1', 'unknown', True]}, 'Where?', None, [], reads)
    assert result == {'s0': {'anchor_quote': 'Mira joined Cedar.', 'quote': 'Cedar moved to Rome.'}}
    assert 'arc_review_missing_entity' in events
    assert 'arc_review_unknown_selection' in events


def test_engine_delivers_bridge_without_model_copied_quotes(planned, monkeypatch):
    def model(prompt):
        if 'READS:\n' in prompt:
            return {'accepted': [item['selection'] for item in json.loads(prompt.split('READS:\n')[1])]}
        decision = chooser(prompt)
        return {'selections': decision['selections']}
    monkeypatch.setattr(engine, '_model_json', model)
    rows = search()
    assert {row['id'] for row in rows} == {'anchor', 'bridge'}
    assert any('Valencia' in row['content'] for row in rows)
    assert search(user_id='another-user') == []


def test_engine_domain_exclusion_survives_simple_review(planned, monkeypatch):
    def model(prompt):
        if 'READS:\n' in prompt:
            assert 'EXCLUDED_BODY_ONLY' not in prompt
            return {'accepted': []}
        return {'selections': chooser(prompt, target='excluded')['selections']}
    monkeypatch.setattr(engine, '_model_json', model)
    assert all('EXCLUDED_BODY_ONLY' not in row['content'] for row in search())


def test_over_budget_material_is_not_sent_to_model(monkeypatch):
    monkeypatch.setattr(arc_planning, 'input_limit', lambda: 1500)
    reads = [{'selection': 's0', 'gap': 'date', 'anchor': {'text': 'A happened.'},
              'material': {'ref': 'b', 'text': 'x' * 3000}}]
    assert arc_planning.review(lambda _: pytest.fail('Oversized material was sent'),
                               'When?', None, [], reads) == {}


def test_review_format_failure_has_distinct_anonymous_status(monkeypatch):
    events = []
    monkeypatch.setattr(delivery, 'observe', lambda stage, *args, **kw: events.append(stage))
    def malformed(_):
        raise ValueError('synthetic invalid JSON')
    reads = [{'selection': 's0', 'gap': 'date', 'anchor': {'text': 'A happened.'},
              'material': {'ref': 'b', 'text': 'A happened in May.'}}]
    assert arc_planning.review(malformed, 'When?', None, [], reads) == {}
    assert 'arc_review_model_format_error' in events


def test_source_changed_after_simple_review_is_not_delivered(planned, monkeypatch):
    paths, _ = planned
    def model(prompt):
        if 'READS:\n' not in prompt:
            return {'selections': chooser(prompt)['selections']}
        reads = json.loads(prompt.split('READS:\n')[1])
        with Store(paths.database) as store:
            doc = store.read('bridge')
            store.revise('bridge', expected_revision=doc['revision'], title=doc['title'], body_md='Changed after review.')
        return {'accepted': [item['selection'] for item in reads]}
    monkeypatch.setattr(engine, '_model_json', model)
    assert [row['id'] for row in search()] == ['anchor']


@pytest.mark.parametrize('has_menu', [False, True])
def test_menu_instructions_only_when_available(has_menu):
    def model(prompt):
        assert ('Index 0 is authored narrative prose' in prompt) is has_menu
        assert ('No menu is available' in prompt) is not has_menu
        if not has_menu:
            assert '"picks":[0]' not in prompt
        return {'selections': [], 'searches': []}
    arc_planning.assess(model, 'Where?', None, [{'ref': 'a', 'text': 'Cedar moved.'}],
                        {'story': {'title': 'History', 'materials': [{'index': 0}]}} if has_menu else {},
                        allow_search=True)
