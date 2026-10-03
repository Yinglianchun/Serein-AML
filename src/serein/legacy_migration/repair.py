"""Repair only legacy relationships in an already imported public database."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..core.store import Store, encode
from ..deployment import read_settings
from ..compat.scenes import scene_payload
from .edges import OLD_REASON_MARKER, OLD_VERSION, VERSION, endpoints, save_edge


def backup(database, directory):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'before-edge-repair.db'
    temporary=path.with_suffix('.pending')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as src, \
            closing(sqlite3.connect(temporary)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('旧边补救前备份未通过完整性检查')
    temporary.replace(path)
    return path


def collect(database, plan=None):
    """Match imported source identities, never guess IDs from names or new hashes."""
    records=[];issues=[]
    with Store(database,read_only=True) as store:
        docs=[store.read(row[0]) for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene'")]
        imported={d['id']:d for d in docs if d['metadata'].get('import_format')=='ombre-legacy'}
        tables={row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'scene_edges' in tables:
            rows=store.conn.execute("SELECT * FROM scene_edges WHERE linker_version=? AND accepted_by='legacy_migration'",
                                    (OLD_VERSION,)).fetchall()
            for row in rows:
                try:
                    old=json.loads(row['reason'].split(OLD_REASON_MARKER,1)[1])
                    if not isinstance(old,dict) or not all(endpoints(old)):raise ValueError()
                except (ValueError,IndexError):
                    issues.append({'edge_id':row['edge_id'],'reason':'旧导入边未保存可解析的原记录，需要原始备份'});continue
                owners=[imported.get(row[name+'_scene_id']) for name in ('source','target')]
                ids={d['metadata']['legacy_id']:d['id'] for d in owners if d and d['metadata'].get('legacy_id')}
                left,right=endpoints(old)
                if left not in ids or right not in ids:
                    issues.append({'edge_id':row['edge_id'],'reason':'无法核对旧边与导入端点的身份，未修改'});continue
                records.append({'old':old,'left':ids[left],'right':ids[right]})
        if 'legacy_edge_imports' in tables:
            records.extend({'old':json.loads(row['original_json']),'left':row['source_id'],'right':row['target_id']}
                           for row in store.conn.execute('SELECT * FROM legacy_edge_imports'))
        if plan is not None:
            matched={};changed=set();unmatched=set()
            for item in plan['items']:
                if item['kind']!='scene':continue
                found=[d for d in imported.values() if d['metadata'].get('legacy_id')==item['old_id']
                       and d['metadata'].get('import_source_hash')==item['source_hash']]
                if len(found)!=1:
                    unmatched.add(item['old_id']);continue
                matched[item['old_id']]=found[0]['id']
                if found[0]['body_md']!=item['body']:changed.add(item['old_id'])
            for old in plan['edges']:
                left,right=endpoints(old)
                error=''
                if left not in matched or right not in matched:
                    error='未找到唯一且来源一致的已导入 Scene；需要先核对原始备份和导入记录'
                elif left in changed or right in changed:error='导入后正文已改变，保留原记录待核对'
                records.append({'old':old,'left':matched.get(left,''),'right':matched.get(right,''),'error':error})
            if unmatched:issues.append({'reason':'部分旧 Scene 未唯一匹配当前导入记录','old_ids':sorted(unmatched)})
    # Explicit source validation takes precedence over a recovered v1 record.
    unique={encode([r['left'],r['right'],r['old']]):r for r in records}
    return list(unique.values()),issues


def run_repair(settings, records, issues=()):
    from .edges import initialize_edges
    from .workflow import checkpoint
    directory=settings.database.parent/'migrations'/'edge-repair'/uuid4().hex
    saved_backup=backup(settings.database,directory)
    report={'version':VERSION,'backup':str(saved_backup),'issues':list(issues),'records':[]}
    path=directory/'report.json'

    def write_report():
        report['counts']=dict(Counter('repeated' if row.get('repeated') else row['status'] for row in report['records']))
        temporary=path.with_suffix('.pending');temporary.write_text(encode(report),'utf-8');temporary.replace(path)

    write_report()
    initialize_edges(settings.database)
    try:
        for position,record in enumerate(records):
            checkpoint('edge_repair',position,len(records))
            with Store(settings.database,read_only=True) as store:
                docs=[store.read(record[key]) if record[key] else None for key in ('left','right')]
            policies={d['key']:d['policy'] for d in read_settings(settings.database)['tagging']['domains']}
            error=record.get('error','')
            if not error and any(not d or d['lifecycle']!='active' or not d['manual_surface']
                                 or policies.get(d['metadata'].get('canonical_domain'))=='excluded' for d in docs):
                error='缺端点、归档、不浮现或排除主域'
            try:
                result=save_edge(settings.database,*(scene_payload(d) if d else None for d in docs),
                                record['old'],endpoint_reason=error)
            except Exception:
                report['records'].append({'status':'failed','original':record['old'],'reason':'写入失败，可重新执行补救'})
                raise
            report['records'].append(result)
            write_report()
    finally:write_report()
    return {**report,'report':str(path)}
