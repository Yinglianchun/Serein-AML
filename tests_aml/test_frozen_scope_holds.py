"""Synthetic durable holds; no external models or production databases."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from serein.core.store import Store, Conflict, encode
from serein.compat.raw_archive import raw_archive
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from tests.test_public_features import settings, synthetic_runner
from aml import runtime


def seed(settings, session='held', count=1):
    raw_archive(settings).ingest([
        {'source_event_id':f'{session}-{i}-{role}', 'session_id':session,
         'role':role, 'text':'Synthetic application update.',
         'created_at':'2025-01-01T00:00:00Z'}
        for i in range(count) for role in ('user','assistant')],source='synthetic')
    p.initialize(settings.database)


@pytest.mark.parametrize('status', ['pending','paused_failure','needs_repair','routing_only','retry_wait','routed'])
def test_upgrade_preserves_frozen_records(settings,monkeypatch,status):
    seed(settings)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_batches SET status=? WHERE id=?',(status,batch['id']))
        store.conn.execute("INSERT INTO pipeline_jobs(id,batch_id,role,request_json) VALUES ('job',?,'track_router','{}')",(batch['id'],))
        store.conn.execute("INSERT INTO pipeline_job_failures VALUES ('job',2,'synthetic')")
        store.conn.execute("INSERT INTO pipeline_routes VALUES (1,'{}')")
        store.conn.execute("INSERT INTO pipeline_route_provenance VALUES (1,?,'{}')",(batch['id'],))
    monkeypatch.setattr(p,'runtime_revision',lambda:'synthetic-new-version')
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()
        assert row['input_json']==batch['input_json']
        assert row['status']==('needs_repair' if status in ('pending','routing_only','retry_wait') else status)
        assert store.conn.execute('SELECT failures FROM pipeline_job_failures').fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==1
        assert store.conn.execute('SELECT count(*) FROM pipeline_route_provenance').fetchone()[0]==1
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==2
    if status=='paused_failure':
        with pytest.raises(Conflict,match='contract changed'):p.retry_batch(settings.database,batch['id'])


@pytest.mark.parametrize('status',['needs_repair','paused_failure','retry_wait','routing_only'])
def test_held_scope_does_not_starve_independent_scope(settings,status):
    seed(settings)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_batches SET status=?,result_json=? WHERE id=?',
                           (status,encode({'status':status,'batch_id':batch['id']}),batch['id']))
    seed(settings,'independent')
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['status']=='processed' and result['processed_originals']==2
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['status'] in ('blocked','needs_repair')
    with Store(settings.database,read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==[3,4]


def test_failed_request_is_not_rebatched_when_budget_changes(settings):
    seed(settings,count=4)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_jobs(id,batch_id,role,request_json) VALUES ('job',?,'track_router','{}')",(batch['id'],))
        store.conn.execute("INSERT INTO pipeline_job_failures VALUES ('job',1,'synthetic')")
    save_settings(settings.database,{'pipeline':{'max_input_chars':1}})
    again=p.new_batch(settings.database,True)
    assert again['id']==batch['id'] and again['input_json']==batch['input_json']


@pytest.mark.parametrize('status',['blocked','needs_repair','retry_wait','routing_only'])
def test_aml_hold_is_not_success_or_a_busy_retry(monkeypatch,status):
    calls=[]
    async def advance(*args,**kwargs):
        calls.append(1)
        return {'status':status}
    monkeypatch.setattr(p,'advance',advance)
    with pytest.raises(RuntimeError,match='unfinished or paused batch'):
        asyncio.run(runtime.ingest_pipeline(SimpleNamespace(database='unused')))
    assert len(calls)==1


@pytest.mark.parametrize('status',['pending','paused_failure'])
def test_explicit_rebuild_preserves_old_plan_after_upgrade(settings,monkeypatch,status):
    from serein.extensions import pipeline_recovery
    seed(settings)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_batches SET status=? WHERE id=?',(status,batch['id']))
    monkeypatch.setattr(p,'runtime_revision',lambda:'synthetic-new-version')
    p.initialize(settings.database)
    result=pipeline_recovery._rebuild(settings.database,batch['id'])
    with Store(settings.database,read_only=True) as store:
        old=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()
        new=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(result['batch_id'],)).fetchone()
        assert old['status']=='superseded_repair' and old['input_json']==batch['input_json']
        assert json.loads(new['input_json'])['runtime_revision']=='synthetic-new-version'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0



def test_three_failures_remain_paused_across_restart_and_budget_change(settings):
    seed(settings)
    calls=[]
    async def fail(role,request):
        calls.append(role)
        raise OSError('synthetic transport failure')
    for _ in range(2):
        with pytest.raises(OSError):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))['status']=='paused'
    p.initialize(settings.database)
    save_settings(settings.database,{'pipeline':{'max_input_chars':1}})
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))['status']=='blocked'
    assert len(calls)==3
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT failures FROM pipeline_job_failures').fetchone()[0]==3
        batch=store.conn.execute("SELECT id FROM pipeline_batches WHERE status='paused_failure'").fetchone()[0]
    assert p.retry_batch(settings.database,batch)['status']=='resumed'


from test_engine import memory, add
from aml import engine

@pytest.mark.parametrize('status',['needs_repair','paused_failure','retry_wait','routing_only'])
def test_actual_add_keeps_receipt_pending_for_holds(memory,status):
    add([{'role':'user','content':'Synthetic incomplete tail.'}])
    paths=engine._paths('user-a')
    with Store(paths.database) as store:
        frozen=encode({'contract':p.CONTRACT,'runtime_revision':p.runtime_revision()})
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('held','test',?,?)",(frozen,status))
    for _ in range(2):
        with pytest.raises(RuntimeError,match='paused batch'):
            add([{'role':'user','content':'Another synthetic tail.'}],request_id='req-hold')
    with Store(paths.database,read_only=True) as store:
        assert store.conn.execute("SELECT status FROM aml_add_receipts WHERE id=?",('aml:'+engine.hashlib.sha256(b'req-hold').hexdigest(),)).fetchone()[0]=='pending'
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==2


def test_same_scope_pending_cannot_bypass_earlier_pause(settings):
    seed(settings)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET status='paused_failure' WHERE id=?",(batch['id'],))
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('later',?,?,'pending')",(batch['scope'],batch['input_json']))
    assert p.new_batch(settings.database,True) is None
    assert p.held_result(settings.database)['status']=='blocked'


def test_same_version_pause_cannot_rebuild_around_retry_budget(settings):
    from serein.extensions import pipeline_recovery
    seed(settings)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET status='paused_failure' WHERE id=?",(batch['id'],))
    with pytest.raises(Conflict,match='显式重试'):
        pipeline_recovery._rebuild(settings.database,batch['id'])
