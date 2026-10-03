"""Reviewed Scene relations share the canonical SQLite transaction."""

import json
import sqlite3
from pathlib import Path
from contextlib import closing

from ..core.store import Store, encode


class RelationConnection(sqlite3.Connection):
    def commit(self):
        if self.in_transaction:
            project_relations(self)
        super().commit()


def connect_relations(database):
    conn=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=rw',uri=True,timeout=10,factory=RelationConnection)
    conn.row_factory=sqlite3.Row
    return conn


def project_relations(conn):
    from .germany.scene_linker import SceneEdgeStore, SCENE_RELATION_TYPES
    from .scenes import scene_payload
    store=Store.__new__(Store);store.conn=conn
    for raw,target,field in (('scene_edges','scene_relations','edge_id'),('scene_edge_proposals','scene_proposals','proposal_id')):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(raw,)).fetchone():continue
        for row in conn.execute('SELECT * FROM '+raw).fetchall():
            meta=dict(row)
            if raw=='scene_edges':
                # An interrupted early import may have unprojected generic links.
                # They must not enter the five-type projection or block repair of
                # another edge; repair retains and retires the raw originals.
                if (row['active'] and row['linker_version']=='legacy-edge-import-v1'
                    and row['accepted_by']=='legacy_migration' and row['relation_type'] not in SCENE_RELATION_TYPES):continue
                previous=conn.execute('SELECT metadata_json FROM scene_relations WHERE id=?',(row[field],)).fetchone()
                if row['active'] and (previous is None or previous[0]!=encode(meta)):
                    owners=[store.read(row[name+'_scene_id']) for name in ('source','target')]
                    error=SceneEdgeStore._current_edge_error(meta,*(scene_payload(doc) if doc else None for doc in owners))
                    if error:raise ValueError('Scene edge changed before commit: '+error)
                conn.execute('INSERT INTO scene_relations VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'source_scene_id=excluded.source_scene_id,target_scene_id=excluded.target_scene_id,lifecycle=excluded.lifecycle,'
                    'active=excluded.active,metadata_json=excluded.metadata_json',
                    (row[field],'germany_scene_linker',row['source_scene_id'],row['target_scene_id'],row['lifecycle_status'],row['active'],encode(meta)))
            else:
                conn.execute('INSERT INTO scene_proposals VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'source_scene_id=excluded.source_scene_id,target_scene_id=excluded.target_scene_id,status=excluded.status,metadata_json=excluded.metadata_json',
                    (row[field],'germany_scene_linker',row['source_scene_id'],row['target_scene_id'],row['status'],encode(meta)))


def seed_relations(database,source_database):
    from .germany.scene_linker import SceneEdgeProposalStore
    SceneEdgeProposalStore({'serein_database':str(database)})
    with closing(sqlite3.connect(Path(source_database).resolve().as_uri()+'?mode=ro',uri=True)) as source, closing(connect_relations(database)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for table in ('scene_edges','scene_edge_proposals'):
                if conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():raise ValueError('Relation transplant requires empty tables')
                cursor=source.execute('SELECT * FROM '+table)
                columns=[row[0] for row in cursor.description]
                conn.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',cursor.fetchall())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
