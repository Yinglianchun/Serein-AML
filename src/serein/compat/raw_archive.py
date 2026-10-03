"""Existing raw transcript archive in the same live SQLite database."""

import sqlite3
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

from .germany.raw_events import RawEventStore


def raw_archive(settings):
    return RawEventStore({'raw_events': {'db_path': str(settings.database)}})


def seed_raw_archive(database, source_database):
    """Explicit transplant before enabling the existing Bridge retry queue."""
    archive = RawEventStore({'raw_events': {'db_path': str(database)}})
    with closing(sqlite3.connect(Path(source_database).resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(archive._connect()) as target:
        target.execute('BEGIN IMMEDIATE')
        try:
            if target.execute('SELECT 1 FROM raw_events LIMIT 1').fetchone():
                raise ValueError('Raw transcript transplant requires an empty destination')
            cursor = source.execute('SELECT * FROM raw_events ORDER BY id')
            columns = [c[0] for c in cursor.description]
            target.executemany('INSERT INTO raw_events ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', cursor)
            if any(tuple(new) != old for new, old in zip_longest(target.execute('SELECT * FROM raw_events ORDER BY id'), source.execute('SELECT * FROM raw_events ORDER BY id'))):
                raise ValueError('Raw transcript rows differ from source')
            sequence = source.execute("SELECT seq FROM sqlite_sequence WHERE name='raw_events'").fetchone()
            if sequence:
                target.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='raw_events'", (sequence[0],))
            if archive.fts_enabled:
                target.execute("INSERT INTO raw_events_fts(raw_events_fts) VALUES ('rebuild')")
            count = target.execute('SELECT count(*) FROM raw_events').fetchone()[0]
            target.commit()
            return {'status': 'ok', 'rows': count, 'original_rows_exact': True}
        except BaseException:
            target.rollback()
            raise
