"""Strict group-scoped shared projection; review focus is a controlled fixture."""
import json
import pytest

from aml import delivery, engine, evidence
from serein.core.reader import Reader
from serein.core.store import Store
from test_delivery import tail_chain
from test_engine import memory

BODY = 'Lin Vale works for Oriole Atelier. Oriole Atelier operates in Valencia. Shared irrelevant sentence.'
EMPLOYMENT = 'Lin Vale works for Oriole Atelier.'
CITY = 'Oriole Atelier operates in Valencia.'


@pytest.fixture
def projection(tmp_path, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_DELIVERY_FIXES', '1')
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '0')
    db = tmp_path/'synthetic.sqlite'
    rows = [{'id': key, 'content': BODY} for key in ('b','a')]
    with Store(db) as store:
        for row in rows:
            store.create(row['id'], 'event', row['id'], BODY, manual_surface=True)
    sources = {key: {'source': 'shared-synthetic', 'text': BODY, 'speaker': 'user',
                     'message_time': '', 'time_origin': 'unknown'} for key in ('a','b')}
    monkeypatch.setattr(evidence, 'bound_sources', lambda _reader, ref: [sources[ref]])
    # Production-shaped reviewed support, explicitly fixture-authored, not inferred by code.
    group = {'ids': ['a','b'], 'route': 'entity_relation', 'focus': {'a': [EMPLOYMENT], 'b': [CITY]}}
    return db, rows, sources, group


def choose_b(units=('0','1')):
    return lambda _: {'selections': [{'ref': 'b', 'units': list(units)}]}


def select(reader, rows, group, model=choose_b(), cap=24000, direct=('a',)):
    return evidence.select(rows, reader, "In which city does Lin Vale's employer operate?", model, cap,
                           groups=[group], direct=set(direct), _read_refs=['a','b'], _allow_read=False)


def test_reviewed_shared_projection_one_physical_carrier_and_budget(projection):
    db, rows, _, group = projection
    with Reader(db) as reader:
        result = select(reader, rows, group)
        assert [row['id'] for row in result] == ['b']
        assert EMPLOYMENT in result[0]['content'] and CITY in result[0]['content']
        assert 'irrelevant' not in result[0]['content']
        assert delivery.finish(result, [group], {'a'}, result.carriers) == result
        cap = len(result[0]['content'])
        assert select(reader, rows, group, cap=cap) == result  # Charge the real b only, not virtual a.
        assert select(reader, rows, group, cap=cap-1) == []
        assert delivery.finish([], [group], {'a'}, result.carriers) == []


@pytest.mark.parametrize('units', [('1',), ('2',)])
def test_city_only_or_unrelated_common_sentence_cannot_satisfy_review(projection, units):
    db, rows, _, group = projection
    with Reader(db) as reader:
        assert select(reader, rows, group, choose_b(units)) == []


@pytest.mark.parametrize('focus', [None, {}, {'a': []}, {'a': [EMPLOYMENT]}, {'a': [EMPLOYMENT], 'b': []},
                                  {'a': [EMPLOYMENT], 'b': ['A fact not provided.']}])
def test_missing_empty_or_unoffered_review_is_not_proof(projection, focus):
    db, rows, _, group = projection
    group['focus'] = focus
    with Reader(db) as reader:
        assert select(reader, rows, group) == []


@pytest.mark.parametrize('change', ['different_source', 'different_text', 'different_speaker', 'different_time_origin'])
def test_source_id_text_and_provenance_must_all_match(projection, change):
    db, rows, sources, group = projection
    if change == 'different_source': sources['a']['source'] = 'other-synthetic'
    if change == 'different_text': sources['a']['text'] += ' Another side note.'
    if change == 'different_speaker': sources['a']['speaker'] = 'assistant'
    if change == 'different_time_origin': sources['a']['time_origin'] = 'source'
    with Reader(db) as reader:
        assert select(reader, rows, group) == []


def test_unselected_endpoint_changed_during_choice_invalidates_proof(projection):
    db, rows, _, group = projection
    def change_then_choose(_):
        with Store(db) as store:
            doc = store.read('a')
            store.revise('a', expected_revision=doc['revision'], title='a', body_md=BODY+' Changed.', metadata=doc['metadata'])
        return choose_b()('')
    with Reader(db) as reader:
        assert select(reader, rows, group, change_then_choose) == []


def test_post_selection_source_change_or_visibility_invalidates_proof(projection):
    db, rows, sources, group = projection
    with Reader(db) as reader:
        result = select(reader, rows, group)
        sources['a']['speaker'] = 'assistant'
        result.carriers.refresh()
        assert delivery.finish(result, [group], {'a'}, result.carriers) == []
        sources['a']['speaker'] = 'user'
        result = select(reader, rows, group)
        result.carriers.refresh(lambda ref: ref != 'a')
        assert delivery.finish(result, [group], {'a'}, result.carriers) == []


