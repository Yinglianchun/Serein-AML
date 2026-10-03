"""Original Germany raw HTTP payload normalization."""

def _raw_ingest_events_from_body(
    body: dict,
    *,
    default_session_id: str = "",
    default_conversation_id: str = "",
) -> list[dict]:
    if not isinstance(body, dict):
        return []
    if isinstance(body.get("events"), list):
        events = [item for item in body.get("events", []) if isinstance(item, dict)]
    elif isinstance(body.get("event"), dict):
        events = [body["event"]]
    elif any(key in body for key in ("role", "text", "content")):
        events = [body]
    else:
        events = []

    fallback_session_id = str(default_session_id or "").strip()
    fallback_conversation_id = str(default_conversation_id or "").strip() or fallback_session_id
    common = {
        "source": body.get("source"),
        "conversation_id": body.get("conversation_id") or fallback_conversation_id,
        "session_id": body.get("session_id") or fallback_session_id,
        "client": body.get("client"),
    }
    for event in events:
        for key, value in common.items():
            if value is not None and key not in event:
                event[key] = value
    return events
