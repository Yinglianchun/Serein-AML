import json

import pytest

from aml import bridges, delivery, engine, evidence
from serein.core.reader import Reader
from serein.core.store import Store
from serein.recall.index import build_index
from test_engine import memory


def pick_all(prompt):
    packet = json.loads(prompt.split('\nEVIDENCE: ', 1)[1])
    return {'selections': [{'ref': row['ref'], 'units': [u['id'] for u in row['units'][:12]]}
                           for row in packet]}


@pytest.fixture
def records(tmp_path, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_DELIVERY_FIXES', '1')
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '0')
    db = tmp_path/'synthetic.sqlite'
    rows = [{'id': 'a', 'content': 'Alice owns AsterBridge. '+('x'*750)+'.'},
            {'id': 'b', 'content': 'AsterBridge comes from CopperBay. '+('y'*250)+'.'}]
    with Store(db) as store:
        for row in rows:
            store.create(row['id'], 'event', row['id'], row['content'], manual_surface=True)
    return db, rows


@pytest.mark.parametrize('unit', [0, '0'])
def test_unit_id_compatibility(records, unit):
    db, rows = records
    with Reader(db) as reader:
        result = evidence.select(rows, reader, 'device', lambda _: {'selections': [{'ref': 'a', 'units': [unit]}]}, 24000)
    assert len(result) == 1 and 'Alice owns AsterBridge' in result[0]['content']


@pytest.mark.parametrize('units,reason', [([True], 'INVALID_UNIT_ID_TYPE'), ([False], 'INVALID_UNIT_ID_TYPE'),
    ([0.0], 'INVALID_UNIT_ID_TYPE'), ([-1], 'INVALID_UNIT_ID_TYPE'), ([0, '0'], 'INVALID_UNIT_ID'),
    (['0', 'unknown'], 'INVALID_UNIT_ID')])
def test_invalid_ids_are_explicit_and_never_fail_open(records, units, reason):
    db, rows = records
    with Reader(db) as reader, pytest.raises(delivery.DeliveryError, match=reason):
        evidence.select(rows, reader, 'device', lambda _: {'selections': [{'ref': 'a', 'units': units}]}, 24000)


@pytest.mark.parametrize('response', [{}, {'selections': 'bad'}, [], {'selections': [None]}])
def test_format_errors_are_not_no_match(records, response):
    db, rows = records
    with Reader(db) as reader, pytest.raises(delivery.DeliveryError, match='MODEL_FORMAT_ERROR'):
        evidence.select(rows, reader, 'device', lambda _: response, 24000)


def test_model_call_error_and_no_match_are_distinct(records):
    db, rows = records
    with Reader(db) as reader:
        assert evidence.select(rows, reader, 'device', lambda _: {'selections': []}, 24000) == []
        def failed(_):
            raise RuntimeError('CONTENT_THAT_MUST_NOT_APPEAR')
        with pytest.raises(delivery.DeliveryError) as exc:
            evidence.select(rows, reader, 'device', failed, 24000)
        assert str(exc.value) == 'MODEL_CALL_ERROR'


def test_budget_keeps_complete_group_or_only_independent_direct_hit(records):
    db, rows = records
    groups = [{'ids': ['a', 'b']}]
    with Reader(db) as reader:
        full = evidence.select(rows, reader, 'device', pick_all, 24000, groups=groups, direct={'a'})
        small = evidence.select(rows, reader, 'device', pick_all, 1000, groups=groups, direct={'a'})
    assert {r['id'] for r in full} == {'a', 'b'}
    assert [r['id'] for r in small] == ['a']
    assert sum(len(r['content']) for r in small) <= 1000
    assert [r['id'] for r in delivery.finish(small, groups, {'a'}, small.carriers)] == ['a']


def test_topk_cannot_reintroduce_orphan_expansion():
    rows = {key: {'id': key} for key in ('a', 'b', 'c')}
    group = {'ids': ['a', 'b'], 'route': 'entity_relation'}
    assert [r['id'] for r in bridges.select(rows, ['b', 'a'], [group], 1, strict=True, direct={'a'})] == ['a']
    assert [r['id'] for r in bridges.select(rows, ['b', 'a'], [group], 2, strict=True, direct={'a'})] == ['a', 'b']
    # A direct hit keeps its independent eligibility even if it belongs to a group.
    assert [r['id'] for r in bridges.select(rows, ['b', 'a'], [group], 1, strict=True, direct={'a', 'b'})] == ['b']


