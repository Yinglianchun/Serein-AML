"""Original Germany relation proposals and explicit review over current Scenes."""

from fastapi import APIRouter, HTTPException
from starlette.responses import JSONResponse
from ..compat.background import SceneReader, germany_config
from ..compat.germany.scene_linker import SceneLinker


def routes(settings,auth):
    router=APIRouter(dependencies=auth)
    config=germany_config(settings)
    # Review/read endpoints do not need model clients; generation belongs to the optional job.
    local_options={k:v for k,v in config.get('scene_linker',{}).items()
                   if k not in ('models','model','base_url','api_key')}
    linker=SceneLinker({**config,'scene_linker':local_options})
    scenes=SceneReader(settings.database)
    # Schema bootstrap is explicit in live mode; no background model is started here.
    linker.proposal_store(create=True)

    def response(result):
        status=result.get('status')
        code=404 if status=='not_found' else 409 if status in ('stale','conflict','confirmation_required') else 400 if status in ('error','invalid') else 200
        return JSONResponse(result,status_code=code)

    @router.get('/api/scene-edge-proposals')
    async def proposals(status: str='pending',proposal_id: str='',anchor_scene_id: str='',limit: int=20,include_context: bool=False):
        from ..compat.jobs import scene_job_failures
        if status == 'error':
            failures = scene_job_failures(settings)
            return response({'status':'ok','count':len(failures),'proposals':[], 'failed_jobs':failures})
        result = await linker.list_proposals(scenes,status=status,proposal_id=proposal_id,
            anchor_scene_id=anchor_scene_id,limit=max(1,min(100,limit)),include_context=include_context)
        return response({**result, 'failed_jobs': scene_job_failures(settings)})

    @router.post('/api/scene-relation-jobs/retry')
    def retry(body: dict):
        from ..compat.jobs import retry_scene_job
        from ..deployment import task_model
        if not task_model(settings.database, 'relations'):
            raise HTTPException(409, '请先配置 Scene 关联模型。')
        if not isinstance(body.get('scene_id'), str) or not isinstance(body.get('attempt_id'), str):
            raise HTTPException(400, 'Scene 和失败记录编号不能为空。')
        try:return response(retry_scene_job(settings, body['scene_id'], body['attempt_id']))
        except RuntimeError as exc:raise HTTPException(409, str(exc)) from exc

    @router.post('/api/scene-edge-proposals/manual')
    async def manual(body: dict):
        if body.get('confirm')!='CREATE_SCENE_EDGE_PROPOSAL':raise HTTPException(400,'Confirmation required')
        return response(await linker.create_manual_proposal(
            source_scene_id=body.get('source_scene_id',''),target_scene_id=body.get('target_scene_id',''),
            relation_type=body.get('relation_type',''),source_evidence=body.get('source_evidence',''),
            target_evidence=body.get('target_evidence',''),reason=body.get('reason',''),
            confidence=body.get('confidence',1),supersedes_edge_id=body.get('supersedes_edge_id',''),
            bucket_mgr=scenes,created_by='dashboard'))

    @router.post('/api/scene-edge-proposals/review')
    async def review(body: dict):
        return response(await linker.review_proposal(body.get('proposal_id',''),body.get('decision',''),
            body.get('confirm',''),scenes,reviewed_by='dashboard'))

    @router.get('/api/scene-edges')
    async def edges(include_inactive: bool=False,scene_id: str=''):
        titles={row['id']:row['metadata'].get('name','') for row in await scenes.list_all(include_archive=True)}
        rows=linker.list_scene_edges(include_inactive=include_inactive)
        return {'edges':[{**row,'source_title':titles.get(row['source'],''),'target_title':titles.get(row['target'],'')}
            for row in rows if not scene_id or scene_id in (row['source'],row['target'])]}

    @router.delete('/api/scene-edges/{edge_id}')
    def delete(edge_id: str,body: dict):
        if body.get('confirm')!='DELETE_SCENE_EDGE':raise HTTPException(400,'Confirmation required')
        return response(linker.deactivate_scene_edge(edge_id,scene_id=body.get('scene_id',''),
            reviewer='dashboard',reason=body.get('reason') or 'manual_remove'))

    @router.post('/api/scene-edges/{edge_id}/restore')
    async def restore(edge_id: str,body: dict):
        if body.get('confirm')!='RESTORE_SCENE_EDGE':raise HTTPException(400,'Confirmation required')
        return response(await linker.restore_scene_edge(edge_id,scenes,reviewed_by='dashboard'))

    return router
