"""Existing Dream model rules over retained SQLite works and atomic surfacing."""

import json
from pathlib import Path
from datetime import datetime, timezone

from ..core.store import Store, digest, encode, now
from .background import germany_config
from .germany.dream_engine import DreamEngine, DreamRecord


class Dreams(DreamEngine):
    def _dream_prompt(self):
        from ..deployment import read_settings
        prompt = read_settings(self.database)['dream']['main_prompt'].strip()
        return (prompt+'\n\n以下是本次梦境写作任务：\n\n' if prompt else '') + super()._dream_prompt()

    def _payload_for(self, *args, **kwargs):
        from ..deployment import identity
        self.identity = identity(self.database)
        return super()._payload_for(*args, **kwargs)

    async def run_due(self, *args, **kwargs):
        from ..deployment import read_settings
        self.daily_probability = float(read_settings(self.database)['dream']['daily_probability'])
        return await super().run_due(*args, **kwargs)

    def __init__(self, settings):
        self.database = settings.database
        super().__init__(germany_config(settings))
        from ..deployment import task_model
        if task_model(settings.database,'dreams'):
            from ..model_runtime import TaskClient
            self.client=TaskClient(settings.database,'dreams')

    def _claim_generation_attempt(self, date_key, *, force=False):
        # Serialize competing workers before recording the first request of the day.
        with Store(self.database) as store:
            store.conn.execute('BEGIN IMMEDIATE')
            existing = store.conn.execute(
                "SELECT 1 FROM historical_work_events WHERE event IN "
                "('generation_started','generated','probability_skipped') "
                "AND json_extract(metadata_json,'$.local_date')=? LIMIT 1", (date_key,)).fetchone()
            if existing and not force:
                return False
            self.log_event(store, 'generation_started', {
                'local_date': date_key, 'started_at': now(), 'manual': force})
            store.conn.commit()
        return True

    @staticmethod
    def record(row):
        return DreamRecord(json.loads(row['metadata_json']), row['body_md'], Path(row['id']))

    def list_records(self):
        with Store(self.database, read_only=True) as store:
            return [self.record(row) for row in store.conn.execute(
                "SELECT * FROM historical_works WHERE kind='dream' AND id NOT IN (SELECT document_id FROM deletions) ORDER BY id")]

    def _read_events(self):
        with Store(self.database, read_only=True) as store:
            return [json.loads(row[0]) for row in store.conn.execute(
                'SELECT metadata_json FROM historical_work_events ORDER BY origin,line_number')]

    @staticmethod
    def save_record(store, metadata, body):
        body = body.strip()
        key = metadata['dream_id']
        store.conn.execute("INSERT INTO historical_works VALUES (?,'dream',1,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET body_md=excluded.body_md,body_sha256=excluded.body_sha256,metadata_json=excluded.metadata_json",
            (key,key,body,digest(body),encode(metadata),'serein-live',''))
        return DreamRecord(metadata, body, Path(key))

    def _write_record(self, metadata, body):
        with Store(self.database) as store, store.transaction():
            return self.save_record(store, metadata, body)

    @staticmethod
    def log_event(store, event, payload):
        sequence = store.conn.execute("SELECT COALESCE(MAX(line_number),0)+1 FROM historical_work_events WHERE origin='serein-live'").fetchone()[0]
        store.conn.execute('INSERT INTO historical_work_events VALUES (?,?,?,?,?)',
            ('serein-live',sequence,payload.get('dream_id',''),event,encode({'event':event,**payload})))

    def _log_event(self, event, payload):
        with Store(self.database) as store, store.transaction():
            self.log_event(store, event, payload)

    def _delete_record(self, record, reason, embedding_engine=None):
        with Store(self.database) as store, store.transaction():
            store.conn.execute('INSERT OR IGNORE INTO deletions VALUES (?,?,?)', (record.dream_id,now(),encode({'reason':reason})))
            self.log_event(store,'deleted',{'dream_id':record.dream_id,'generated_at':record.metadata.get('generated_at'),'deleted_at':now(),'reason':reason})
        if embedding_engine:
            embedding_engine.delete_embedding(record.dream_id)

    def claim_surface(self, record, retain, embedding_engine=None):
        with Store(self.database) as store:
            store.conn.execute('BEGIN IMMEDIATE')
            row=store.conn.execute("SELECT * FROM historical_works WHERE id=? AND kind='dream' AND id NOT IN (SELECT document_id FROM deletions)",(record.dream_id,)).fetchone()
            if row is None:
                return {'status':'skipped','reason':'record_missing'}
            current=self.record(row)
            if current.surfaced:
                return {'status':'skipped','reason':'already_claimed'}
            stamp=datetime.now(timezone.utc).isoformat(timespec='seconds')
            surfaced=self.save_record(store,{**current.metadata,'surfaced':True,'surfaced_at':stamp},current.body)
            self.log_event(store,'surfaced',{'dream_id':record.dream_id,'generated_at':current.metadata.get('generated_at'),'surfaced_at':stamp})
            if not retain:
                store.conn.execute('INSERT INTO deletions VALUES (?,?,?)',(record.dream_id,stamp,encode({'reason':'surfaced_one_shot'})))
                self.log_event(store,'deleted',{'dream_id':record.dream_id,'generated_at':current.metadata.get('generated_at'),'deleted_at':stamp,'reason':'surfaced_one_shot'})
            store.conn.commit()
        if not retain and embedding_engine:
            embedding_engine.delete_embedding(record.dream_id)
        return {'status':'injected','reason':'resonant','retained':bool(retain),'text':self._format_surface(surfaced),
            'dream_id':record.dream_id,'generated_at':surfaced.metadata.get('generated_at'),'surfaced_at':stamp,
            'source_bucket_ids':[str(key) for key in surfaced.metadata.get('source_bucket_ids',[]) if str(key or '').strip()][:5]}
