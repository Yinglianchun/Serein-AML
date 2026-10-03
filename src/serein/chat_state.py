"""Window-scoped upstream delivery ledger for the public chat host."""
import json
from .core.store import Store, encode, now

# Verified host policy: suppress cards delivered in the previous five successful
# upstream responses. Selection itself still uses the unchanged core contract.
COOLDOWN_TURNS = 5


def recent_deliveries(database, window_id):
    with Store(database, read_only=True) as store:
        rows = store.conn.execute("SELECT payload FROM host_deliveries WHERE json_extract(payload,'$.window_id')=? "
            "AND json_extract(payload,'$.reported_by')='serein_chat_proxy' ORDER BY id DESC LIMIT ?",
            (window_id, COOLDOWN_TURNS)).fetchall()
    return list(dict.fromkeys(key for row in rows for key in json.loads(row[0])['delivered_ids']))


def record_delivery(database, window_id, receipt_id, ids, *, query='', observation_id=None, memory_items=()):
    with Store(database) as store, store.transaction():
        payload = {'receipt_id': receipt_id, 'window_id': window_id, 'delivered_ids': ids,
                   'reported_by': 'serein_chat_proxy', 'delivery_target': 'upstream_model', 'created_at': now(),
                   'query':query, 'observation_id':observation_id, 'gateway_memory_items':list(memory_items)}
        store.conn.execute('INSERT INTO host_deliveries(receipt_id,payload) VALUES (?,?)', (receipt_id, encode(payload)))
