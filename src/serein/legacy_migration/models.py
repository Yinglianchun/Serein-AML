"""Import only model connections from a user-selected legacy configuration."""
from pathlib import Path
import re
import yaml
from ..api.settings import ModelEntry, UpstreamEntry
from ..deployment import read_settings, save_settings, configured_models


def load_models(root):
    root=Path(root).resolve()
    path=next((root/name for name in ('config.yaml','config.yml','state/config.runtime.yaml') if (root/name).is_file()),None)
    if not path:return {'models':[],'upstreams':[],'assignments':{}},['没有找到旧模型配置']
    if not path.resolve().is_relative_to(root):raise ValueError('旧模型配置不能指向所选备份之外')
    cfg=yaml.safe_load(path.read_text('utf-8')) or {}
    if not isinstance(cfg,dict):raise ValueError('旧模型配置必须是 YAML 对象')
    env={}
    for file in (root/'.env',path.parent/'.env'):
        if file.is_file():
            if not file.resolve().is_relative_to(root):raise ValueError('旧模型密钥文件不能指向所选备份之外')
            for line in file.read_text('utf-8').splitlines():
                if not line.strip() or line.lstrip().startswith('#') or '=' not in line:continue
                key,value=line.split('=',1);env[key.strip()]=value.strip().strip('\"\'')
    def key(raw):
        value=str(raw.get('api_key') or '')
        match=re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',value)
        names=raw.get('api_key_envs') or []
        if isinstance(names,str):names=[names]
        return env.get(match[1],'') if match else value or env.get(raw.get('api_key_env',''),'') or next((env[n] for n in names if env.get(n)), '')
    models=[];assignments={};upstreams=[];issues=[]
    gateway=cfg.get('gateway') or {}
    if not isinstance(gateway,dict):raise ValueError('旧 gateway 配置格式错误')
    for index,raw in enumerate(gateway.get('upstreams') or []):
        try:
            raw={**raw,'api_key':key(raw)}
            raw={k:v for k,v in raw.items() if k in UpstreamEntry.model_fields}
            upstreams.append(UpstreamEntry(**raw).model_dump(exclude_none=True))
        except (ValueError,TypeError):issues.append(f'第 {index+1} 个旧聊天上游格式不完整，需在页面补配')
    sections={'operit_tagging':cfg.get('dehydration'), 'embedding':cfg.get('embedding'),
        'reranker':gateway.get('reranker') or cfg.get('reranker'), 'persona':cfg.get('persona')}
    for task,raw in sections.items():
        if not isinstance(raw,dict) or not raw.get('model'):continue
        # The old loader used these named environment overrides. Resolve only
        # the .env files inside the selected backup, never this process's env.
        prefix={'operit_tagging':'OMBRE_DEHYDRATION','embedding':'OMBRE_EMBEDDING','reranker':'OMBRE_RERANKER','persona':'OMBRE_PERSONA'}[task]
        raw=dict(raw)
        for field,suffix in (('base_url','BASE_URL'),('model','MODEL'),('api_key','API_KEY')):
            value=env.get(prefix+'_'+suffix,'')
            if task=='operit_tagging':value=value or env.get({'base_url':'OMBRE_BASE_URL','model':'OMBRE_MODEL','api_key':'OMBRE_API_KEY'}[field],'')
            if value:raw[field]=value
        if task=='embedding':
            dehy=cfg.get('dehydration') or {}
            raw={**raw,'base_url':raw.get('base_url') or dehy.get('base_url') or 'https://generativelanguage.googleapis.com/v1beta/openai/',
                'api_key':key(raw) or key(dehy) or env.get('OMBRE_API_KEY',''),
                'query_instruction':raw.get('query_instruction') or 'Given a memory search query, retrieve relevant long-term memory passages.'}
        try:
            model=ModelEntry(id='legacy-'+task,label=str(raw['model'])[:100],model=raw['model'],
                base_url=raw.get('base_url') or raw.get('api_base') or '',api_key=key(raw),
                protocol='openai',document_instruction=raw.get('document_instruction') or '',
                query_instruction=raw.get('query_instruction') or '').model_dump(exclude_none=True)
            models.append(model);assignments[task]=model['id']
            if not model.get('api_key'):issues.append(task+' 的密钥未能从旧配置解析，需要确认该上游是否免密')
        except (ValueError,TypeError):issues.append(task+' 的旧模型连接不完整，需补配')
    return {'models':models,'upstreams':upstreams,'assignments':assignments},issues


def import_models(database,root):
    patch,issues=load_models(root)
    current=read_settings(database)
    # Re-entry preserves edits made in the new settings page.
    models={m['id']:m for m in patch['models']}
    grouped_ids={m['id'] for m in configured_models({**current,'models':[]})}
    models={key:value for key,value in models.items() if key not in grouped_ids}
    models.update({m['id']:m for m in current['models']})
    assignments={**patch['assignments'],**current['assignments']}
    changes={'models':list(models.values()),'upstreams':current['upstreams'] or patch['upstreams'],
        'assignments':assignments}
    if any(current[key]!=value for key,value in changes.items()):
        save_settings(database,changes)
    return {'models':len(patch['models']),'upstreams':len(patch['upstreams']),'issues':issues}
