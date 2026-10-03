"""Relation-conditioned retrieval keeps literal evidence, without a planner LLM."""
import pytest

from aml import bridges, engine, originals
from serein.adapters.reranker import RerankerClient
from serein.core.reader import Reader
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index, refresh_index
from serein.tagging_entities import snapshot, validate


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, '_DATA_ROOT', tmp_path)
    monkeypatch.setattr(engine, '_EXPAND_ARCS', False)
    monkeypatch.setattr(engine, '_EXPAND_ENTITIES', True)
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: {'queries': ['Dana'], 'entities': ['Dana']})
    monkeypatch.setattr(engine, '_model_json', lambda *_: pytest.fail('Entity round must not call a planner LLM'))
    paths = engine._paths('alice')
    with Store(paths.database) as store:
        store.create('anchor', 'event', 'Dana employer', 'Dana joined Morningstar Studio in 2023.', manual_surface=True)
        store.create('bridge', 'event', 'Studio move',
                     'Morningstar Studio moved from Hangzhou to Suzhou in 2024.', manual_surface=True)
        store.create('noise', 'event', 'Studio awards', 'Morningstar Studio won a design award.', manual_surface=True)
    build_index(paths.database, paths.index)
    save_settings(paths.database, {'models': [{'id': 'rank', 'model': 'synthetic',
        'base_url': 'https://mock.invalid/v1', 'protocol': 'openai', 'api_key': 'synthetic'}],
        'assignments': {'reranker': 'rank'}})
    observed = {'queries': []}
    def rank(self, query, documents):
        observed['queries'].append(query)
        return {row['ref']: (0.01 if query.startswith('Where') and row['ref'] == 'bridge' else .9)
                for row in documents}
    monkeypatch.setattr(RerankerClient, '__call__', rank)
    return paths, observed


def search(**changes):
    return engine.search_memory(**{'query': 'Where does Dana work now?', 'options': None,
                                   'user_id': 'alice', 'top_k': 2, **changes})


def test_one_round_finds_relation_and_retains_both_low_similarity_evidence(memory, monkeypatch):
    _, observed = memory
    paths_seen = []
    select = bridges.select
    def capture(records, ordered, groups, limit):
        paths_seen.extend(groups)
        return select(records, ordered, groups, limit)
    monkeypatch.setattr(bridges, 'select', capture)
    rows = search()
    assert {row['id'] for row in rows} == {'anchor', 'bridge'}
    assert any('Suzhou in 2024' in row['content'] for row in rows)
    assert any(query.startswith('Morningstar Studio location') and 'now' in query for query in observed['queries'])
    assert 'Where does Dana work now?' in observed['queries']
    group = next(group for group in paths_seen if group['route'] == 'entity_relation')
    assert group['ids'] == ('anchor', 'bridge')
    assert group['plan']['time_conditions'] == ['now']
    assert group['plan']['entity_supports'][0]['kind'] == 'memory_body'
    assert group['plan']['entity_supports'][0]['quote'] == 'Dana joined Morningstar Studio in 2023.'
    assert group['target_quote'] == 'Morningstar Studio moved from Hangzhou to Suzhou in 2024.'
    assert len(search(top_k=1)) <= 1
    assert search(user_id='bob') == []


def test_shared_name_without_requested_relationship_is_not_a_bridge(memory):
    paths, _ = memory
    with Store(paths.database) as store:
        doc = store.read('bridge')
        store.revise('bridge', expected_revision=doc['revision'], title=doc['title'],
                     body_md='Morningstar Studio won a design award.')
    refresh_index(paths.database, paths.index, ['bridge'])
    assert [row['id'] for row in search()] == ['anchor']


def test_entity_round_is_opt_in(memory, monkeypatch):
    monkeypatch.setattr(engine, '_EXPAND_ENTITIES', False)
    assert [row['id'] for row in search()] == ['anchor']


def test_open_ended_question_declines_rule_expansion(memory):
    assert [row['id'] for row in search(query='What made Dana change her mind?')] == ['anchor']


