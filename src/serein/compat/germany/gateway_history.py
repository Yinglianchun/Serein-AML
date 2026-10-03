"""Original read-only Gateway debug pagination."""
import json
from typing import Any
class GatewayStateStore:

    def list_injection_debug(self, *, session_id: str='', limit: int=20, include_context: bool=True, before_id: int=0, ids: list[int] | None=None, visible_only: bool=False, after_id: int | None=None) -> list[dict[str, Any]]:
        safe_ids: list[int] = []
        for raw_id in ids or []:
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and item_id not in safe_ids:
                safe_ids.append(item_id)
            if len(safe_ids) >= 500:
                break
        limit_ceiling = 500 if safe_ids else 101
        limit = max(1, min(limit_ceiling, int(limit)))
        try:
            safe_before_id = max(0, int(before_id))
        except (TypeError, ValueError):
            safe_before_id = 0
        conn = self._connect()
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append('session_id = ?')
            params.append(session_id)
        if visible_only:
            where.append("(CASE WHEN json_valid(payload_json)=0 THEN 1 "
                         "WHEN json_extract(payload_json,'$.observation_version') IS NULL THEN 1 "
                         "WHEN json_extract(payload_json,'$.request_kind')='user_turn' "
                         "THEN 1 ELSE 0 END)=1")
        ascending = after_id is not None and not safe_ids
        if ascending:
            where.append('id > ?')
            params.append(max(0, int(after_id)))
        if safe_ids:
            where.append(f"id IN ({','.join(('?' for _ in safe_ids))})")
            params.extend(safe_ids)
        elif safe_before_id:
            where.append('id < ?')
            params.append(safe_before_id)
        order = 'ASC' if ascending else 'DESC'
        where_sql = f"WHERE {' AND '.join(where)}" if where else ''
        params.append(limit)
        rows = conn.execute(f'\n            SELECT id, session_id, round_id, created_at, payload_json\n            FROM injection_debug\n            {where_sql}\n            ORDER BY id {order}\n            LIMIT ?\n            ', params).fetchall()
        conn.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row['payload_json'])
            except json.JSONDecodeError:
                payload = {'raw': row['payload_json']}
            if isinstance(payload, dict) and (not include_context):
                payload = dict(payload)
                payload.pop('stable_context', None)
                payload.pop('dynamic_context', None)
            items.append({'id': row['id'], 'session_id': row['session_id'], 'round_id': row['round_id'], 'created_at': row['created_at'], 'payload': payload})
        return items
