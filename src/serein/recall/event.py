"""Event evidence and time rules; no Scene cue or emotional weighting."""

from datetime import date


def memory_date(document):
    metadata = document["metadata"]
    value = metadata.get("local_date") or metadata.get("date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def annotate(hit):
    document = hit["object"]["document"]
    return {**hit, "time": {"event_date": memory_date(document), "recorded_at": document["created_at"],
                            "note": "Original event date is distinct from record creation; relative prose dates are historical."}}