@pytest.mark.parametrize('rule', ['excluded', 'explicit_only'])
def test_entity_round_keeps_original_question_domain_admission(memory, rule):
    paths, _ = memory
    with Store(paths.database) as store:
        doc = store.read('bridge')
        store.revise('bridge', expected_revision=doc['revision'], title=doc['title'],
                     body_md=doc['body_md'], metadata={'domain': 'work'})
    refresh_index(paths.database, paths.index, ['bridge'])
    save_settings(paths.database, {'recall': {'domains': {'work': rule}}})
    assert [row['id'] for row in search()] == ['anchor']


def test_changed_anchor_source_invalidates_expansion_only_bridge(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        source = store.add_source('original', 'Dana joined Morningstar Studio in 2023.')
        binding = store.bind('anchor', source)
    original = RerankerClient.__call__
    def unbind_during_final_rank(self, query, documents):
        if query.startswith('Where'):
            with Store(paths.database) as store:
                store.unbind(binding)
        return original(self, query, documents)
    monkeypatch.setattr(RerankerClient, '__call__', unbind_during_final_rank)
    assert [row['id'] for row in search()] == ['anchor']


def test_fallback_ignores_unrelated_source_names_and_never_merges_aliases(memory):
    paths, _ = memory
    with Store(paths.database) as store:
        source = store.add_source('original', 'Dana joined Morningstar Studio in 2023. Unrelated Acme Factory is based in London.')
        store.bind('anchor', source)
        doc = store.read('anchor')
        materials, stamp = snapshot(store, doc)
        entities, _ = validate([{'name': 'Morningstar Studio', 'type': 'organization', 'aliases': ['Acme Factory'],
            'supports': [{'source_id': source, 'quote': 'Dana joined Morningstar Studio in 2023.'}]}], materials)
        store.revise('anchor', expected_revision=doc['revision'], title=doc['title'], body_md=doc['body_md'],
            metadata={'tagged_entities': entities, 'entity_extraction_version': 1, 'entity_input_hash': stamp})
    with Reader(paths.database) as reader:
        doc = reader.read('anchor')['document']
        names, _ = bridges.grounded_entities(reader, {'id': 'anchor', 'kind': 'event', 'document': doc})
    assert 'Morningstar Studio' in {item['name'] for item in names}
    assert 'Acme Factory' not in {item['name'] for item in names}
    assert all(support['kind'] == 'bound_source' for item in names for support in item['supports'])


def test_chinese_entity_plus_location_from_unannotated_public_body(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        for key, body in [('anchor', '小林加入了晨星工作室。'), ('bridge', '晨星工作室后来从杭州搬到了苏州。')]:
            doc = store.read(key)
            store.revise(key, expected_revision=doc['revision'], title=key, body_md=body)
    refresh_index(paths.database, paths.index, ['anchor', 'bridge'])
    monkeypatch.setattr(engine, '_rewrite_query', lambda *_: {'queries': ['小林'], 'entities': ['小林']})
    assert {row['id'] for row in search(query='小林现在在哪座城市工作？')} == {'anchor', 'bridge'}


def test_one_added_candidate_does_not_start_another_hop(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        doc = store.read('bridge')
        store.revise('bridge', expected_revision=doc['revision'], title=doc['title'],
                     body_md=doc['body_md'] + ' Delta Project is based there.')
        store.create('third', 'event', 'Another project', 'Delta Project is based in Rome.', manual_surface=True)
    refresh_index(paths.database, paths.index, ['bridge', 'third'])
    queries = []
    recall = engine._recall_hits
    def capture(services, query, **kwargs):
        queries.append(query)
        return recall(services, query, **kwargs)
    monkeypatch.setattr(engine, '_recall_hits', capture)
    assert {row['id'] for row in search(top_k=100)} == {'anchor', 'bridge'}
    assert not any('Delta Project' in query for query in queries)


def test_long_bridge_excerpt_keeps_exact_relation_quote(memory, monkeypatch):
    paths, _ = memory
    quote = 'Morningstar Studio moved from Hangzhou to Suzhou in 2024.'
    with Store(paths.database) as store:
        doc = store.read('bridge')
        store.revise('bridge', expected_revision=doc['revision'], title=doc['title'],
                     body_md=('Unrelated notes.\n' * 500) + quote + ('\nUnrelated notes.' * 500))
    refresh_index(paths.database, paths.index, ['bridge'])
    monkeypatch.setattr(engine, '_CONTEXT_CHAR_CAP', 1000)
    rows = search()
    assert len(rows) == 2 and sum(len(row['content']) for row in rows) <= 1000
    assert quote in next(row['content'] for row in rows if row['id'] == 'bridge')


def test_old_evidence_is_not_suppressed_when_new_evidence_is_expanded(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        store.create('old', 'event', 'Dana former location',
                     'In 2023 Dana worked at Morningstar Studio, based in Hangzhou.', manual_surface=True)
    refresh_index(paths.database, paths.index, ['old'])
    rows = search(top_k=3)
    assert {'old', 'anchor', 'bridge'} == {row['id'] for row in rows}


def test_pending_original_name_has_literal_raw_provenance(memory):
    paths, _ = memory
    hit = {'id': 'raw:1', 'kind': 'original', 'content': 'Dana joined Morningstar Studio in 2023.'}
    with Reader(paths.database) as reader:
        rows, _ = bridges.grounded_entities(reader, hit)
    studio = next(row for row in rows if row['name'] == 'Morningstar Studio')
    assert studio['supports'][0]['source_id'] == 'raw:1'
    assert studio['supports'][0]['kind'] == 'raw_original'


def test_tagged_source_only_name_cannot_form_a_chain_absent_from_returned_anchor(memory):
    paths, _ = memory
    with Store(paths.database) as store:
        doc = store.read('anchor')
        store.revise('anchor', expected_revision=doc['revision'], title=doc['title'],
                     body_md='Dana joined a studio in 2023.')
        source = store.add_source('source', 'Dana joined Morningstar Studio in 2023.')
        store.bind('anchor', source)
        doc = store.read('anchor')
        materials, stamp = snapshot(store, doc)
        entities, _ = validate([{'name': 'Morningstar Studio', 'type': 'organization',
            'supports': [{'source_id': source, 'quote': 'Dana joined Morningstar Studio in 2023.'}]}], materials)
        store.revise('anchor', expected_revision=doc['revision'], title=doc['title'], body_md=doc['body_md'],
            metadata={'tagged_entities': entities, 'entity_extraction_version': 1, 'entity_input_hash': stamp})
    refresh_index(paths.database, paths.index, ['anchor'])
    assert [row['id'] for row in search()] == ['anchor']


def test_entity_pair_quote_budget_survives_many_longer_noise_records(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        for index in range(45):
            store.create(f'n{index:02d}', 'event', 'Dana notes', 'Dana kept general notes.\n' * 50, manual_surface=True)
    refresh_index(paths.database, paths.index, [f'n{index:02d}' for index in range(45)])
    original_recall = engine._recall_hits
    def anchor_first(services, query, **kwargs):
        return sorted(original_recall(services, query, **kwargs), key=lambda row: row['id'] != 'anchor')
    monkeypatch.setattr(engine, '_recall_hits', anchor_first)
    monkeypatch.setattr(engine, '_CONTEXT_CHAR_CAP', 1000)
    rows = search(top_k=100)
    assert len(rows) == 40 and sum(len(row['content']) for row in rows) <= 1000
    assert 'Dana joined Morningstar Studio in 2023.' in next(row['content'] for row in rows if row['id'] == 'anchor')
    assert 'Morningstar Studio moved from Hangzhou to Suzhou in 2024.' in next(row['content'] for row in rows if row['id'] == 'bridge')


def test_entity_round_tries_multiple_starts_instead_of_only_first(memory, monkeypatch):
    paths, _ = memory
    with Store(paths.database) as store:
        store.create('first', 'event', 'Dana hobby', 'Dana liked Cobalt Project.', manual_surface=True)
        store.create('wrong_bridge', 'event', 'Project location', 'Cobalt Project is based in Rome.', manual_surface=True)
    refresh_index(paths.database, paths.index, ['first', 'wrong_bridge'])
    original_recall = engine._recall_hits
    def first_unhelpful(services, query, **kwargs):
        return sorted(original_recall(services, query, **kwargs), key=lambda row: row['id'] != 'first')
    monkeypatch.setattr(engine, '_recall_hits', first_unhelpful)
    assert {row['id'] for row in search()} == {'anchor', 'bridge'}
