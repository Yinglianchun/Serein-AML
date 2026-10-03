"""Read only explicitly selected Bridge sessions; never scan Codex files."""

import sqlite3

from serein.core.store import digest, encode


class BridgeMessages:
    def __init__(self, database, session_ids, source_system):
        if not session_ids or any(type(i) is not int or i <= 0 for i in session_ids):
            raise ValueError("Message source requires explicit positive session IDs")
        self.database = database
        self.session_ids = tuple(sorted(set(session_ids)))
        self.source_system = source_system

    def _connect(self):
        conn = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def list(self, after_id=0, limit=20):
        if after_id < 0 or not 1 <= limit <= 100:
            raise ValueError("after_id must be nonnegative and limit must be 1..100")
        placeholders = ",".join("?" for _ in self.session_ids)
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT id,session_id,role,content,created_at FROM messages WHERE session_id IN ({placeholders}) "
                                "AND id>? AND role IN ('user','assistant') AND content!='' ORDER BY id LIMIT ?",
                                (*self.session_ids, after_id, limit)).fetchall()
            return {"items": [{"id": row["id"], "session_id": row["session_id"], "role": row["role"],
                                "created_at": row["created_at"], "preview": row["content"][:240]} for row in rows],
                    "next_after_id": rows[-1]["id"] if rows else after_id,
                    "cursor_persisted": False}
        finally:
            conn.close()

    def read(self, message_ids):
        if not 1 <= len(message_ids) <= 20:
            raise ValueError("Read 1..20 explicitly selected source messages")
        conn = self._connect()
        try:
            results = []
            for message_id in dict.fromkeys(message_ids):
                row = conn.execute("SELECT id,session_id,role,content,created_at FROM messages WHERE id=?", (message_id,)).fetchone()
                if not row or row["session_id"] not in self.session_ids or row["role"] not in {"user", "assistant"}:
                    raise ValueError("Message is missing or outside the selected source scope")
                metadata = {"source_system": self.source_system, "session_id": str(row["session_id"]),
                            "message_id": str(row["id"]), "role": row["role"], "created_at": row["created_at"],
                            "content_sha256": digest(row["content"])}
                results.append({"source_key": encode([self.source_system, str(row["session_id"]), str(row["id"])]),
                                "content": row["content"], "metadata": metadata})
            return results
        finally:
            conn.close()
