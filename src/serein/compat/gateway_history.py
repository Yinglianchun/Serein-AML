"""Historical Gateway records; actual current Hook delivery stays in Bridge."""

import sqlite3
from pathlib import Path
from contextlib import closing

from .germany.gateway_history import GatewayStateStore
from ..core.store import Store


class GatewayHistory(GatewayStateStore):
    def __init__(self,database):self.database=database

    def _connect(self):
        conn=sqlite3.connect(Path(self.database).resolve().as_uri()+'?mode=ro',uri=True)
        conn.row_factory=sqlite3.Row
        return conn


def seed_gateway_history(database,source_database):
    with closing(sqlite3.connect(Path(source_database).resolve().as_uri()+'?mode=ro',uri=True)) as source, Store(database) as store,store.transaction():
        if store.conn.execute('SELECT 1 FROM injection_debug LIMIT 1').fetchone():
            raise ValueError('Gateway history transplant requires an empty destination')
        store.conn.executemany('INSERT INTO injection_debug VALUES (?,?,?,?,?)',source.execute('SELECT id,session_id,round_id,created_at,payload_json FROM injection_debug').fetchall())
