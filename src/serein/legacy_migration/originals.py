"""Copy the legacy raw archive verbatim without scheduling model work."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from ..core.store import Store, encode
from .companion import source_config, database_path

COLUMNS = ('source','source_event_id','event_hash','role','text','created_at','ingested_at',
           'conversation_id','session_id','client','metadata_json')


def read_rows(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('旧原文库完整性检查失败')
        columns={r[1] for r in conn.execute('PRAGMA table_info(raw_events)')}
        if not {'id',*COLUMNS}<=columns:raise ValueError('旧原文库缺少 raw_events 必要字段')
        for row in conn.execute('SELECT id,'+','.join(COLUMNS)+' FROM raw_events ORDER BY id'):
            value=dict(row)
            if any(not isinstance(value[k],str) for k in COLUMNS):raise ValueError('旧原文库字段格式不完整，请核对备份')
            if not isinstance(json.loads(value['metadata_json']),dict):raise ValueError('旧原文元数据必须是对象')
            yield value


def scan_originals(root):
    root=Path(root).resolve();cfg=source_config(root);warnings=[]
    raw=cfg.get('raw_events') or {}
    if not isinstance(raw,dict):raise ValueError('旧 raw_events 配置必须是对象')
    state=str(cfg.get('state_dir') or 'state')
    path=database_path(root,raw.get('db_path'),(f'{state}/raw_events.sqlite','state/raw_events.sqlite'),warnings)
    sha=hashlib.sha256();count=0
    if path:
        for row in read_rows(path):sha.update(encode(row).encode('utf-8'));count+=1
    else:warnings.append('未找到旧原文库 state/raw_events.sqlite；请检查旧服务实际状态目录的备份。')
    return {'path':str(path) if path else '', 'fingerprint':sha.hexdigest(),
            'summary':{'messages':count,'path':path.relative_to(root).as_posix() if path else '', 'warnings':warnings}}


def import_originals(database, source):
    if not source['path']:return {'inserted':0,'duplicate':0,'held':[], 'messages':0}
    inserted=duplicate=0;held=[];sha=hashlib.sha256();count=0
    with Store(database) as store,store.transaction(immediate=True):
        store.conn.execute('CREATE TABLE IF NOT EXISTS raw_processing(raw_id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,outcome TEXT NOT NULL)')
        has_fts=store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_events_fts'").fetchone()
        for row in read_rows(source['path']):
            sha.update(encode(row).encode('utf-8'));count+=1
            matches=store.conn.execute("SELECT * FROM raw_events WHERE source=? AND (event_hash=? OR (source_event_id!='' AND source_event_id=?))",
                (row['source'],row['event_hash'],row['source_event_id'])).fetchall()
            if matches:
                # Same identity with changed prose/time is a conflict, never an overwrite.
                compare=[k for k in COLUMNS if k not in ('ingested_at','metadata_json','client')]
                if len(matches)==1 and all(matches[0][k]==row[k] for k in compare):duplicate+=1
                else:held.append({'old_id':row['id'],'reason':'同来源消息已存在但内容或身份字段不同，保留现有记录，请核对原备份'})
                continue
            key=store.conn.execute('INSERT INTO raw_events ('+','.join(COLUMNS)+') VALUES ('+','.join('?' for _ in COLUMNS)+')',
                                   [row[k] for k in COLUMNS]).lastrowid
            if has_fts:
                store.conn.execute('INSERT INTO raw_events_fts(rowid,text,source,conversation_id,session_id) VALUES (?,?,?,?,?)',
                    (key,row['text'],row['source'],row['conversation_id'],row['session_id']))
            # Archive for search/binding only; do not turn an old transcript into paid Event work.
            store.conn.execute('INSERT INTO raw_processing VALUES (?,?,?)',(key,'legacy-originals:'+source['fingerprint'],'archived_only'))
            inserted+=1
        if sha.hexdigest()!=source['fingerprint'] or count!=source['summary']['messages']:
            raise ValueError('预览后旧原文库发生变化，本次导入已回滚，请重新预览停止写入后的备份')
    return {'messages':count,'inserted':inserted,'duplicate':duplicate,'held':held,'automatic_processing':False}
