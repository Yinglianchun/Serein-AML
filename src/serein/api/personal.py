from fastapi import APIRouter, HTTPException, Query
from ..core.personal import Personal
from ..core.store import Conflict


def routes(settings, auth):
    router=APIRouter(dependencies=auth)
    personal=Personal(settings.database)

    @router.get('/api/personal')
    def read(scope:str, offset:int=Query(0,ge=0), limit:int=Query(500,ge=1,le=500)):
        return personal.list(scope,offset=offset,limit=limit)

    @router.post('/api/personal')
    def save(body:dict):
        try:return personal.save(**body)
        except Conflict as error:raise HTTPException(409,str(error))

    @router.post('/api/personal/import')
    def import_legacy(body:dict):return personal.import_legacy(body.get('records'))

    return router