def test_missing_carrier_and_overlapping_groups_cannot_claim_delivery():
    rows = [{'id': key, 'content': key} for key in ('b', 'c', 'd')]
    groups = [{'ids': ['a', 'b']}, {'ids': ['c', 'd']}]
    # a aliases c, while c aliases b. Removing b must invalidate the second group too.
    carriers = {'a': {'c'}, 'b': {'missing'}, 'c': {'b'}, 'd': {'d'}}
    assert delivery.finish(rows, groups, set(), carriers) == []
    assert [r['id'] for r in delivery.finish(rows, groups, {'b'}, carriers)] == ['b', 'c', 'd']


def test_group_duplicates_keep_real_record_carriers(records, monkeypatch):
    db, rows = records
    source = {'source': 'shared', 'text': 'Shared irrelevant sentence. Alice owns AsterBridge. AsterBridge comes from CopperBay.',
              'speaker': 'user', 'message_time': '', 'time_origin': 'unknown'}
    monkeypatch.setattr(evidence, 'bound_sources', lambda *_: [source])
    groups = [{'ids': ['a', 'b']}]
    with Reader(db) as reader:
        result = evidence.select(rows, reader, 'device', pick_all, 24000, groups=groups,
                                 direct={'a'}, _read_refs=['a', 'b'])
    assert {r['id'] for r in result} == {'a', 'b'}
    assert result.carriers == {'a': {'a'}, 'b': {'b'}}
    # A common unrelated sentence is never used to alias one relation endpoint to the other.
    assert delivery.finish([result[1]], groups, set(), result.carriers) == []


@pytest.fixture
def tail_chain(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_DELIVERY_FIXES', '1')
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '0')
    monkeypatch.setattr(engine, '_EXPAND_ARCS', False)
    monkeypatch.setattr(engine, '_EXPAND_GAPS', False)
    monkeypatch.setattr(engine, '_EXPAND_ENTITIES', True)
    paths = engine._paths('synthetic-user')
    with Store(paths.database) as store:
        store.create('a', 'event', 'device', 'Alice owns AsterBridge. '+('x'*750)+'.', manual_surface=True)
        store.create('b', 'event', 'factory', 'AsterBridge comes from CopperBay. '+('y'*250)+'.', manual_surface=True)
    build_index(paths.database, paths.index)
    with Reader(paths.database) as reader:
        hits = {key: {'id': key, 'kind': 'event', 'document': reader.read(key)['document'],
                      'content': reader.read(key)['document']['body_md']} for key in ('a', 'b')}
    group = {'ids': ['a', 'b'], 'route': 'entity_relation', 'plan': {'anchor': 'a', 'anchor_stamp': 'synthetic'}}
    monkeypatch.setattr(engine, '_recall_hits', lambda *_a, **_k: [hits['a']])
    monkeypatch.setattr(engine.originals, 'hits', lambda *_a, **_k: [])
    monkeypatch.setattr(engine, '_bridge_candidate', lambda *_: ([hits['b']], [group]))
    monkeypatch.setattr(bridges, 'grounded_entities', lambda *_: ([], 'synthetic'))
    monkeypatch.setattr(bridges, 'matching_quote', lambda *_: 'synthetic relationship')
    monkeypatch.setattr(engine, '_rank_scores', lambda *_: {'a': .1, 'b': 1})
    monkeypatch.setattr(engine, '_model_json', pick_all)
    return paths, hits, group


def search(k=2):
    return engine.search_memory(query='Where is the device from?', options=None, user_id='synthetic-user', top_k=k)


def test_actual_engine_topk_and_budget_tail_chain(tail_chain, monkeypatch):
    assert {r['id'] for r in search()} == {'a', 'b'}
    assert [r['id'] for r in search(1)] == ['a']
    monkeypatch.setattr(engine, '_CONTEXT_CHAR_CAP', 1000)
    assert [r['id'] for r in search()] == ['a']


