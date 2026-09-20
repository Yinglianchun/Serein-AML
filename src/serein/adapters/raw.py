"""Full local transcript archive; no recent-message table or host discovery."""
from ..core.store import Store, digest, encode


class RawMessages:
    def __init__(self, database):
        self.database = database

    def list(self, after_id=0, limit=20):
        if after_id < 0 or not 1 <= limit <= 100:
            raise ValueError('Invalid source pagination')
        with Store(self.database, read_only=True) as store:
            rows = store.conn.execute('SELECT id,session_id,role,text,created_at FROM raw_events '
                'WHERE id>? ORDER BY id LIMIT ?', (after_id,limit)).fetchall()
            return {'items':[{**dict(r),'preview':r['text'][:240],'text':None} for r in rows],
                    'next_after_id':rows[-1]['id'] if rows else after_id,'cursor_persisted':False}

    def read(self, message_ids):
        if not 1 <= len(message_ids) <= 20:
            raise ValueError('Read 1..20 selected original messages')
        with Store(self.database, read_only=True) as store:
            result=[]
            for key in dict.fromkeys(message_ids):
                row=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(key,)).fetchone()
                if row is None:
                    raise ValueError('Original message not found')
                result.append({'source_key':encode([row['source'],row['session_id'],row['source_event_id'] or str(key)]),
                    'content':row['text'],'metadata':{'source_system':row['source'],'session_id':row['session_id'],
                    'message_id':row['source_event_id'] or str(key),'raw_id':key,'role':row['role'],
                    'created_at':row['created_at'],'content_sha256':digest(row['text'])}})
            return result
