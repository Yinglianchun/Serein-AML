"""Pending source evidence, using public original reads and embedding profiles.

Incomplete or unselected dialogue stays available as original evidence. Originals
represented by settled Events never enter this fallback, including excluded Events.
"""
from contextlib import closing
import json
import sqlite3

from serein.adapters.embedding import EmbeddingClient
from serein.compat.originals import Originals, READABLE
from serein.core.store import Store
from serein.recall.index import unit_vector
from serein.recall.passages import slices


def pending(database):
    with Store(database, read_only=True) as store:
        names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "raw_events" not in names:
            return []
        condition = " AND NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=raw_events.id AND p.outcome='settled')" if "raw_processing" in names else ""
        return [dict(r) for r in store.conn.execute(
            "SELECT * FROM raw_events WHERE source='serein_aml' AND " + READABLE + condition + " ORDER BY id")]


def _tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS aml_original_vectors (
        raw_id INTEGER NOT NULL, stamp TEXT NOT NULL, ordinal INTEGER NOT NULL,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, embedding TEXT NOT NULL,
        PRIMARY KEY(raw_id,ordinal))""")


def fill(settings):
    if not settings.embedding:
        return {"embedded": 0}
    client = EmbeddingClient(settings.database, settings.index, **settings.embedding)
    rows = pending(settings.database)
    todo = []
    with closing(sqlite3.connect(settings.index)) as conn:
        _tables(conn)
        available = {(r[0], r[1]): r[2] for r in conn.execute("SELECT raw_id,ordinal,stamp FROM aml_original_vectors")}
        active = {r["id"] for r in rows}
        conn.executemany("DELETE FROM aml_original_vectors WHERE raw_id=?", [(key,) for key in {k[0] for k in available} if key not in active])
        for row in rows:
            conn.execute("DELETE FROM aml_original_vectors WHERE raw_id=? AND stamp!=?", (row["id"], row["event_hash"]))
            document = {"kind": "event", "body_md": row["text"]}
            spans = slices(document, maximum=4000, overlap=200, min_chars=4000) or [(0, len(row["text"]))]
            for ordinal, (start, end) in enumerate(spans):
                if available.get((row["id"], ordinal)) != row["event_hash"]:
                    todo.append((row, ordinal, start, end))
        conn.commit()
    for offset in range(0, len(todo), 16):
        batch = todo[offset:offset+16]
        vectors = client.documents([row["text"][start:end] for row, _, start, end in batch])
        if len(vectors) != len(batch):
            raise ValueError("Unexpected original embedding count")
        vectors = [unit_vector(vector, client.dimension) for vector in vectors]
        with closing(sqlite3.connect(settings.index)) as conn:
            conn.executemany("INSERT OR REPLACE INTO aml_original_vectors VALUES (?,?,?,?,?,?)",
                [(row["id"], row["event_hash"], ordinal, start, end, json.dumps(vector))
                 for (row, ordinal, start, end), vector in zip(batch, vectors)])
            conn.commit()
    return {"embedded": len(todo)}


def hits(settings, query, terms, limit=40):
    rows = {r["id"]: r for r in pending(settings.database)}
    if not rows:
        return []
    scores = {}
    # Literal original tools are case-sensitive. Bounded single query words also
    # cover evidence with separated names and predicates without reading recent history.
    for term in list(dict.fromkeys(terms))[:10]:
        result = Originals(settings.database).source_message_search(query=term, limit=50)
        for rank, item in enumerate(result["items"], 1):
            key = int(item["id"].split(":")[1])
            if key in rows:
                scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    if settings.embedding:
        client = EmbeddingClient(settings.database, settings.index, **settings.embedding)
        vector = unit_vector(client.query(query)["embedding"], client.dimension)
        with closing(sqlite3.connect(settings.index)) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='aml_original_vectors'").fetchone()
            cached = list(conn.execute("SELECT raw_id,stamp,embedding FROM aml_original_vectors")) if exists else []
        semantic = {}
        for key, stamp, encoded in cached:
            if key not in rows or rows[key]["event_hash"] != stamp:
                continue
            candidate = unit_vector(json.loads(encoded), client.dimension)
            cosine = sum(a*b for a, b in zip(vector, candidate))
            if cosine >= .3:
                semantic[key] = max(semantic.get(key, -1), cosine)
        for rank, (key, _) in enumerate(sorted(semantic.items(), key=lambda p: (-p[1], p[0]))[:limit], 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    selected = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    result = []
    # Canonical original read rechecks role, draft and discard visibility.
    for start in range(0, len(selected), 20):
        page = Originals(settings.database).source_message_read(ids=["raw:"+str(key) for key in selected[start:start+20]])
        for item in page["items"]:
            meta = item["metadata"]
            result.append({"id": item["id"], "kind": "original", "content": item["content"],
                           "score": scores[int(item["id"].split(":")[1])],
                           "created_at": meta["created_at"], "metadata": meta})
    return result
