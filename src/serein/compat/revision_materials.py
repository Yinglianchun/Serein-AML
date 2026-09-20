"""Current, access-checked material cards for the revision inbox."""
from ..core.reader import Reader
from .narratives import RevisionInbox


def references(item):
    result = []
    for kind in ('event', 'scene', 'diary', 'darkroom', 'upload'):
        ids = item.get(f'source_{kind}_ids') or []
        if not ids and item.get('source_type') == kind and item.get('source_id'):
            ids = [item['source_id']]
        result.extend((kind, str(key)) for key in ids)
    return list(dict.fromkeys(result))


def read_materials(database, proposal_id, *, offset=0, limit=50, kind='', identifier=''):
    with Reader(database) as reader:
        reader.store.conn.execute('BEGIN')
        proposal = next((item for item in RevisionInbox(reader.store)._load()['items']
                         if item.get('proposal_id') == proposal_id), None)
        if proposal is None:
            return {'status':'not_found'}
        refs = references(proposal)
        if identifier:
            if (kind, identifier) not in refs:
                return {'status':'not_found'}
            return reader.read(identifier, kind=kind, with_evidence=False)
        items = []
        for source_kind, key in refs[offset:offset+limit]:
            result = reader.read(key, kind=source_kind, with_evidence=False)
            doc = result.get('document') or {}
            metadata = doc.get('metadata') or {}
            items.append({'kind':source_kind, 'id':key, 'readable':result['readable'],
                          'status':result['status'], 'title':doc.get('title') if result['readable'] else '',
                          'date':metadata.get('local_date') or metadata.get('date') or doc.get('created_at') or ''})
        return {'status':'ok', 'items':items, 'total':len(refs),
                'next_offset':offset+len(items) if offset+len(items)<len(refs) else None}
