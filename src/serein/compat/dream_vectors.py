"""Disposable Dream cue vectors using the same Germany embedding profile."""

import asyncio
import json
import sqlite3
from contextlib import closing

from ..adapters.embedding import EmbeddingClient
from ..core.store import Store, digest, encode


class DreamVectors:
    def __init__(self, settings):
        from ..configured_models import effective_settings
        settings=effective_settings(settings)
        self.settings = settings
        self.enabled = bool(settings.embedding)

    def client(self):
        return EmbeddingClient(self.settings.database,self.settings.index,**self.settings.embedding)

    async def _generate_embedding(self, query):
        return (await asyncio.to_thread(self.client().query,query))['embedding']

    async def generate_and_store(self, key, text):
        if not self.enabled:return
        client=self.client()
        vectors=await asyncio.to_thread(client.documents,[text])
        with closing(sqlite3.connect(self.settings.index)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS dream_vectors(id TEXT PRIMARY KEY,stamp TEXT NOT NULL,vector_json TEXT NOT NULL)')
            conn.execute('INSERT INTO dream_vectors VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET stamp=excluded.stamp,vector_json=excluded.vector_json',
                (key,digest(encode(client.profile)+text),encode(vectors[0])))
            conn.commit()

    async def get_embedding(self, key):
        with Store(self.settings.database,read_only=True) as store:
            row=store.conn.execute("SELECT metadata_json FROM historical_works WHERE id=? AND kind='dream' AND id NOT IN (SELECT document_id FROM deletions)",(key,)).fetchone()
        if not row:return None
        text='；'.join(json.loads(row[0]).get('recall_cues') or [])
        if not text:return None
        stamp=digest(encode(self.client().profile)+text)
        with closing(sqlite3.connect(self.settings.index)) as conn:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='dream_vectors'").fetchone()
            cached=conn.execute('SELECT stamp,vector_json FROM dream_vectors WHERE id=?',(key,)).fetchone() if exists else None
        if cached and cached[0]==stamp:return json.loads(cached[1])
        await self.generate_and_store(key,text)
        with closing(sqlite3.connect(self.settings.index)) as conn:
            return json.loads(conn.execute('SELECT vector_json FROM dream_vectors WHERE id=?',(key,)).fetchone()[0])

    def delete_embedding(self,key):
        if not self.enabled:return
        with closing(sqlite3.connect(self.settings.index)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='dream_vectors'").fetchone():
                conn.execute('DELETE FROM dream_vectors WHERE id=?',(key,))
                conn.commit()
