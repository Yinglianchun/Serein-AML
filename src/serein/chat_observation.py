"""Request observations are diagnostics, never recall delivery receipts."""
from uuid import uuid4

from .core.store import Store, encode, now


def recall_summary(result):
    route = result.get('routing') or {}
    return {
        'semantic_recall_debug': {key: route[key] for key in ('route', 'action', 'confidence', 'score', 'reason') if key in route},
        'prepared_items': [{key: card[key] for key in ('id', 'title', 'source_kind', 'score') if key in card}
                           for card in result.get('cards', [])],
        'recall_diagnostics': {key:result[key] for key in ('status','reason','suppressed','reranker_error','pre_cooldown_selected_refs') if key in result},
        'candidate_count': (result.get('candidate_retrieval') or {}).get('candidate_count', len(result.get('candidates', []))),
    }


class ChatObservation:
    def __init__(self, database):
        self.database = database
        self.receipt_id = 'chat:' + uuid4().hex
        self.id = None
        self.payload = {}

    def start(self, window_id, query, memory_enabled):
        self.payload = {'observation_version': 1, 'observation_revision': 1,
                        'updated_at': now(), 'receipt_id': self.receipt_id,
                        'reported_by': 'serein_chat_proxy', 'query': query,
                        'request_kind': 'user_turn' if query else 'tool_continuation',
                        'memory_enabled': memory_enabled, 'request_status': 'preparing',
                        'prepared_ids': [], 'injected_bucket_ids': [], 'prepared_items': []}
        # A tool continuation carries the original turn's context; it is not
        # another user recall to observe or count.
        if not query:
            return
        with Store(self.database) as store, store.transaction():
            self.id = store.conn.execute('INSERT INTO injection_debug(session_id,round_id,created_at,payload_json) VALUES (?,?,?,?)',
                                         (window_id, 0, now(), encode(self.payload))).lastrowid

    def prepared(self, selected, summary, *, recall_state, replayed):
        self.payload.update(summary)
        self.payload.update(prepared_ids=list(selected), recall_state=recall_state,
                            context_replayed=replayed, request_status='upstream_pending')
        self._save()

    def finish(self, status, *, reason=''):
        if self.id is None or self.payload.get('request_status') in ('completed', 'failed', 'interrupted'):
            return
        # Diagnostics survive failures; they are never successful-delivery receipts.
        self.payload.update(request_status=status, ended_at=now(), failure_reason=reason,
                            injected_bucket_ids=list(self.payload['prepared_ids']) if status == 'completed' else [])
        if status == 'completed':
            self.payload['completed_at'] = self.payload['ended_at']
        self.payload['recall_why_summary'] = {
            'injected': self.payload.get('prepared_items', []) if status == 'completed' else []}
        self._save()

    def _save(self):
        if self.id is None:
            return
        self.payload['observation_revision'] = self.payload.get('observation_revision', 0) + 1
        self.payload['updated_at'] = now()
        with Store(self.database) as store, store.transaction():
            store.conn.execute('UPDATE injection_debug SET payload_json=? WHERE id=?', (encode(self.payload), self.id))
