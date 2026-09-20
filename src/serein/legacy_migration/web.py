"""Uploaded snapshots and resumable migration using the same CLI workflow."""
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from ..core.store import encode
from ..deployment import task_model
from ..file_lock import exclusive_lock
from ..work_tasks import progress, status
from .scan import scan, unpack
from .workflow import Migration,MigrationPaused,migration_options

MAX_UPLOAD=64*1024*1024


def location(settings, identifier):
    if not re.fullmatch(r'[0-9a-f]{64}',identifier):raise ValueError('无效的旧库任务编号')
    return settings.database.parent/'migrations'/'uploads'/identifier


def upload(settings, content):
    try:raw=base64.b64decode(content,validate=True)
    except (ValueError,binascii.Error):raise ValueError('备份编码不正确') from None
    if not raw or len(raw)>MAX_UPLOAD:raise ValueError('网页备份上限为 64 MiB，较大备份请使用脚本')
    identifier=hashlib.sha256(raw).hexdigest();root=location(settings,identifier)
    with exclusive_lock(settings.database.parent/'migrations'/'operation.lock'):
        root.mkdir(parents=True,exist_ok=True)
        file=root/'input.tar'
        if not file.exists():file.write_bytes(raw)
        plan=scan(unpack(file,root))
        (root/'plan.json').write_text(encode(plan),'utf-8')
    return {'id':identifier,'summary':plan['summary'],'errors':plan['errors']}


def validated_source(settings, value):
    source=Path(value).expanduser()
    if not source.is_absolute():raise ValueError('请输入运行 Serein 的机器上的绝对路径')
    try:source=source.resolve(strict=True)
    except (FileNotFoundError,NotADirectoryError):raise ValueError('旧库路径不存在，请核对运行 Serein 的机器上的位置') from None
    if not (source.is_dir() or source.is_file()):raise ValueError('旧库必须是目录或 tar/tar.gz 文件')
    allowed=os.environ.get('SEREIN_MIGRATION_PATH_ROOT','').strip()
    if allowed:
        root=Path(allowed).resolve(strict=True)
        if source!=root and not source.is_relative_to(root):raise ValueError('此目录尚未挂载给记忆服务；请先在安装菜单授权只读旧库目录')
    elif source==settings.database.parent.resolve() or source.is_relative_to(settings.database.parent.resolve()):
        raise ValueError('旧库不能指向当前实例数据目录')
    return source


def preview_path(settings, value, display_path=''):
    source=validated_source(settings,value)
    workspace=settings.database.parent/'migrations'/'path-sources'/hashlib.sha256(str(source).encode()).hexdigest()
    with exclusive_lock(settings.database.parent/'migrations'/'operation.lock'):
        plan=scan(unpack(source,workspace))
        identifier=hashlib.sha256(encode(plan).encode()).hexdigest()
        root=location(settings,identifier)
        root.mkdir(parents=True,exist_ok=True)
        (root/'plan.json').write_text(encode(plan),'utf-8')
        (root/'source.json').write_text(encode({'path':str(source),'display_path':display_path or str(source)}),'utf-8')
    return {'id':identifier,'summary':plan['summary'],'errors':plan['errors']}


def read_plan(settings,identifier):
    file=location(settings,identifier)/'plan.json'
    if not file.is_file():raise ValueError('请先上传并预览旧库备份')
    return json.loads(file.read_text('utf-8'))


