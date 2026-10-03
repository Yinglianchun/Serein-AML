"""Compact original-message references for selected cards, without source prose."""

import json


def message_refs(evidence):
    groups = {}
    for row in evidence:
        meta = row.get('metadata') or {}
        identity = {k: str(meta[k]).strip() for k in
                    ('source_system', 'session_id', 'thread_id', 'message_id')
                    if meta.get(k) is not None and str(meta[k]).strip()}
        # The canonical source key retains identity for older sparse metadata.
        try:
            key = json.loads(row.get('source_key') or '')
        except (ValueError, TypeError):
            key = None
        if isinstance(key, list) and len(key) == 3:
            for name, value in (('source_system', key[0]), ('message_id', key[2])):
                if value is not None and str(value).strip():
                    identity.setdefault(name, str(value).strip())
            if not identity.get('session_id') and not identity.get('thread_id') and key[1]:
                identity['conversation_id'] = str(key[1])
        message_id = identity.pop('message_id', '')
        if not message_id:
            continue
        scope = tuple(sorted(identity.items()))
        group = groups.setdefault(scope, {**identity, 'message_ids': []})
        if message_id not in group['message_ids']:
            group['message_ids'].append(message_id)
    return list(groups.values())


def card_source_refs(hit, reader=None):
    if reader is None:
        return message_refs(hit['object'].get('evidence') or [])
    rows = reader.store.conn.execute(
        'SELECT s.source_key,s.metadata_json AS source_metadata,b.metadata_json AS binding_metadata '
        'FROM evidence_bindings b JOIN sources s ON s.id=b.source_id '
        'WHERE b.document_id=? AND b.active=1 ORDER BY b.id', (hit['id'],))
    return message_refs({'source_key': r['source_key'],
                         'metadata': {**json.loads(r['source_metadata']), **json.loads(r['binding_metadata'])}}
                        for r in rows)


def source_ref_lines(card):
    refs = card.get('source_refs')
    return ['source_refs: ' + json.dumps(refs, ensure_ascii=False, separators=(',', ':'))] if refs else []
