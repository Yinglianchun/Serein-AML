"""Diary HTTP compatibility used by the current frontend and Bridge."""

from fastapi import APIRouter, HTTPException
from ..compat.diaries import Diaries


def routes(settings, auth):
    router=APIRouter(dependencies=auth)
    diaries=Diaries(settings.database)

    @router.post('/diaries')
    def create(body: dict):
        result=diaries.create(content=str(body.get('content') or ''),date=str(body.get('date') or ''),
            title=body.get('title'),emotion_tags=body.get('emotion_tags'),author=str(body.get('author') or 'ai'),
            unlock_at=str(body.get('unlock_at') or ''))
        return {key:result.get(key) for key in ('id','entry_type','locked','unlock_at')}

    @router.post('/diaries/search')
    def search(body: dict):
        result=diaries.search(**{k:v for k,v in body.items() if k in ('keyword','date','title','start_date','end_date','limit','offset')})
        return {key:result[key] for key in ('count','diaries')}

    @router.get('/diaries/date/{date}/all')
    def date_all(date: str,title: str='',limit: int=100):
        return diaries.read(date=date,title=title,limit=limit)

    @router.get('/diaries/date/{date}')
    def date_one(date: str):
        result=diaries.read(date=date,limit=1)
        if not result['diaries']:
            raise HTTPException(404,'Diary not found')
        return result['diaries'][0]

    @router.get('/diaries/{diary_id}')
    def read(diary_id: int):
        result=diaries.read(diary_id=diary_id,limit=1)
        if not result['diaries']:
            raise HTTPException(404,'Diary not found')
        return result['diaries'][0]

    @router.put('/diaries/{diary_id}')
    def revise(diary_id: int,body: dict):
        result=diaries.revise(diary_id,**{k:v for k,v in body.items() if k in ('content','date','title','emotion_tags','unlock_at')})
        return {'diary_id':result['id'],'revision':result['revision']}

    @router.delete('/diaries/{diary_id}')
    def delete(diary_id: int):
        return diaries.delete(diary_id)

    @router.get('/diaries/{diary_id}/comments')
    def comments(diary_id: int):
        return diaries.comments(diary_id)

    @router.post('/diaries/{diary_id}/comments')
    def comment(diary_id: int,body: dict):
        return diaries.comment(diary_id,content=str(body.get('content') or ''),author='user')

    @router.delete('/diaries/{diary_id}/comments/{comment_id}')
    def delete_comment(diary_id: int,comment_id: int):
        return diaries.delete_comment(diary_id,comment_id)

    return router
