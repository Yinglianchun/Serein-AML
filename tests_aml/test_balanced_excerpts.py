"""Bounded literal delivery without another model call or unit-ID protocol."""
import pytest
from aml import bridges, engine
from serein.core.store import Store
from test_engine import memory, add, search


def test_head_tail_and_query_matching_middle_survive():
    body = ('Old city: Paris.\n' + 'Unrelated material.\n'*200 +
            'The branch closure caused the transfer to Rome.\n' +
            'Unrelated material.\n'*300 + 'Current city: Rome.')
    result = bridges.balanced_excerpt(body, 1000, query='Why did the branch closure cause a transfer?')
    assert 'Old city: Paris.' in result
    assert 'Current city: Rome.' in result
    assert 'branch closure caused the transfer' in result
    assert '[…]' in result and len(result) <= 1000
    for piece in result.split('\n[…]\n'):
        assert piece in body


def test_all_reviewed_quotes_fit_and_keep_source_order():
    body = 'lead '*200 + 'Cause A.' + ' gap '*300 + 'Result B.' + ' tail'*200
    result = bridges.balanced_excerpt(body, 500, ['Result B.', 'Cause A.'])
    assert result.index('Cause A.') < result.index('Result B.')
    assert len(result) <= 500


def test_exact_quote_reservation_does_not_lose_quote_to_markers():
    body = 'lead '*200 + 'Important fact.' + ' tail'*200
    assert bridges.balanced_excerpt(body, len('Important fact.'), ['Important fact.']) == 'Important fact.'


@pytest.mark.parametrize('cap', [0, 1, 10, 11, 20, 50, 99, 1000])
def test_unicode_budget_and_no_invented_text(cap):
    body = '先前住在巴黎。\n'*100 + '由于分部关闭，调往罗马。\n' + '目前住在罗马。'*100
    result = bridges.balanced_excerpt(body, cap, query='为何调往罗马')
    assert len(result) <= cap
    for piece in result.split('\n[…]\n'):
        assert piece in body


def test_short_material_unchanged():
    assert bridges.balanced_excerpt('Known fact.', 100) == 'Known fact.'


def test_real_search_preserves_tail_with_opt_in_only(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '0')
    monkeypatch.setenv('SEREIN_AML_DELIVERY_FIXES', '0')
    for flag in ('_EXPAND_ARCS', '_EXPAND_ENTITIES', '_EXPAND_GAPS'):
        monkeypatch.setattr(engine, flag, False)
    add()
    with Store(engine._paths('user-a').database) as store:
        ref = store.conn.execute("SELECT id FROM documents WHERE kind='event'").fetchone()[0]
        doc = store.read(ref)
        body = 'Alice lived in Paris. '+ 'Old details. '*2500 + 'Current city: Rome.'
        store.revise(ref, expected_revision=doc['revision'], title=doc['title'], body_md=body)
        current = store.read(ref)
    monkeypatch.setattr(engine, '_recall_hits', lambda *a, **k: [{'id':ref, 'kind':'event', 'document':current}])
    monkeypatch.setattr(engine.originals, 'hits', lambda *a, **k: [])
    monkeypatch.setattr(engine, '_model_json', lambda *a: pytest.fail('no additional model call'))
    monkeypatch.setenv('SEREIN_AML_BALANCED_EXCERPTS', '0')
    baseline = search()
    assert 'Current city: Rome.' not in baseline[0]['content']
    monkeypatch.setenv('SEREIN_AML_BALANCED_EXCERPTS', '1')
    result = search()
    assert 'Current city: Rome.' in result[0]['content']
    assert result[0]['id'] == baseline[0]['id']
    assert set(result[0]) == {'id','content','score','created_at'}
    assert sum(len(row['content']) for row in result) <= engine._CONTEXT_CHAR_CAP
