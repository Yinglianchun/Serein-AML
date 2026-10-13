"""Bounded rereads of existing candidates; no external model calls."""
import json

import pytest

from aml import engine
from serein.config import Settings
from serein.core.store import Store
from tests_aml.test_arc_planning import planned, reviewer, search
from tests_aml.test_gap_search import gap_memory, decision


@pytest.mark.parametrize('body_size,cap,expected,truncated', [
    (1500, 24000, 12000, False),
    (3000, 24000, 16000, True),
    (1500, 6000, 6000, True),
])
def test_planning_body_budget_keeps_eight_records(tmp_path, monkeypatch, body_size, cap, expected, truncated):
    prepared = Settings(database=tmp_path / 'memory.sqlite', index=tmp_path / 'index.sqlite')
    monkeypatch.setattr(engine, '_CONTEXT_CHAR_CAP', cap)
    with Store(prepared.database) as store:
        for i in range(9):
            store.create(f'e{i}', 'event', 'History', 'x' * (body_size - len(' END_FACT.')) + ' END_FACT.',
                         manual_surface=True)
        hits = {f'e{i}': {'id': f'e{i}', 'kind': 'event', 'document': store.read(f'e{i}')}
                for i in range(9)}
    evidence = engine._planning_evidence(prepared, 'History', hits,
                                        {key: 9 - i for i, key in enumerate(hits)}, 100, [])
    assert len(evidence) == 8 and 'e8' not in {row['ref'] for row in evidence}
    assert sum(len(row['text']) for row in evidence) == expected
    assert all(row['truncated'] == truncated for row in evidence)
    if not truncated:
        assert all(row['text'].endswith(' END_FACT.') for row in evidence)


@pytest.mark.parametrize('visibility,expected', [
    (None, False), ([], True),
    ([{'ref': 'bridge', 'truncated': False}], False),
    ([{'ref': 'bridge', 'truncated': True}], True),
])
def test_existing_candidate_visibility(planned, monkeypatch, visibility, expected):
    paths, _ = planned
    prepared = Settings(database=paths.database, index=paths.index)
    with Store(paths.database, read_only=True) as store:
        hits = {key: {'id': key, 'kind': 'event', 'document': store.read(key)}
                for key in ('anchor', 'bridge')}
    monkeypatch.setattr(engine, '_recall_hits', lambda *a, **kw: list(hits.values()))
    monkeypatch.setattr(engine, '_rank_scores', lambda p, q, pool, scores: scores)
    plan = {**decision()['searches'][0], 'missing': 'Current location'}
    result = engine._gap_candidate(None, prepared, 'Location?', [plan], hits, visible=visibility)
    assert (result is not None) == expected
    if expected:
        assert result['hit']['id'] == 'bridge'
    assert engine._gap_candidate(None, prepared, 'Location?', [plan], hits,
                                 visible=visibility, already_read={'bridge'}) is None


def test_changed_existing_candidate_not_reread(planned, monkeypatch):
    paths, _ = planned
    prepared = Settings(database=paths.database, index=paths.index)
    with Store(paths.database) as store:
        hits = {key: {'id': key, 'kind': 'event', 'document': store.read(key)}
                for key in ('anchor', 'bridge')}
        doc = store.read('bridge')
        store.revise('bridge', expected_revision=doc['revision'], title=doc['title'],
                     body_md='Oriole Atelier moved again.')
        changed = {**hits['bridge'], 'document': store.read('bridge')}
    monkeypatch.setattr(engine, '_recall_hits', lambda *a, **kw: [changed])
    monkeypatch.setattr(engine, '_rank_scores', lambda p, q, pool, scores: scores)
    plan = {**decision()['searches'][0], 'missing': 'Current location'}
    assert engine._gap_candidate(None, prepared, 'Location?', [plan], hits, visible=[]) is None


@pytest.mark.parametrize('enabled', [False, True])
def test_unseen_existing_candidate_reviewed_once_in_search(gap_memory, monkeypatch, enabled):
    paths, observed = gap_memory
    monkeypatch.setenv('SEREIN_AML_PLANNING_COVERAGE', str(int(enabled)))
    with Store(paths.database) as store:
        for i in range(7):
            store.create(f'filler{i}', 'event', 'Other detail', 'Unrelated detail.', manual_surface=True)
        hits = {key: {'id': key, 'kind': 'event', 'document': store.read(key)}
                for key in ['anchor', *[f'filler{i}' for i in range(7)], 'bridge']}
    monkeypatch.setattr(engine, '_recall_hits', lambda s, q, **kw:
                        list(hits.values()) if q == 'Lin Vale' else [hits['bridge']])
    monkeypatch.setattr(engine, '_rank_scores', lambda p, q, pool, scores:
                        {key: (0.001 if key == 'bridge' else 1) for key in pool})
    def model(prompt):
        observed['prompts'].append(prompt)
        return reviewer(prompt) if 'READS:\n' in prompt else decision()
    monkeypatch.setattr(engine, '_model_json', model)
    rows = search(top_k=100)
    assert len(rows) == 9 and len({row['id'] for row in rows}) == 9
    assert sum('READS:\n' in p for p in observed['prompts']) == int(enabled)
    if enabled:
        reads = json.loads(observed['prompts'][-1].split('READS:\n', 1)[1])
        assert len(reads) == 1 and reads[0]['material']['ref'] == 'bridge'


@pytest.mark.parametrize('enabled', [False, True])
def test_menu_seed_uses_reranking(planned, monkeypatch, enabled):
    paths, _ = planned
    monkeypatch.setenv('SEREIN_AML_PLANNING_COVERAGE', str(int(enabled)))
    monkeypatch.setattr(engine, '_EXPAND_GAPS', True)
    with Store(paths.database) as store:
        for i in range(8):
            store.create(f'filler{i}', 'event', 'Other detail', 'Unrelated detail.', manual_surface=True)
        hits = [{'id': key, 'kind': 'event', 'document': store.read(key)}
                for key in [*[f'filler{i}' for i in range(8)], 'anchor']]
    monkeypatch.setattr(engine, '_recall_hits', lambda *a, **kw: hits)
    seen = []
    def model(prompt):
        seen.extend(json.loads(prompt.split('MENUS:\n', 1)[1]))
        return {'sufficient': True, 'supports': [{'ref': 'anchor',
                'quote': 'Lin Vale joined Oriole Atelier.'}], 'missing': [], 'selections': []}
    monkeypatch.setattr(engine, '_model_json', model)
    search(top_k=100)
    assert bool(seen) == enabled