def test_linked_reference_not_offered_is_still_rejected(records):
    db, rows = records
    def invent_linked_selection(prompt):
        packet = json.loads(prompt.split('\nEVIDENCE: ', 1)[1])
        assert [row['ref'] for row in packet] == ['a']
        assert packet[0]['linked_refs'] == ['b']
        return {'selections': [{'ref': 'a', 'units': ['0']}, {'ref': 'b', 'units': ['0']}]}
    with Reader(db) as reader, pytest.raises(delivery.DeliveryError, match='INVALID_SELECTION'):
        evidence.select(rows[:1], reader, 'device', invent_linked_selection, 24000,
                        groups=[{'ids': ['a', 'b']}], direct={'a'}, _allow_read=False)


def test_actual_engine_shared_source_tail_chain(tail_chain, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '1')
    source = {'source': 'shared', 'text': 'Alice owns AsterBridge. AsterBridge comes from CopperBay.',
              'speaker': 'user', 'message_time': '', 'time_origin': 'unknown'}
    monkeypatch.setattr(evidence, 'bound_sources', lambda *_: [source])
    def request_then_choose(prompt):
        if 'Original reading is now finished.' not in prompt:
            return {'read_sources': [{'ref': 'a', 'gap': 'device'}, {'ref': 'b', 'gap': 'factory'}]}
        result = pick_all(prompt)
        result['selections'].reverse()
        return result
    monkeypatch.setattr(engine, '_model_json', request_then_choose)
    assert {r['id'] for r in search()} == {'a', 'b'}


def test_independent_switch_does_not_enable_time_or_source_requests(tail_chain, monkeypatch):
    monkeypatch.setattr(evidence, 'time_candidates', lambda *_: pytest.fail('time feature must remain off'))
    seen = []
    def model(prompt):
        seen.append(prompt)
        packet = json.loads(prompt.split('\nEVIDENCE: ', 1)[1])
        assert all(not row['can_read_sources'] for row in packet)
        assert all(not unit['date_notes'] for row in packet for unit in row['units'])
        return pick_all(prompt)
    monkeypatch.setattr(engine, '_model_json', model)
    assert search() and len(seen) == 1


def test_defaults_preserve_baseline_even_for_orphan_topk(tail_chain, monkeypatch):
    monkeypatch.delenv('SEREIN_AML_DELIVERY_FIXES')
    monkeypatch.setattr(evidence, 'select', lambda *_a, **_k: pytest.fail('default selector must stay unchanged'))
    assert [r['id'] for r in search(1)] == ['b']


def test_audit_is_content_free_request_local_and_default_off(tail_chain, monkeypatch):
    events = []
    monkeypatch.setattr(delivery, '_emit', events.append)
    monkeypatch.delenv('SEREIN_AML_DELIVERY_AUDIT', raising=False)
    assert search() and not events
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT', '1')
    assert search()
    first = list(events)
    events.clear()
    assert search()
    assert {e['request'] for e in first}.isdisjoint(e['request'] for e in events)
    assert {e['material'] for e in first if 'material' in e}.isdisjoint(e['material'] for e in events if 'material' in e)
    assert {'found', 'provided', 'selected', 'accepted', 'final', 'final_count'} <= {e['stage'] for e in first}
    assert all(set(e) <= {'request', 'material', 'stage', 'reason', 'count', 'length'} for e in first)
    text = json.dumps(first)
    assert 'AsterBridge' not in text and 'device' not in text and 'synthetic-user' not in text
    assert not delivery._audit.get()


def test_empty_mapping_cannot_recreate_group_support():
    rows = [{'id': 'a'}, {'id': 'b'}]
    assert delivery.finish(rows, [{'ids': ['a', 'b']}], set(), {}) == []


@pytest.mark.parametrize('reason', ['MODEL_FORMAT_ERROR', 'MODEL_CALL_ERROR', 'INVALID_UNIT_ID_TYPE'])
def test_http_delivery_failure_returns_safe_code_not_empty_success(monkeypatch, caplog, reason):
    import aml.app as api
    from fastapi.testclient import TestClient
    def fail(**kwargs):
        raise delivery.DeliveryError(reason)
    monkeypatch.setattr(api, 'search_memory', fail)
    response = TestClient(api.app).post('/search', json={
        'query': 'PRIVATE_QUERY', 'options': ['PRIVATE_OPTIONS'], 'user_id': 'PRIVATE_USER', 'top_k': 2})
    assert response.status_code == 503
    assert response.json() == {'detail': {'reason': reason}}
    assert 'PRIVATE' not in response.text + caplog.text


