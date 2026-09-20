"""HTTP access to exactly the same enabled contributions as MCP."""
from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool


def routes(application, auth):
    router = APIRouter(dependencies=auth)
    builtins = {'memory_read','memory_materials','memory_search','memory_write','memory_candidates',
                'memory_recall','source_messages','source_read'}

    @router.post('/v1/extensions/{name}')
    async def call(name: str, arguments: dict):
        application.refresh_optional()
        tools = {k:v for k,v in application.contributions.tools.items() if k not in builtins}
        if name not in tools:
            raise HTTPException(404, 'Feature is disabled or unavailable')
        import inspect
        try:
            inspect.signature(tools[name]).bind(**arguments)
        except TypeError as exc:
            raise HTTPException(400, str(exc)) from None
        if inspect.iscoroutinefunction(tools[name]):
            return await tools[name](**arguments)
        return await run_in_threadpool(tools[name], **arguments)

    return router
