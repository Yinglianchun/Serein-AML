import sqlite3
from contextlib import closing

import pytest

from serein.compat.events import Events
from serein.core import Store
from serein.core.reader import Reader


def item(body='一起看雨', origin='assistant_bridge:test'):
    return {'type':'event','title':'雨天','body':body,'origin_id':origin,'recallable':True,
            'source_refs':[{'source_system':'assistant_bridge','session_id':'1','message_id':'7',
                            'role':'user','created_at':'2026-09-07T10:00:00+08:00',
                            'content':'我们一起看雨吧','binding_method':'approved_candidate'}]}


@pytest.fixture
def events(tmp_path):
    database = tmp_path/'serein.db'
    with Store(database):
        pass
    return Events(database, initialize=True)


def test_event_write_replay_edit_and_read_share_transaction(events):
    result=events.write_many([item()])
    key=result['items'][0]['item_id']
    assert result['inserted']==1
    assert events.write_many([item()])['idempotent']==1
    with Reader(events.db_path) as reader:
        obj=reader.read(key)
        assert obj['document']['body_md']=='一起看雨'
        assert obj['surface_state']['can_surface']
        assert obj['evidence'][0]['content']=='我们一起看雨吧'
    revised=events.revise(key,body='约好了下雨时一起看雨',recallable=True)
    successor=revised['item']['item_id']
    with Reader(events.db_path) as reader:
        assert reader.read(key)['status']=='superseded'
        assert reader.read(successor)['document']['body_md']=='约好了下雨时一起看雨'
    events.delete(successor)
    with Reader(events.db_path) as reader:
        assert reader.read(successor)['status']=='deleted'
        assert reader.read(successor)['document'] is None


def test_projection_failure_rolls_back_canonical_event_and_receipt(events,monkeypatch):
    def fail(_conn):
        raise ValueError('projection failed')
    monkeypatch.setattr('serein.compat.events.project_events',fail)
    with pytest.raises(ValueError,match='projection failed'):
        events.write_many([item()])
    with sqlite3.connect(events.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==0


def test_settlement_receipt_survives_retry_without_duplicate_projection(events):
    result=events.settle('test_settlement',[item()])
    key=result['items'][0]['item_id']
    with Reader(events.db_path) as reader:
        before=reader.read(key)
    replay=events.settle('test_settlement',[item()])
    assert replay['items'][0]['item_id']==key
    with Reader(events.db_path) as reader:
        assert reader.read(key)==before


def test_new_event_is_suppressed_by_existing_exact_scene_evidence(events):
    result=events.write_many([item()]);key=result['items'][0]['item_id']
    with Store(events.db_path) as store:
        source=store.conn.execute('SELECT source_id FROM evidence_bindings WHERE document_id=?',(key,)).fetchone()[0]
        store.create('scene_rain','scene','下雨','我们一起看雨')
        store.bind('scene_rain',source)
    with Reader(events.db_path) as reader:
        assert reader.read(key)['status']=='active'
        assert reader.read(key)['surface_state']['reasons']==['covered_by_scene']