def test_review_proof_is_scoped_to_group_and_overlap_is_stable():
    first = {'ids': ['a','b']}
    other = {'ids': ['a','c']}
    carriers = delivery.Carriers({'b': {'b'}, 'c': {'c'}})
    carriers.proofs = [(first, {'a': {'b'}, 'b': {'b'}})]
    rows = [{'id': key, 'content': key} for key in ('b','c')]
    assert [row['id'] for row in delivery.finish(rows, [first, other], set(), carriers)] == ['b']
    assert delivery.finish(rows[1:], [first, other], set(), carriers) == []
    # An equivalent owner cannot borrow one group's proof for a different group object.
    assert delivery.finish(rows, [dict(first)], set(), carriers) == []


def test_independent_direct_retains_eligibility_without_group_proof(projection):
    db, rows, _, group = projection
    with Reader(db) as reader:
        result = select(reader, rows, group, choose_b(('1',)), direct=('a','b'))
        assert [row['id'] for row in delivery.finish(result, [group], {'a','b'}, result.carriers)] == ['b']


def test_allocate_incomplete_audit_has_only_fixed_codes_and_random_refs(monkeypatch):
    events = []
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT', '1')
    monkeypatch.setattr(delivery, '_emit', events.append)
    with delivery.request():
        assert delivery.allocate([{'id':'private-b','content':'PRIVATE_FACT'}],
                                 [{'ids':['private-a','private-b']}], {'private-a'}, 24000) == []
    assert any(event.get('reason') == 'INCOMPLETE_GROUP' for event in events)
    assert 'private-' not in json.dumps(events) and 'PRIVATE_FACT' not in json.dumps(events)
    assert all(set(event) <= {'request','material','stage','reason','count','length'} for event in events)


@pytest.fixture
def engine_projection(tail_chain, monkeypatch):
    paths, hits, group = tail_chain
    with Store(paths.database) as store:
        for ref in ('a','b'):
            doc = store.read(ref)
            store.revise(ref, expected_revision=doc['revision'], title=ref, body_md=BODY, metadata=doc['metadata'])
    with Reader(paths.database) as reader:
        for ref in ('a','b'):
            hits[ref]['document'] = reader.read(ref)['document']
            hits[ref]['content'] = BODY
    group['focus'] = {'a': [EMPLOYMENT], 'b': [CITY]}
    monkeypatch.setattr(evidence, 'bound_sources', lambda *_: [{'source':'shared-synthetic','text':BODY,
                          'speaker':'user','message_time':'','time_origin':'unknown'}])
    monkeypatch.setattr(engine, '_model_json', choose_b())
    return paths, hits, group


def test_real_engine_single_carrier_and_topk_remains_physical(engine_projection):
    found = engine.search_memory(query="In which city does Lin Vale's employer operate?", options=None,
                                 user_id='synthetic-user', top_k=2)
    assert [row['id'] for row in found] == ['b']
    assert EMPLOYMENT in found[0]['content'] and CITY in found[0]['content']


def test_engine_unselected_narrative_endpoint_filter_invalidates_proof(engine_projection, monkeypatch):
    _, hits, _ = engine_projection
    actual = evidence.select
    def select_then_filter(*args, **kwargs):
        result = actual(*args, **kwargs)
        hits['a']['kind'] = 'narrative'
        return result
    monkeypatch.setattr(evidence, 'select', select_then_filter)
    monkeypatch.setattr(engine.narratives, 'readable_for_search', lambda *_: False)
    assert engine.search_memory(query='device', options=None, user_id='synthetic-user', top_k=2) == []


def test_engine_post_selection_revision_change_invalidates_proof(engine_projection, monkeypatch):
    paths, _, _ = engine_projection
    actual = evidence.select
    def select_then_change(*args, **kwargs):
        result = actual(*args, **kwargs)
        with Store(paths.database) as store:
            doc = store.read('b')
            store.revise('b', expected_revision=doc['revision'], title='b', body_md=BODY+' Changed.', metadata=doc['metadata'])
        return result
    monkeypatch.setattr(evidence, 'select', select_then_change)
    assert engine.search_memory(query='device', options=None, user_id='synthetic-user', top_k=2) == []


def test_invalid_physical_proof_carrier_cannot_survive_as_direct(engine_projection, monkeypatch):
    paths, hits, _ = engine_projection
    monkeypatch.setattr(engine, '_recall_hits', lambda *_a, **_k: list(hits.values()))
    actual = evidence.select
    def select_then_change(*args, **kwargs):
        result = actual(*args, **kwargs)
        with Store(paths.database) as store:
            doc = store.read('b')
            store.revise('b', expected_revision=doc['revision'], title='b', body_md=BODY+' Changed.', metadata=doc['metadata'])
        return result
    monkeypatch.setattr(evidence, 'select', select_then_change)
    assert engine.search_memory(query='device', options=None, user_id='synthetic-user', top_k=2) == []
