"""Explicit source-bound Scene lookups, independent of automatic Event generation."""
from ..core.source_scenes import read_source_scenes
from ..core.store import Store, digest, encode


def scene_keys(messages):
    return [{'source_system':m['source'], 'session_id':m['original_session_id'],
             'message_id':str(m.get('source_event_id') or m['id']),
             'content_sha256':digest(m['content'])} for m in messages]


def read_bound_scenes(database, messages, *, conn=None):
    keys = scene_keys(messages)
    if conn is None:
        with Store(database, read_only=True) as store:
            store.conn.execute('BEGIN')
            return read_bound_scenes(database, messages, conn=store.conn)
    ids = {}
    for message, key in zip(messages, keys):
        identity = encode([key[k] for k in ('source_system','session_id','message_id')])
        ids.setdefault(identity, set()).add(message['id'])
    items = read_source_scenes(conn, keys)
    for item in items:
        matched = item.pop('matched_sources')
        item['matched_source_message_ids'] = sorted({identifier for key in matched
            for identifier in ids[encode([key[k] for k in ('source_system','session_id','message_id')])]})
    return items
