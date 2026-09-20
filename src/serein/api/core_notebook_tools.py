"""Self-use notebook calls for core-only snapshots without legacy diary tables."""
import json
from uuid import uuid4

from ..compat.diaries import Diaries
from ..compat.germany.diary_store import _normalize_date, _normalize_unlock_at
from ..core.store import Store
from ..core.writer import Writer


def invoke(database, method, *args, **kwargs):
    class CoreDiaries(Diaries):
        def _comments(self, conn, diary_id):
            return [json.loads(row[0]) for row in conn.execute(
                'SELECT metadata_json FROM diary_comments WHERE entry_id=? ORDER BY created_at,id', (diary_id,))]

    view = CoreDiaries(database)

    def entry(store, key):
        row = store.conn.execute('SELECT metadata_json FROM diary_entries WHERE id=?', (key,)).fetchone()
        return view._public_entry(store.conn, json.loads(row[0]))

    if method == 'read':
        key, day = kwargs.get('diary_id'), kwargs.get('date', '')
        clauses = ["visibility<>'deleted'", "deleted_at=''"]
        values = []
        if key is not None and key > 0:
            clauses.append('id=?');values.append(key)
        if day.strip():
            clauses.append('day=?');values.append(_normalize_date(day))
        values.append(max(1, min(kwargs.get('limit') or 20, 100)))
        with Store(database, read_only=True) as store:
            rows = store.conn.execute('SELECT id FROM diary_entries WHERE '+' AND '.join(clauses)
                                      +' ORDER BY day DESC,id DESC LIMIT ?', values).fetchall()
            items = [entry(store, row[0]) for row in rows]
        result = {'status': 'ok', 'count': len(items), 'date': day.strip(), 'query_title': '', 'diaries': items}
        if items:
            result.update(items[0])
            result.update(status='ok', count=len(items), diaries=items, query_title='')
        return result

    with Writer(database) as writer, writer.store.transaction(immediate=True):
        store = writer.store
        key = args[0] if args else None
        old = writer._notebook(key) if key is not None else None
        if method == 'create':
            unlock = _normalize_unlock_at(kwargs['unlock_at'])
            request = {'kind': 'darkroom' if unlock else 'diary', 'author': kwargs['author'],
                       'day': _normalize_date(kwargs['date'], default_today=True), 'body_md': kwargs['content'].strip(),
                       'title': kwargs['title'].strip(), 'unlock_at': unlock}
            action = 'diary_save'
        elif method == 'revise':
            request = {'entry_id': key, 'expected_revision': old['revision'], 'kind': old['kind'],
                       'author': old['author'], 'body_md': kwargs['content'].strip(),
                       'day': old['day'] if kwargs['date'] is None else _normalize_date(kwargs['date']),
                       'title': old['title'] if kwargs['title'] is None else kwargs['title'].strip() or None}
            action = 'diary_save'
        elif method == 'comment':
            request = {'entry_id': key, 'author': kwargs['author'], 'body_md': kwargs['content'].strip()}
            action = 'diary_comment'
        elif method == 'delete':
            request = {'entry_id': key, 'expected_revision': old['revision']}
            action = 'diary_delete'
        else:
            raise ValueError('Unknown diary operation')
        result = writer.execute('notebook:' + uuid4().hex, action, request)
        if method == 'delete':
            deleted_at = store.conn.execute('SELECT deleted_at FROM diary_entries WHERE id=?', (key,)).fetchone()[0]
            return {'status': 'deleted', 'diary_id': key, 'deleted_at': deleted_at, 'recoverable': True}
        if method == 'comment':
            row = store.conn.execute('SELECT metadata_json FROM diary_comments WHERE id=?', (result['id'],)).fetchone()
            return {'status': 'commented', **json.loads(row[0])}
        return {**entry(store, int(result['id'])), 'status': 'revised' if method == 'revise' else 'created'}
