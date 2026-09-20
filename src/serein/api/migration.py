"""Authenticated old-library upload, preview and durable conversion jobs."""
from fastapi import APIRouter,HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pathlib import Path
from pydantic import BaseModel,Field
from ..legacy_migration.web import upload,preview_path,entries,configure,read_plan,export_path_zip
from ..work_tasks import enqueue,pause
from ..file_lock import exclusive_lock


class Upload(BaseModel):
    content: str=Field(min_length=1,max_length=90_000_000)


class PathPreview(BaseModel):
    path: str=Field(min_length=1,max_length=4096)
    display_path: str=Field(default='',max_length=4096)


class Start(BaseModel):
    user_name: str=Field(min_length=1,max_length=64)
    ai_name: str=Field(min_length=1,max_length=64)
    aliases: list[str]=Field(default_factory=list,max_length=30)
    confirmed: bool=False
    generate_cues: bool=Field(default=True,strict=True)


def routes(settings,auth):
    router=APIRouter(dependencies=auth)
    @router.post('/v1/migration/preview')
    def preview(body:Upload):
        try:return upload(settings,body.content)
        except RuntimeError:raise HTTPException(409,'已有迁移任务运行，请先等待或暂停') from None

    @router.post('/v1/migration/preview-path')
    def preview_from_path(body:PathPreview):
        try:return preview_path(settings,body.path,body.display_path)
        except RuntimeError:raise HTTPException(409,'已有迁移任务运行，请先等待或暂停') from None

    @router.get('/v1/migration')
    def listing():return {'items':entries(settings)}

    @router.get('/v1/migration/{identifier}/export')
    def export_source(identifier:str):
        filename=export_path_zip(settings,identifier)
        return FileResponse(filename,media_type='application/zip',filename='ombre-legacy-source.zip',
            headers={'Cache-Control':'no-store'},background=BackgroundTask(Path(filename).unlink,missing_ok=True))

    @router.post('/v1/migration/{identifier}/continue')
    def proceed(identifier:str,body:Start):
        if not body.confirmed:raise ValueError('请先确认转换规则与模型费用')
        options={'user_name':body.user_name.strip(),'ai_name':body.ai_name.strip(),'aliases':[a.strip() for a in body.aliases if a.strip()],
                 'generate_cues':body.generate_cues}
        if not options['user_name'] or not options['ai_name'] or any(len(a)>64 for a in options['aliases']):raise ValueError('请填写名字，别名最多 64 字')
        try:
            with exclusive_lock(settings.database.parent/'migrations'/'operation.lock'):
                configure(settings,identifier,options)
                return enqueue(settings.database,'legacy:'+identifier)
        except RuntimeError:raise HTTPException(409,'已有迁移任务运行，请先等待或暂停') from None

    @router.post('/v1/migration/{identifier}/pause')
    def stop(identifier:str):
        read_plan(settings,identifier)
        return pause(settings.database,'legacy:'+identifier)
    return router
