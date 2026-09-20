"""Host-owned delivery acknowledgements and archive-backed evidence selection."""
import json
from fastapi import APIRouter, HTTPException, Query
from ..core.store import Store, encode, Conflict
from ..compat.raw_archive import raw_archive


def routes(settings, auth):
    router=APIRouter(dependencies=auth)
    with Store(settings.database) as store:
        store.conn.execute('CREATE TABLE IF NOT EXISTS host_deliveries '
            '(id INTEGER PRIMARY KEY, receipt_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')

    @router.post('/v1/host/deliveries')
    def delivered(body:dict):
        if not body.get('receipt_id') or not body.get('window_id') or not isinstance(body.get('delivered_ids'),list):
            raise HTTPException(400,'receipt_id, window_id and actually delivered_ids are required')
        if len(body['delivered_ids'])>500 or any(not isinstance(k,str) for k in body['delivered_ids']):
            raise HTTPException(400,'Invalid delivery IDs')
        payload=encode({k:body[k] for k in ('receipt_id','window_id','delivered_ids')})
        with Store(settings.database) as store,store.transaction():
            old=store.conn.execute('SELECT * FROM host_deliveries WHERE receipt_id=?',(body['receipt_id'],)).fetchone()
            if old and old['payload']!=payload:
                raise Conflict('Delivery receipt already records a different acknowledgement')
            store.conn.execute('INSERT OR IGNORE INTO host_deliveries(receipt_id,payload) VALUES (?,?)',(body['receipt_id'],payload))
        return {'status':'recorded','reported_by':'host','delivered_ids':body['delivered_ids']}

    @router.get('/v1/host/deliveries')
    def history(limit:int=Query(80,ge=1,le=200),before_id:int=0):
        with Store(settings.database,read_only=True) as store:
            rows=store.conn.execute('SELECT * FROM host_deliveries WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT ?',
                (before_id,before_id,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            payload=json.loads(row['payload'])
            items.append({'id':row['id'],**payload,'gateway_memory_injected_ids':payload['delivered_ids'],
                'hook_memory_outcome':'injected' if payload['delivered_ids'] else 'no_match',
                'gateway_memory_trigger':'host_acknowledgement','session_id':payload['window_id']})
        return {'status':'ok','items':items,
                'has_more':len(rows)>limit,'next_before_id':rows[min(limit,len(rows))-1]['id'] if rows else 0}

    @router.post('/v1/host/messages/search')
    def search(body:dict):
        archive=raw_archive(settings)
        ids=body.get('messageIds') or body.get('message_ids')
        limit=max(1,min(100,int(body.get('limit') or 30)))
        before=int(body.get('beforeId') or 0)
        context=int(body.get('contextMessageId') or 0)
        query=str(body.get('query') or '').strip()
        more=False
        mode='search' if query else 'browse'
        if ids:
            if not isinstance(ids,list) or not 1<=len(ids)<=100:
                raise HTTPException(400,'Select at most 100 messages')
            with Store(settings.database,read_only=True) as store:
                rows=store.conn.execute('SELECT * FROM raw_events WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id',ids).fetchall()
                items=[archive._row_to_event(r) for r in rows]
        else:
            with Store(settings.database,read_only=True) as store:
                if context:
                    target=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(context,)).fetchone()
                    if target is None:
                        raise HTTPException(404,'Original context message not found')
                    radius=max(1,min(20,int(body.get('contextRadius') or 6)))
                    scope=(target['source'],target['session_id'])
                    earlier=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id<? ORDER BY id DESC LIMIT ?',(*scope,context,radius)).fetchall()
                    later=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id>=? ORDER BY id LIMIT ?',(*scope,context,radius+1)).fetchall()
                    rows=list(reversed(earlier))+later
                    mode='context'
                else:
                    session=str(body.get('sessionId') or body.get('session_id') or '')
                    conditions=['(?=0 OR id<?)','(?=\'\' OR session_id=?)']
                    params=[before,before,session,session]
                    if query.startswith('#'):
                        conditions.append('(source_event_id=? OR CAST(id AS TEXT)=?)');params.extend([query[1:]]*2)
                    elif query:
                        conditions.append('instr(text,?)>0');params.append(query)
                    rows=store.conn.execute('SELECT * FROM raw_events WHERE '+' AND '.join(conditions)+' ORDER BY id DESC LIMIT ?',(*params,limit+1)).fetchall()
                    more=len(rows)>limit;rows=rows[:limit]
                items=[archive._row_to_event(r) for r in rows]
        messages=[{**r,'content':r['text'],'source_system':r['source'],
                   'source_message_id':r['source_event_id'] or str(r['id'])} for r in items]
        return {'status':'ok','messages':messages,'items':messages,'count':len(messages),'mode':mode,
                'target_message_id':context or None,'has_more':more,
                'next_before_id':min((r['id'] for r in messages),default=0)}
    return router
