"""Original Diary/Comment API with atomic Serein notebook projections."""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock

from ..core.store import Store, Conflict, encode
from .germany.diary_store import DiaryStore


class DiaryConnection(sqlite3.Connection):
    project = False

    def commit(self):
        if self.project and self.in_transaction:
            project_diaries(self)
        super().commit()


class Diaries(DiaryStore):
    def __init__(self, database, *, initialize=False):
        self.db_path = Path(database)
        self._lock = RLock()
        self._project = not initialize
        if initialize:
            with Store(database,read_only=True):
                pass
            self._init_database()
            with self._connection() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS diary_projection_pending(id INTEGER PRIMARY KEY)')
                for table, field in (('diaries','id'),('comments','diary_id'),('diary_revisions','diary_id')):
                    for event, prefix in (('INSERT','NEW'),('UPDATE','NEW'),('DELETE','OLD')):
                        conn.execute(f'CREATE TRIGGER IF NOT EXISTS diary_projection_{table}_{event} AFTER {event} ON {table} '
                                     f'BEGIN INSERT OR IGNORE INTO diary_projection_pending VALUES ({prefix}.{field}); END')
            self._project=True

    def _connect(self):
        conn=sqlite3.connect(self.db_path.resolve().as_uri()+'?mode=rw',uri=True,timeout=15,factory=DiaryConnection)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.project=self._project
        return conn

    def _public_entry(self, conn, row, *, include_comments=True):
        item=super()._public_entry(conn,row,include_comments=include_comments)
        if row['entry_type']=='darkroom' and row['visibility']!='active':
            item.update(content='',comments=[],metadata={},body_available=False)
        return item


def write_in_store(store, action, request):
    """Use the canonical diary writer inside the caller's retry transaction."""
    class TransactionDiaries(Diaries):
        @contextmanager
        def _connection(self):
            # Writer owns the lock, commit and rollback, including the receipt.
            yield store.conn

    diaries = TransactionDiaries(store.path)
    entry_id = request.get('entry_id')
    if action in {'diary_delete', 'diary_save'} and entry_id is not None:
        old = store.conn.execute('SELECT * FROM diaries WHERE id=?', (entry_id,)).fetchone()
        if old is None or request.get('expected_revision') != old['revision']:
            raise Conflict('Editing requires the current diary revision')
        if action == 'diary_save' and (request['kind'] != old['entry_type'] or request['author'] != old['author']):
            raise Conflict('Editing preserves diary kind and author')
    if action == 'diary_save':
        kind, author, day = request['kind'], request['author'], request['day']
        if kind not in {'diary', 'darkroom'} or author not in {'ai', 'user'}:
            raise ValueError('Notebook requires diary/darkroom and explicit ai/user author')
        date.fromisoformat(day)
        unlock = request.get('unlock_at')
        if unlock and datetime.fromisoformat(unlock).tzinfo is None:
            raise ValueError('unlock_at must include a timezone')
        if entry_id is None:
            if kind == 'darkroom' and (not unlock or datetime.fromisoformat(unlock) <= datetime.now(timezone.utc)):
                raise ValueError('A new darkroom entry requires a future unlock time')
            entry = diaries.create(content=request['body_md'], date=day, title=request.get('title'),
                                   author=author, entry_type=kind, unlock_at=unlock or '')
        else:
            entry = diaries.revise(entry_id, content=request['body_md'], date=day,
                                   title=request.get('title'), unlock_at=unlock)
        result = {'id': str(entry['id']), 'kind': entry['entry_type'], 'revision': entry['revision'], 'status': 'saved'}
    elif action == 'diary_comment':
        comment = diaries.comment(entry_id, content=request['body_md'], author=request['author'])
        result = {'id': comment['id'], 'entry_id': entry_id, 'status': 'saved'}
    elif action == 'diary_delete':
        diaries.delete(entry_id)
        result = {'id': str(entry_id), 'kind': old['entry_type'], 'revision': old['revision'] + 1, 'status': 'deleted'}
    else:
        raise ValueError('Unknown diary write action')
    project_diaries(store.conn)
    return result


def project_diaries(conn):
    for key, in conn.execute('SELECT id FROM diary_projection_pending').fetchall():
        row=conn.execute('SELECT * FROM diaries WHERE id=?',(key,)).fetchone()
        if row is None:
            # The canonical row may have been removed by an older migration or
            # an explicit hard-delete.  A stale derived row must not keep every
            # later Diary write from committing.
            conn.execute('DELETE FROM diary_entries WHERE id=?',(key,))
            conn.execute('DELETE FROM diary_comments WHERE entry_id=?',(key,))
            conn.execute('DELETE FROM diary_sessions WHERE entry_id=?',(key,))
            continue
        row=dict(row)
        values=(row['id'],row['entry_type'],row['revision'],row['author'],row['date'],row['title'],
                row['content'],row['visibility'],row['unlock_at'],row['deleted_at'],row['created_at'],
                row['updated_at'],row['source_id'],encode(row))
        conn.execute('INSERT INTO diary_entries VALUES ('+','.join('?' for _ in values)+') ON CONFLICT(id) DO UPDATE SET '
            'kind=excluded.kind,revision=excluded.revision,author=excluded.author,day=excluded.day,title=excluded.title,'
            'body_md=excluded.body_md,visibility=excluded.visibility,unlock_at=excluded.unlock_at,deleted_at=excluded.deleted_at,'
            'updated_at=excluded.updated_at,source_id=excluded.source_id,metadata_json=excluded.metadata_json',values)
        conn.execute('DELETE FROM diary_comments WHERE entry_id=?',(key,))
        for ref in conn.execute('SELECT * FROM comments WHERE diary_id=?',(key,)).fetchall():
            conn.execute('INSERT INTO diary_comments VALUES (?,?,?,?,?,?)',
                         (ref['id'],key,ref['author'],ref['content'],ref['created_at'],encode(dict(ref))))
        for ref in conn.execute('SELECT * FROM diary_revisions WHERE diary_id=?',(key,)).fetchall():
            conn.execute('INSERT OR IGNORE INTO diary_history VALUES (?,?,?,?,?)',
                         (ref['id'],key,ref['revision'],ref['content'],encode(dict(ref))))
    conn.execute('DELETE FROM diary_projection_pending')
    for row in conn.execute('SELECT * FROM darkroom_sessions').fetchall():
        conn.execute('INSERT INTO diary_sessions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'entry_id=excluded.entry_id,locked_at=excluded.locked_at,unlock_at=excluded.unlock_at,metadata_json=excluded.metadata_json',
            (row['id'],row['diary_id'],row['locked_at'],row['unlock_at'],encode(dict(row))))


def seed_diaries(database, source_database):
    diaries=Diaries(database,initialize=True)
    with sqlite3.connect(Path(source_database).resolve().as_uri()+'?mode=ro',uri=True) as source, diaries._connection() as conn:
        if conn.execute('SELECT 1 FROM diaries LIMIT 1').fetchone():
            raise ValueError('Diary transplant requires empty destination Diary tables')
        conn.execute('BEGIN IMMEDIATE')
        for table in ('diaries','comments','diary_revisions','darkroom_sessions'):
            cursor=source.execute('SELECT * FROM '+table)
            columns=[column[0] for column in cursor.description]
            conn.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',cursor.fetchall())
    return diaries.stats()
