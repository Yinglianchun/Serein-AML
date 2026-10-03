"""Raw dialogue ingestion and lookup used by the existing Bridge queue."""

from fastapi import APIRouter, HTTPException, Request

from ..compat.raw_archive import raw_archive
from ..compat.germany.raw_ingest import _raw_ingest_events_from_body


def routes(settings, auth):
    router = APIRouter(dependencies=auth)
    archive = raw_archive(settings)

    @router.post('/api/ingest-raw')
    def ingest(body: dict, request: Request):
        session = request.headers.get('X-Serein-Session-Id', '').strip()
        events = _raw_ingest_events_from_body(body, default_session_id=session, default_conversation_id=session)
        if not events:
            raise HTTPException(400, 'missing events')
        if len(events)>archive.max_ingest_batch:
            raise HTTPException(413, f'At most {archive.max_ingest_batch} original messages per request; split larger imports')
        return archive.ingest(events, source=str(body.get('source') or 'raw'))

    @router.api_route('/api/search-raw', methods=['GET', 'POST'])
    async def search(request: Request):
        params = dict(request.query_params)
        if request.method == 'POST':
            params.update(await request.json())
        return archive.search(query=str(params.get('q', params.get('query', '')) or ''),
            limit=int(params.get('limit') or 10),
            **{k: str(params.get(k) or '') for k in ('source', 'role', 'conversation_id', 'session_id', 'since', 'until')})

    return router