def export_path_zip(settings, identifier):
    """Archive a previously previewed server-side source for one browser download."""
    read_plan(settings, identifier)
    source_file = location(settings, identifier)/'source.json'
    if not source_file.is_file():
        raise ValueError('只有输入旧库路径并预览后，才能导出原库 ZIP')
    source = validated_source(settings, json.loads(source_file.read_text('utf-8'))['path'])
    archive_root = unpack(source, settings.database.parent/'migrations'/'path-sources'/hashlib.sha256(str(source).encode()).hexdigest())
    fd, filename = tempfile.mkstemp(prefix='serein-legacy-', suffix='.zip')
    os.close(fd)
    count = total = 0
    try:
        with zipfile.ZipFile(filename, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            roots = archive_root.rglob('*')
            for item in roots:
                if item.is_symlink():
                    raise ValueError('旧库含符号链接，无法安全导出')
                if not item.is_file():
                    continue
                if item.name == '.complete' and item.parent == archive_root:
                    continue
                count += 1
                total += item.stat().st_size
                if count > 50000 or total > 4*1024*1024*1024:
                    raise ValueError('旧库 ZIP 超出网页导出上限（5 万文件／4 GiB）')
                name = item.relative_to(archive_root).as_posix()
                archive.write(item, name)
        return filename
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise


def entries(settings):
    root=settings.database.parent/'migrations'/'uploads'
    if not root.exists():return []
    result=[]
    for folder in sorted(root.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
        if not re.fullmatch(r'[0-9a-f]{64}',folder.name) or not (folder/'plan.json').is_file():continue
        plan=read_plan(settings,folder.name)
        options=json.loads((folder/'options.json').read_text('utf-8')) if (folder/'options.json').is_file() else None
        source=json.loads((folder/'source.json').read_text('utf-8')) if (folder/'source.json').is_file() else None
        result.append({'id':folder.name,'source_path':source['display_path'] if source else None,
                       'summary':plan['summary'],'errors':plan['errors'],
                       'options':options,'task':status(settings.database,'legacy:'+folder.name)})
    return result


def configure(settings,identifier,options):
    options=migration_options(options)
    plan=read_plan(settings,identifier)
    if plan['errors']:raise ValueError('备份含扫描错误，请修正后重新上传')
    for task in ('operit_tagging','embedding'):
        if not task_model(settings.database,task):raise ValueError('请先在设置 → 配置选择打标和嵌入模型')
    file=location(settings,identifier)/'options.json'
    if file.exists():
        previous=migration_options(json.loads(file.read_text('utf-8')))
        if {k:v for k,v in previous.items() if k!='generate_cues'}!={k:v for k,v in options.items() if k!='generate_cues'}:
            raise ValueError('续跑时请保留首次确认的名字和别名')
        if previous!=options and status(settings.database,'legacy:'+identifier).get('status') in ('queued','running'):
            raise RuntimeError('请先暂停迁移，再修改 cues 选项')
    file.write_text(encode(options),'utf-8')


def run(settings,identifier):
    from ..configured_models import prepare_selected,effective_settings
    from ..recall.legacy_indexes import refresh
    from .vectors import reuse_legacy
    key='legacy:'+identifier
    def checkpoint(stage):
        progress(stage=stage)
        return status(settings.database,key).get('stop_requested',False)
    with exclusive_lock(settings.database.parent/'migrations'/'operation.lock'):
        plan=read_plan(settings,identifier)
        source_file=location(settings,identifier)/'source.json'
        if source_file.is_file():
            source=validated_source(settings,json.loads(source_file.read_text('utf-8'))['path'])
            current=scan(unpack(source,settings.database.parent/'migrations'/'path-sources'/hashlib.sha256(str(source).encode()).hexdigest()))
            if encode(current)!=encode(plan):raise ValueError('旧库在预览后发生变化，请重新预览并确认')
        if plan['errors']:raise ValueError('备份含扫描错误')
        options=json.loads((location(settings,identifier)/'options.json').read_text('utf-8'))
        migration=Migration(settings,plan,options)
        try:
            migration.backup();migration.freeze_configuration()
            if checkpoint('history'):return {'status':'paused'}
            migration.import_history()
            if checkpoint('companion'):return {'status':'paused'}
            migration.import_companion()
            if checkpoint('bodies'):return {'status':'paused'}
            migration.import_bodies()
            if checkpoint('tagging'):return {'status':'paused'}
            asyncio.run(migration.tag_all())
            if checkpoint('edges'):return {'status':'paused'}
            asyncio.run(migration.edges())
            if checkpoint('vectors'):return {'status':'paused'}
            migration.freeze_configuration()
            if not migration.done('all','vectors'):
                reuse={}
                def reuse_before_fill(current,profile):reuse.update(reuse_legacy(current,profile,plan,migration.ids))
                result=prepare_selected(settings,before_fill=reuse_before_fill)
                migration.mark('all','vectors','done',{'reuse':reuse,'preparation':result})
            if checkpoint('cue_bindings'):return {'status':'paused'}
            migration.freeze_configuration()
            report=asyncio.run(refresh(effective_settings(settings), retry_failed=True))
            if report['cues'].get('failed_scenes'):raise ValueError('部分 cue passage 绑定失败，可继续重试')
            migration.mark('all','cue_bindings','done',report)
            return {'status':'completed','stages':migration.report()}
        except MigrationPaused:
            return {'status':'paused','stages':migration.report()}
        finally:
            migration.report();migration.close()
