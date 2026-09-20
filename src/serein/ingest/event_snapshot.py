"""Read an Event-only snapshot in one transaction, without legacy Store code."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def referenced_ids(value, known_ids):
    if isinstance(value, str):
        return {value} if value in known_ids else set()
    if isinstance(value, dict):
        value = value.values()
    if isinstance(value, (list, tuple)) or type(value).__name__ == "dict_values":
        return set().union(*(referenced_ids(v, known_ids) for v in value))
    return set()


def export_events(database):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        events = [dict(r) for r in db.execute("SELECT * FROM fact_events WHERE item_type='event' ORDER BY item_id")]
        ids = {r["item_id"] for r in events}
        excluded = [r[0] for r in db.execute("SELECT item_id FROM fact_events WHERE item_type!='event' ORDER BY item_id")]
        sources = [dict(r) for r in db.execute(
            "SELECT s.* FROM fact_event_sources s JOIN fact_events e ON e.item_id=s.item_id "
            "WHERE e.item_type='event' ORDER BY s.id")]
        edges = [dict(r) for r in db.execute("SELECT * FROM fact_event_replacement_edges ORDER BY predecessor_id")
                 if r["predecessor_id"] in ids or r["successor_id"] in ids]
        links = [dict(r) for r in db.execute("SELECT * FROM fact_event_arc_links ORDER BY arc_key,event_id")
                 if r["event_id"] in ids]
        receipts, excluded_receipts = [], []
        for row in db.execute("SELECT * FROM fact_event_settlement_operations ORDER BY operation_id"):
            if referenced_ids(json.loads(row["result_json"]), ids):
                receipts.append(dict(row))
            else:
                excluded_receipts.append(row["operation_id"])
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"format": "serein-event-snapshot-v1", "origin": "legacy-events-" + timestamp,
            "captured_at": timestamp, "events": events, "sources": sources,
            "replacement_edges": edges, "arc_event_links": links, "settlement_receipts": receipts,
            "excluded_item_ids": excluded, "excluded_receipt_ids": excluded_receipts,
            "consistency": "Single read-only SQLite transaction"}
