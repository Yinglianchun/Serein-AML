"""Legacy bucket comments become separate annotations, never Scene prose/evidence."""
from collections import Counter
import json
from pathlib import Path

from ..core.store import Store, digest, encode
from ..core.personal import Personal
from .companion import source_config


def capture_comments(meta):
    # YAML timestamp values need a JSON-safe representation without changing their text meaning.
    return json.loads(json.dumps(meta.get('comments',[]),ensure_ascii=False,default=str))


def comment_summary(items):
    return {'comments':sum(len(i['legacy_comments']) for i in items if isinstance(i.get('legacy_comments'),list)),
            'memories':sum(bool(i.get('legacy_comments')) for i in items)}


def author_role(raw,names):
    role=raw.get('role')
    if role in ('user','assistant','ai'):return 'assistant' if role=='ai' else role
    source=raw.get('source')
    if source=='dashboard':return 'user'
    if source in ('comment_bucket','annotate','hold(feel=True)'):return 'assistant'
    author=str(raw.get('author') or '')
    matches=[role for role,key in (('user','user_name'),('assistant','ai_name')) if names.get(key) and author==names[key]]
    return matches[0] if len(matches)==1 else 'unknown'


def import_comments(database,plan):
    cfg=source_config(Path(plan['root']))
    names=cfg.get('identity') if isinstance(cfg.get('identity'),dict) else {}
    inserted=existing=0;held=[]
    with Store(database) as store,store.transaction(immediate=True):
        targets={}
        for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene'").fetchall():
            doc=store.read(row['id']);meta=doc['metadata']
            if meta.get('import_format')=='ombre-legacy':targets.setdefault(meta.get('legacy_id'),[]).append(doc)
        for item in plan['items']:
            raw_comments=item.get('legacy_comments',[])
            if not raw_comments:continue
            # Keep the full old records, including kind/source/emotion fields and malformed entries.
            store.save_import_record('ombre-comments:'+plan['fingerprint'],item['path'],encode(raw_comments))
            if not isinstance(raw_comments,list):
                held.append({'old_id':item['old_id'],'reason':'comments 不是数组，原记录已保留'});continue
            owners=[d for d in targets.get(item['old_id'],[]) if d['metadata'].get('import_source_hash')==item['source_hash']]
            if len(owners)!=1 or owners[0]['lifecycle']=='deleted':
                held.append({'old_id':item['old_id'],'count':len(raw_comments),'reason':'未找到唯一且未删除的原 Scene，注脚未挂到其他记忆'});continue
            doc=owners[0]
            ids=Counter(str(r['id']) for r in raw_comments if isinstance(r,dict) and r.get('id'))
            for number,raw in enumerate(raw_comments):
                source_id=str(raw.get('id') or '') if isinstance(raw,dict) else ''
                if not isinstance(raw,dict) or (source_id and ids[source_id]>1):
                    held.append({'old_id':item['old_id'],'index':number,'reason':'旧评论格式错误或 ID 重复，原记录已保留'});continue
                legacy_key=source_id or 'position:'+str(number)+':'+digest(encode(raw))
                key='annotation_legacy_'+digest(encode([doc['id'],legacy_key]))
                if store.conn.execute("SELECT 1 FROM personal_records WHERE scope='annotation' AND key=?",(key,)).fetchone():
                    existing+=1;continue  # Existing edits and tombstones always win.
                role=author_role(raw,names)
                fallback=names.get('user_name' if role=='user' else 'ai_name','') if role!='unknown' else ''
                author=str(raw.get('author') or fallback)
                created=str(raw.get('original_feel_created') or raw.get('created') or raw.get('created_at') or '')
                value={'content':raw.get('content'),'author':author,'role':role,'createdAt':created}
                try:
                    if not isinstance(value['content'],str):raise ValueError('invalid content')
                    value=Personal.validate('annotation',key,value,doc['id'])
                except (ValueError,TypeError):
                    held.append({'old_id':item['old_id'],'index':number,'reason':'旧评论缺少有效正文或超过注脚长度限制，原记录已保留'});continue
                deleted=bool(raw.get('deleted') or raw.get('deleted_at') or raw.get('status')=='deleted')
                store.conn.execute('INSERT INTO personal_records VALUES (?,?,?,?,1,?,?,?)',
                    ('annotation',key,doc['id'],encode(value),int(deleted),created,created))
                inserted+=1
    return {'inserted':inserted,'existing':existing,'held':held,'models_called':False}
