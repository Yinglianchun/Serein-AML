import asyncio
import json
from types import SimpleNamespace

import pytest

from aml import arcs
from serein.config import Settings
from serein.core.store import Store


def candidate(**changes):
    return {'seed_source_type': 'event', 'seed_source_id': 'ev-2', 'title': 'Device history',
            'target_narrative_id': '', 'existing_proposal_id': '', 'reason': 'Installed then repaired same device',
            'materials': [{'source_type': 'event', 'source_id': key} for key in ('ev-2', 'ev-1')],
            'confidence': 'high', 'latest_date': '2024-01-02', **changes}


def propose(tmp_path, monkeypatch, outputs):
    settings = Settings(tmp_path/'memory.sqlite', tmp_path/'index.sqlite')
    scout = SimpleNamespace(role_rules=lambda: 'Use grounded materials only.')
    corridors = [{'seed': {'source_type': 'event', 'source_id': 'ev-2'},
                  'keywords': [], 'candidates': [{'source_type': 'event', 'source_id': 'ev-1'}]}]
    calls = []
    class Client:
        def __init__(self, *args):pass
        async def create(self, **payload):
            calls.append(payload)
            assert payload['store'] is False
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outputs.pop(0)))])
        async def close(self):pass
    monkeypatch.setattr(arcs, 'TaskClient', Client)
    result = asyncio.run(arcs.propose(settings, scout, {'model':'synthetic-other'}, corridors, [], 'synthetic-input'))
    with Store(settings.database, read_only=True) as store:
        attempts = [dict(row) for row in store.conn.execute('SELECT raw_text,error FROM aml_scout_attempts')]
    return result, calls, attempts


def test_arc_format_tolerance_keeps_grounded_public_candidate(tmp_path, monkeypatch):
    row = candidate(extra_note='unused format')
    row['materials'][0].update(source_type=' EVENT ', source_id=' ev-2 ')
    raw = '```json\n'+json.dumps({'output':{'candidates':[row]}})+'\n```'
    result, calls, attempts = propose(tmp_path, monkeypatch, [raw])
    assert result[0]['source_event_ids'] == ['ev-2', 'ev-1']
    assert len(calls) == 1 and attempts == [{'raw_text':raw, 'error':''}]


@pytest.mark.parametrize('invalid', [candidate(target_narrative_id='invented-arc', existing_proposal_id='1'),
    candidate(materials=[{'source_type':'event','source_id':'ev-1'}]),
    candidate(materials=[{'source_type':'event','source_id':key} for key in ('ev-2','ev-1','invented-material')])])
def test_arc_invalid_ids_or_missing_seed_get_correction_not_host_invention(tmp_path, monkeypatch, invalid):
    result, calls, attempts = propose(tmp_path, monkeypatch, [json.dumps({'candidates':[invalid]}),
                                                             json.dumps({'candidates':[candidate()]})])
    assert result[0]['target_narrative_id'] == ''
    assert result[0]['source_event_ids'] == ['ev-2','ev-1']
    assert len(calls) == 2 and attempts[0]['error'] and not attempts[1]['error']
    assert 'Host validation failed' in calls[1]['messages'][-1]['content']


def test_arc_legitimate_decline_does_not_trigger_retry(tmp_path, monkeypatch):
    result, calls, attempts = propose(tmp_path, monkeypatch, ['{"candidates":[]}'])
    assert result == [] and len(calls) == 1 and attempts[0]['error'] == ''


def test_arc_repeated_invalid_decision_is_bounded(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match='did not pass validation'):
        propose(tmp_path, monkeypatch, [json.dumps({'candidates':[candidate(target_narrative_id='invented')]})]*3)
    with Store(tmp_path/'memory.sqlite', read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM aml_scout_attempts').fetchone()[0] == 3
