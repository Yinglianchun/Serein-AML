"""Authenticated client compatibility routes for the writable private service."""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..compat.events import Events
from ..compat.germany.fact_events import FactEventSettlementBlockedError
from ..core.store import Store


def event_surface_states(database,result):
    with Store(database,read_only=True) as store:
        for item in result.get('items',[]):
            if item.get('item_type')=='event':
                item['surface_state']=store.surface_state(item['item_id'])
    return result


def routes(settings, services, auth):
    router = APIRouter(dependencies=auth)
    events = Events(settings.database)

    @router.get('/api/fact-events')
    def list_events(type: str='', status: str='active', date: str='', query: str='',
                    limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0), include_sources: bool=False):
        return event_surface_states(settings.database,events.list(item_type=type,status=status,date=date,query=query,limit=limit,offset=offset,include_sources=include_sources))

    @router.post('/api/fact-events/read-many')
    def read_many(body: dict):
        return event_surface_states(settings.database,events.read_many(body.get('item_ids'), include_sources=bool(body.get('include_sources',True)),
                                resolve_active_successors=bool(body.get('resolve_active_successors',False))))

    @router.post('/api/fact-events/find-by-source-keys')
    def find_sources(body: dict):
        return events.find_active_events_by_source_keys(body.get('source_keys'))

    @router.get('/api/fact-events/active-leaf')
    def active_leaf(item_id: str):
        return events.replacement_family(item_id)

    def written(result):
        # Indexing errors do not turn a committed settlement into an HTTP failure.
        # A persistent worker retries the durable outbox, including vector fills.
        return {**result,'index_status':'queued'}

    @router.post('/api/fact-events/batch')
    def batch(body: dict):
        return written(events.write_many(body.get('items')))

    @router.post('/api/fact-events/settlement')
    def settle(body: dict):
        try:
            return written(events.settle(body.get('operation_id'),body.get('items')))
        except FactEventSettlementBlockedError as error:
            return JSONResponse(status_code=409,content={'ok':False,'status':'blocked',
                'operation_id':body.get('operation_id'),'reason':str(error),'writes_performed':[]})

    @router.post('/api/fact-events/replacements')
    def replacements(body: dict):
        return written(events.replace_many(body.get('items')))

    @router.post('/api/fact-events/revise')
    def revise(body: dict):
        return written(events.revise(body.get('item_id',''),**{k:v for k,v in body.items() if k in ('title','body','importance','recallable')}))

    @router.post('/api/fact-events/status')
    def status(body: dict):
        return written(events.set_status(body.get('item_id',''),body.get('status','')))

    @router.post('/api/fact-events/delete')
    def delete(body: dict):
        return written(events.delete(body.get('item_id','')))

    @router.post('/api/fact-events/recallable-review')
    def review(body: dict):
        return written(events.review_event_recallable(body.get('reviews'),apply=bool(body.get('apply',False))))

    @router.post('/api/fact-events/injected')
    def injected(body: dict):
        return events.mark_injected(body.get('item_ids'))

    @router.post('/api/fact-events/relation-candidates')
    def relation_candidates(body: dict):
        return events.relation_candidates(body.get('new_item_ids'),limit_per_fact=body.get('limit_per_fact',30))

    @router.post('/api/fact-events/relation-proposals')
    def propose(body: dict):
        return events.propose_relations(body.get('proposals'))

    @router.get('/api/fact-events/relation-proposals')
    def proposals(status: str='pending', limit: int=100):
        return events.list_relation_proposals(status=status,limit=limit)

    return router
