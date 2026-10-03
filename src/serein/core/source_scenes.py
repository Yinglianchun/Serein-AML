"""Exact original bindings and complete authored Scene snapshots."""
import json
import re
from .store import Store, encode


def source_keys(entries):
    requested = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError('source key must be an object')
        parts = [raw.get(k) for k in ('source_system', 'session_id', 'message_id')]
        sha = raw.get('content_sha256')
        if any(not isinstance(p, str) or not p.strip() or len(p) > 160 for p in parts) or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('source key requires exact identity and content_sha256')
        key = encode(parts)
        if key in requested and requested[key] != sha:
            raise ValueError('conflicting source hashes')
        requested[key] = sha
    return requested


def read_source_scenes(conn, entries):
    """Caller holds a read snapshot or a write transaction throughout this query."""
    requested = source_keys(entries)
    matches = {}
    keys = sorted(requested)
    for offset in range(0, len(keys), 400):
        chunk = keys[offset:offset + 400]
        rows = conn.execute(
            'SELECT d.id,d.revision,d.lifecycle,r.title,r.body_md,r.body_sha256,s.source_key,s.content_sha256 '
            'FROM sources s JOIN evidence_bindings b ON b.source_id=s.id AND b.active=1 '
            "JOIN documents d ON d.id=b.document_id AND d.kind='scene' AND d.lifecycle IN ('active','archived') "
            'JOIN revisions r ON r.document_id=d.id AND r.number=d.revision '
            'WHERE s.source_key IN (' + ','.join('?' for _ in chunk) + ') ORDER BY d.id,s.source_key', chunk)
        for row in rows:
            if requested[row['source_key']] != row['content_sha256']:
                continue
            item = matches.setdefault(row['id'], {
                'scene_id': row['id'], 'revision': row['revision'], 'status': row['lifecycle'],
                'title': row['title'], 'body': row['body_md'], 'body_sha256': row['body_sha256'],
                'matched_sources': {}})
            item['matched_sources'][row['source_key']] = dict(
                zip(('source_system', 'session_id', 'message_id'), json.loads(row['source_key'])),
                content_sha256=row['content_sha256'])
    items = [matches[key] for key in sorted(matches)]
    if len(items) > 40 or sum(len(item['body']) for item in items) > 100_000:
        raise ValueError('Scene context exceeds complete reading budget; reduce the input batch')
    for item in items:
        item['matched_sources'] = [item['matched_sources'][key] for key in sorted(item['matched_sources'])]
    return items


def find_source_scenes(database, entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
        raise ValueError('source_keys requires 1..500 entries')
    with Store(database, read_only=True) as store:
        store.conn.execute('BEGIN')
        items = read_source_scenes(store.conn, entries)
    return {'items': items, 'count': len(items)}
