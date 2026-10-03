"""Convert the public Ombre JSONL room/revision format, not private diary tables."""
import base64
from datetime import datetime
import json

from ..core.store import digest, encode


def room_groups(files):
    groups = {}
    seen = set()
    for file in files:
        if file['path'] != 'darkroom/entries.jsonl':
            continue
        for line in base64.b64decode(file['bytes_b64'], validate=True).splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('暗房 JSONL 每行必须是对象')
            key = row.get('id')
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError('暗房 JSONL 含无效或重复条目 ID')
            seen.add(key)
            if not isinstance(row.get('note'), str):
                raise ValueError('暗房条目缺少原始 note 正文')
            try:
                if not isinstance(row.get('created_at'), str):
                    raise ValueError()
                datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                if int(row.get('revision') or 1) < 1:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ValueError('暗房条目缺少有效创建时间或修订号') from None
            visibility = row.get('visibility') or 'active'
            if visibility not in ('active', 'archived', 'retracted', 'deleted'):
                raise ValueError('暗房条目含未知可见状态')
            unlock_time(row)
            # The public reader chooses the last JSONL entry, not the greatest timestamp.
            group = row.get('room_id') or key
            if not isinstance(group, str):
                raise ValueError('暗房 room_id 必须是字符串')
            groups.setdefault(group, []).append(row)
    return groups


def unlock_time(row):
    value = row.get('locked_until') or ''
    if not value:
        return ''
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('暗房锁定时间无效，不能当作已解锁迁入') from None
    # Old public Darkroom treats naive dates as its LOCAL_TZ (Asia/Shanghai).
    return value if stamp.tzinfo else value + '+08:00'


def notebook_row(row):
    return {'date': row['created_at'][:10], 'title': None, 'content': row['note'], 'author': 'ai',
            'emotion_tags': encode(row.get('tags') or []), 'entry_type': 'darkroom',
            'visibility': row.get('visibility') or 'active', 'unlock_at': unlock_time(row),
            'revision': int(row.get('revision') or 1),
            'metadata': encode({'legacy_kind':'public_ombre_darkroom', 'legacy_entry_id':row['id'],
                                'legacy_room_id':row.get('room_id') or row['id'],
                                'legacy_previous_entry_id':row.get('previous_entry_id') or ''}),
            'created_at': row['created_at']}


def import_darkroom(store, files):
    groups = room_groups(files)
    result = {'rooms': 0, 'revisions': 0, 'id_map': {}, 'held': []}
    for room_id, rows in groups.items():
        latest = rows[-1]
        owners = {row['id']: store.conn.execute('SELECT id FROM diaries WHERE source_id=?',
                  ('legacy_darkroom:' + row['id'],)).fetchone() for row in rows}
        if any(owners.values()):
            # A canonical diary snapshot (including deletion) takes precedence.
            for key, owner in owners.items():
                if owner:
                    result['id_map'][key] = owner[0]
                else:
                    result['held'].append({'table': 'legacy_darkroom', 'id': key,
                        'reason': '此房间已有正式日记归属，未重复导入旧修订；完整原记录已保留'})
            continue
        head = notebook_row(latest)
        head.update(created_at=rows[0]['created_at'], updated_at=latest['created_at'],
                    deleted_at=latest.get('deleted_at') or (latest['created_at'] if head['visibility']=='deleted' else ''),
                    source_id='legacy_darkroom:' + latest['id'])
        # Reserve against both the writable table and the canonical reading projection.
        key = max(store.conn.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
                  for table in ('diaries', 'diary_entries')) + 1
        head['id'] = key
        columns = sorted(head)
        store.conn.execute('INSERT INTO diaries ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                           [head[name] for name in columns])
        result['rooms'] += 1
        for row in rows[:-1]:
            previous = notebook_row(row)
            previous.update(diary_id=key, reason='legacy_darkroom')
            previous['id'] = max(store.conn.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
                                 for table in ('diary_revisions', 'diary_history')) + 1
            columns = sorted(previous)
            store.conn.execute('INSERT INTO diary_revisions ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                               [previous[name] for name in columns])
            result['revisions'] += 1
        if head['unlock_at'] and head['visibility']=='active':
            session_id = max(store.conn.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
                             for table in ('darkroom_sessions', 'diary_sessions')) + 1
            store.conn.execute('INSERT INTO darkroom_sessions(id,locked_at,unlock_at,diary_id) VALUES (?,?,?,?)',
                               (session_id, latest['created_at'], head['unlock_at'], key))
        for row in rows:
            result['id_map'][row['id']] = key
            proof = {'origin': 'public_ombre_darkroom', 'room_id': room_id, 'entry_id': row['id']}
            values = (row['id'], key, digest(row['note']), encode(proof))
            if store.conn.execute('SELECT 1 FROM diary_source_refs WHERE id=?', (row['id'],)).fetchone():
                raise ValueError('暗房旧 ID 已有归属，未覆盖：' + row['id'])
            store.conn.execute('INSERT INTO diary_source_refs VALUES (?,?,?,?)', values)
    return result