def test_delivery_error_reason_is_closed_vocabulary():
    assert str(delivery.DeliveryError('PRIVATE_CONTENT')) == 'DELIVERY_ERROR'


def test_audit_rejections_budget_and_group_are_visible(tail_chain, monkeypatch):
    events = []
    monkeypatch.setattr(delivery, '_emit', events.append)
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT', '1')
    monkeypatch.setattr(engine, '_CONTEXT_CHAR_CAP', 1000)
    assert [r['id'] for r in search()] == ['a']
    assert any(e.get('reason') == 'OUTPUT_BUDGET' for e in events)
    assert any(e['stage'] == 'group' for e in events)
    assert all(e.get('material') not in {'a', 'b'} for e in events)
    events.clear()
    monkeypatch.setattr(engine, '_model_json', lambda _: {'selections': 'PRIVATE_BAD_FORMAT'})
    with pytest.raises(delivery.DeliveryError, match='MODEL_FORMAT_ERROR'):
        search()
    assert any(e.get('reason') == 'MODEL_FORMAT_ERROR' for e in events)
    assert 'PRIVATE' not in json.dumps(events)
    assert delivery._audit.get() is None


def test_actual_engine_carrier_removed_after_selection_does_not_survive_group(tail_chain, monkeypatch):
    _, hits, _ = tail_chain
    def choose_then_invalidate(prompt):
        result = pick_all(prompt)
        # Exercise the post-selection narrative visibility gate after initial admission.
        hits['b']['kind'] = 'narrative'
        return result
    monkeypatch.setattr(engine, '_model_json', choose_then_invalidate)
    monkeypatch.setattr(engine.narratives, 'readable_for_search', lambda *_: False)
    assert [r['id'] for r in search()] == ['a']


def test_overlapping_groups_keep_complete_group_and_independent_direct():
    rows = [{'id': key, 'content': 'selected evidence '+key} for key in ('a', 'b', 'c')]
    groups = [{'ids': ['a', 'b'], 'route': 'arc_menu'}, {'ids': ['b', 'c'], 'route': 'entity_relation'}]
    limited = delivery.allocate(rows, groups, {'a'}, sum(len(r['content']) for r in rows[:2]))
    assert [r['id'] for r in delivery.finish(limited, groups, {'a'})] == ['a', 'b']
    assert [r['id'] for r in delivery.finish([rows[1]], groups, {'b'})] == ['b']


def test_delivery_switch_does_not_change_upstream_model_prompts(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '0')
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES', '4000')
    received = []
    def create(**kwargs):
        received.append(kwargs['input'])
        return SimpleNamespace(output_text='{}')
    client = SimpleNamespace(base_url=SimpleNamespace(host='synthetic'),
                             responses=SimpleNamespace(create=create))
    monkeypatch.setattr(engine, '_client', lambda: nullcontext(client))
    prompt = 'password: synthetic-secret\n' + 'x' * 5000
    token = engine.runtime.ACTIVE_DATABASE.set(None)
    try:
        for flag in ('0', '1'):
            monkeypatch.setenv('SEREIN_AML_DELIVERY_FIXES', flag)
            assert engine._model_json(prompt) == {}
    finally:
        engine.runtime.ACTIVE_DATABASE.reset(token)
    assert received == [prompt, prompt]


def test_final_delivery_input_still_bounded(records, monkeypatch):
    db, rows = records
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES', '4000')
    with Reader(db) as reader, pytest.raises(delivery.DeliveryError, match='INPUT_BUDGET'):
        evidence.select(rows, reader, 'x' * 5000,
                        lambda _: pytest.fail('over-budget selector must not call model'), 24000)


def test_audit_reaches_real_warning_sink_only_when_enabled(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.delenv('SEREIN_AML_DELIVERY_AUDIT', raising=False)
    with delivery.request():
        delivery.observe('final', 'private-real-id', count=1, length=12)
    assert not caplog.records
    monkeypatch.setenv('SEREIN_AML_DELIVERY_AUDIT', '1')
    with delivery.request():
        delivery.observe('final', 'private-real-id', count=1, length=12)
    assert len(caplog.records) == 1
    assert 'aml_delivery' in caplog.text and 'private-real-id' not in caplog.text
    assert json.loads(caplog.records[0].getMessage().split(' ', 1)[1])['stage'] == 'final'
