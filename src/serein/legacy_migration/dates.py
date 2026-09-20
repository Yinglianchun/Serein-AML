"""Keep legacy occurrence dates separate from ingestion time."""
from datetime import date, datetime
import re

from ..core.store import Store


def valid_date(value):
    text=str(value or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])',text):return ''
    try:
        if len(text)>10:datetime.fromisoformat(text.replace('Z','+00:00'))
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:return ''


def legacy_dates(meta):
    created=next((str(meta[k]) for k in ('created','created_at','creation_date') if valid_date(meta.get(k))), '')
    at=valid_date(meta.get('date')) or valid_date(created)
    return {'date':at,'legacy_created':created}


def repair_dates(database, plan):
    changed=0;held=[]
    candidates={item['old_id']:item for item in plan['items'] if item['kind']=='scene'}
    with Store(database) as store,store.transaction(immediate=True):
        for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle!='deleted'").fetchall():
            doc=store.read(row['id']);meta=doc['metadata'];item=candidates.get(meta.get('legacy_id'))
            if not item or meta.get('import_format')!='ombre-legacy':continue
            if meta.get('import_source_hash')!=item['source_hash']:
                held.append({'id':doc['id'],'reason':'旧记录内容与首次迁入时不同，未修改日期'});continue
            if valid_date(meta.get('date')) or not item.get('date'):continue
            patch={**meta,'date':item['date']}
            if item.get('legacy_created'):patch['created']=item['legacy_created']
            store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],body_md=doc['body_md'],metadata=patch)
            store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(doc['id'],))
            if item.get('legacy_created'):
                store.conn.execute('UPDATE documents SET created_at=? WHERE id=?',(item['legacy_created'],doc['id']))
            changed+=1
    return {'updated':changed,'held':held}
