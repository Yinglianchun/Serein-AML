"""Deterministic legacy graph adapters. Imported links are not model evidence."""
import json
import sqlite3
from contextlib import closing


def endpoints(row):
    return (row.get('source') or row.get('source_scene_id') or row.get('source_id'),
            row.get('target') or row.get('target_scene_id') or row.get('target_id'))


def scan_edges(root):
    edges=[];sources=[];errors=[]
    file=root/'state'/'memory_edges.jsonl'
    if file.is_file():
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            errors.append({'path':'state/memory_edges.jsonl','error':'旧边文件链接超出输入目录'})
        else:
            count=0
            try:
                for number,line in enumerate(file.read_text('utf-8-sig').splitlines(),1):
                    if not line.strip():continue
                    try:
                        row=json.loads(line)
                        if not isinstance(row,dict) or not all(endpoints(row)):raise ValueError()
                        if not row.get('source') or not row.get('target'):
                            row={**row,'source':endpoints(row)[0],'target':endpoints(row)[1]}
                        edges.append(row);count+=1
                    except (ValueError,TypeError):errors.append({'path':'state/memory_edges.jsonl','line':number,'error':'边格式错误'})
                sources.append({'path':'state/memory_edges.jsonl','format':'jsonl','count':count})
            except (OSError,UnicodeError):errors.append({'path':'state/memory_edges.jsonl','error':'旧边文件无法读取'})
    # Inspect only databases directly in the selected root/state, never backup trees.
    for directory in (root,root/'state'):
        if not directory.is_dir():continue
        for file in sorted(directory.iterdir()):
            if not file.is_file() or file.suffix.lower() not in {'.db','.sqlite','.sqlite3'}:continue
            relative=file.relative_to(root).as_posix()
            if file.is_symlink() or not file.resolve().is_relative_to(root):
                errors.append({'path':relative,'error':'状态库链接超出输入目录'});continue
            try:
                with closing(sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
                    conn.row_factory=sqlite3.Row
                    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    # scene_relations can be a projection of scene_edges; never import both.
                    table=next((name for name in ('scene_edges','scene_relations','memory_edges') if name in tables),None)
                    if not table:continue
                    columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
                    if not ({'source','target'}<=columns or {'source_scene_id','target_scene_id'}<=columns
                            or {'source_id','target_id'}<=columns):raise ValueError()
                    count=0
                    for record in conn.execute('SELECT * FROM '+table):
                        row=dict(record)
                        if row.get('metadata_json'):
                            metadata=json.loads(row['metadata_json'])
                            if not isinstance(metadata,dict):raise ValueError()
                            row={**metadata,**row}
                        source,target=endpoints(row)
                        if not source or not target:raise ValueError()
                        edges.append({**row,'source':str(source),'target':str(target),'legacy_path':relative,'legacy_table':table})
                        count+=1
                    sources.append({'path':relative,'format':table,'count':count})
            except (sqlite3.Error,ValueError,OSError):
                errors.append({'path':relative,'error':'状态库或关系表无法读取，请核对备份格式'})
    return edges,sources,errors


VERSION = 'legacy-edge-import-v2'
OLD_VERSION = 'legacy-edge-import-v1'
OLD_REASON_MARKER = '旧记录：'

# The old classifier's updates points from the new memory to the old memory.
# Other legacy labels have broader meanings than the five Scene relations.
HELD_REASONS = {
    'triggers':'触发不一定是同一经历的延续',
    'causes':'因果不一定是同一经历的延续',
    'precedes':'仅时间先后不能确定同一经历',
    'context_of':'前情可能只是泛背景',
    'same_event':'同一事件可能是重复记载而非后续',
    'next_context':'上下文相邻不能确定同一经历',
    'previous_context':'上下文相邻不能确定同一经历',
    'reflects_on':'反思不能确定是回响还是反差',
    'evidenced_by':'旧证据来源不一定是具体经历对判断的佐证',
    'contradicts':'事实冲突不一定是经历反差',
    'supports':'旧支持包含泛主题和共享锚点，不能确定直接佐证',
    'promises':'承诺本身不能证明后续履行',
    'blocks':'阻碍本身不能证明后来解决',
    'belongs_to':'五种关系没有归属类型',
    'emotional_echo':'相同情绪不能证明具体经历回响',
    'relates_to':'泛关联不能确定五种关系之一',
    'related_to':'早期泛关联不是五种关系之一',
}


def conversion(old):
    """Return (type, direction, reverse) or a reason to retain the original only."""
    relation=str(old.get('relation_type') or old.get('relation') or '').strip()
    direction=str(old.get('directionality') or '').strip()
    if relation=='updates' and direction in ('','directed'):
        return ('continues','directed',True), 'updates：后续补充转延续，交换两端'
    if relation in {'echoes','contrasts_with'} and direction in ('','symmetric'):
        return (relation,'symmetric',False), '保留同名对称关系'
    if relation in {'continues','resolves','evidenced_by'} and direction=='directed':
        return (relation,'directed',False), '保留同名且方向明确的关系'
    return None, HELD_REASONS.get(relation,'关系类型或方向无法明确对应')


def inactive(old):
    return (str(old.get('active',1)).lower() in {'0','false'}
            or str(old.get('lifecycle_status') or old.get('lifecycle') or 'active')!='active'
            or str(old.get('status') or 'accepted') in {'pending','rejected','cancelled','archived','superseded'})


def initialize_edges(database):
    from ..compat.germany.scene_linker import _ensure_scene_edge_schema
    # Schema preparation must also work for an early interrupted import whose
    # raw edges have not yet been projected into scene_relations.
    with closing(sqlite3.connect(database)) as conn:
        _ensure_scene_edge_schema(conn);conn.commit()


def save_edge(database, source, target, old, *, endpoint_reason=''):
    """Store the original and conversion outcome atomically with the new edge."""
    from ..compat.germany.scene_linker import _scene_edge_id, _scene_hash, _now_utc
    from ..compat.relation_storage import connect_relations
    from ..core.store import encode,digest
    left,right=(source or {}).get('id',''),(target or {}).get('id','')
    key=digest(encode([left,right,old]))
    mapped,reason=conversion(old)
    result={'status':'held','reason':reason,'original':old,'version':VERSION}
    now=_now_utc()
    with closing(connect_relations(database)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''CREATE TABLE IF NOT EXISTS legacy_edge_imports(
            id TEXT PRIMARY KEY,source_id TEXT,target_id TEXT,original_json TEXT NOT NULL,
            result_json TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        previous=conn.execute('SELECT result_json FROM legacy_edge_imports WHERE id=?',(key,)).fetchone()
        if previous and json.loads(previous[0]).get('version')==VERSION:
            return {**json.loads(previous[0]),'repeated':True}
        # Identify the earlier importer by host marker, exact original, and endpoints.
        prior=[]
        for row in conn.execute("SELECT * FROM scene_edges WHERE linker_version=? AND accepted_by='legacy_migration'",
                                (OLD_VERSION,)).fetchall():
            try: original=json.loads(row['reason'].split(OLD_REASON_MARKER,1)[1])
            except (ValueError,IndexError):continue
            if original==old and {row['source_scene_id'],row['target_scene_id']}=={left,right}:prior.append(row)
        if prior:result['previous_edges']=[dict(row) for row in prior]
        if endpoint_reason or inactive(old) or not left or not right or left==right:
            result.update(status='skipped',reason=endpoint_reason or '已取消或缺少有效 Scene 端点')
        elif any(not row['active'] or row['lifecycle_status']!='active' for row in prior):
            result.update(status='skipped',reason='旧导入边已停用，不自动恢复')
        elif any(row['source_hash']!=_scene_hash(source if row['source_scene_id']==left else target)
                 or row['target_hash']!=_scene_hash(source if row['target_scene_id']==left else target) for row in prior):
            result.update(reason='早期导入后正文已改变，保留原记录待核对')
        elif mapped:
            relation,direction,reverse=mapped
            if reverse:source,target=target,source
            if direction=='symmetric' and target['id']<source['id']:source,target=target,source
            identifier=_scene_edge_id(source['id'],target['id'],relation)
            existing=conn.execute('SELECT * FROM scene_edges WHERE edge_id=?',(identifier,)).fetchone()
            if existing and not any(row['edge_id']==identifier for row in prior):
                result.update(status='skipped',reason='目标关系已存在，保留其审核或停用状态',edge_id=identifier)
            else:
                # A same-ID v1 edge is retired before replacing its payload. Its
                # original record remains in legacy_edge_imports and the report.
                if existing:conn.execute('DELETE FROM scene_edges WHERE edge_id=?',(identifier,))
                conn.execute('''INSERT INTO scene_edges
                    (edge_id,source_scene_id,target_scene_id,relation_type,directionality,confidence,reason,
                     source_evidence,target_evidence,source_hash,target_hash,proposal_id,linker_version,
                     active,accepted_at,accepted_by,lifecycle_status,updated_at)
                    VALUES (?,?,?,?,?,0,?,'','',?,?,?,?,1,?,'legacy_migration','active',?)''',
                    (identifier,source['id'],target['id'],relation,direction,
                     '旧库程序转换；未重新论证正文。'+reason+'。旧记录：'+encode(old),
                     _scene_hash(source),_scene_hash(target),'legacy_import_'+identifier,VERSION,now,now))
                result.update(status='done',edge_id=identifier,relation_type=relation,
                              source=source['id'],target=target['id'],evidence_origin='legacy_graph')
        # Withdraw only matching active v1 imports; retain all original rows/history.
        for row in prior:
            if row['active'] and not (result['status']=='done' and result['edge_id']==row['edge_id']):
                conn.execute("UPDATE scene_edges SET active=0,lifecycle_status='replaced',deactivated_at=?,"
                             "deactivated_by='legacy_migration',deactivation_reason='legacy_conversion_v2',updated_at=? WHERE edge_id=?",
                             (now,now,row['edge_id']))
        conn.execute('INSERT OR REPLACE INTO legacy_edge_imports VALUES (?,?,?,?,?,?)',
                     (key,left,right,encode(old),encode(result),now))
        conn.commit()
    return result
