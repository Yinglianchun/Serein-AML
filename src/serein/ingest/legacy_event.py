"""Import old Event data without re-running writers, settlement, or archival."""

import json
from collections import Counter

from serein.core.store import Conflict, digest, encode, now
from .event_snapshot import referenced_ids
from .legacy_narrative import material_resolution


def event_resolution(store, target, excluded):
    if target in excluded:
        return "excluded_fact"
    row = store.read(target)
    if row:
        return row["lifecycle"] if row["kind"] == "event" else "wrong_kind"
    if store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (target,)).fetchone():
        return "deleted"
    return "missing"


def import_events(store, snapshot):
    if snapshot.get("format") != "serein-event-snapshot-v1":
        raise ValueError("Expected a serein-event-snapshot-v1 file")
    origin = snapshot["origin"]
    events = snapshot["events"]
    ids = {e["item_id"] for e in events}
    excluded = set(snapshot.get("excluded_item_ids", []))
    if len(ids) != len(events) or ids & excluded:
        raise Conflict("Duplicate Event ID or overlap with excluded items")
    if any(e["item_type"] != "event" for e in events):
        raise ValueError("Only Event rows may enter the Event importer")
    refs = snapshot["sources"]
    if len({r["id"] for r in refs}) != len(refs):
        raise Conflict("Duplicate legacy source row ID")
    rows, edge_checks, receipt_checks = [], [], []
    inserted = new_bindings = 0
    with store.transaction():
        fresh = store.save_import_record(origin, "event-snapshot.json", encode(snapshot).encode("utf-8"))
        for event in events:
            event_id = event["item_id"]
            lifecycle = "deleted" if event["status"] == "tombstoned" else event["status"]
            if fresh:
                # The old table has no revision counter/history table. Retain
                # its row as the import baseline; do not invent prior edits.
                store.create(event_id, "event", event["title"], event["body"],
                             lifecycle=lifecycle, manual_surface=event["recallable"], metadata=event,
                             created_at=event["created_at"], updated_at=event["updated_at"])
                if lifecycle == "deleted":
                    store.record_deletion(event_id, event["updated_at"], event)
                inserted += 1
            saved = store.read(event_id)
            if (not saved or saved["kind"] != "event" or saved["body_md"] != event["body"] or
                    saved["title"] != event["title"] or saved["metadata"] != event or
                    saved["lifecycle"] != lifecycle or saved["manual_surface"] != event["recallable"] or
                    saved["revision"] != 1):
                raise Conflict(f"Event differs from source: {event_id}")

        for ref in refs:
            if ref["item_id"] not in ids:
                raise ValueError("Source owner is outside the selected Events")
            if ref["hash_algorithm"] != "sha256-utf8" or digest(ref["content"]) != ref["content_sha256"]:
                raise ValueError(f"Unsupported or mismatched evidence hash: {ref['id']}")
            if not ref["message_id"]:
                raise ValueError("Event evidence requires its original message ID")
            # Same identity contract as the Scene importer: a different content
            # snapshot of the same message is not interchangeable evidence.
            identity = encode([ref["source_system"], ref.get("session_id") or ref.get("thread_id") or "",
                               str(ref["message_id"])])
            source_id = store.add_source(identity, ref["content"], metadata=ref)
            binding_id = "legacy_" + digest(encode([origin, "event_source", ref["id"]]))
            if fresh:
                store.bind(ref["item_id"], source_id, binding_id=binding_id, metadata=ref, record_action=False)
                new_bindings += 1
            saved = store.conn.execute("SELECT document_id,source_id,active,metadata_json FROM evidence_bindings WHERE id=?",
                                       (binding_id,)).fetchone()
            if not saved or tuple(saved) != (ref["item_id"], source_id, 1, encode(ref)):
                raise Conflict(f"Evidence binding differs: {ref['id']}")

        successors = {}
        for edge in snapshot["replacement_edges"]:
            predecessor, successor = edge["predecessor_id"], edge["successor_id"]
            if predecessor in successors:
                raise Conflict("Duplicate replacement predecessor")
            successors[predecessor] = successor
            store.conn.execute("INSERT OR IGNORE INTO event_replacements VALUES (?, ?, ?, ?)",
                               (predecessor, successor, origin, encode(edge)))
            saved = store.conn.execute("SELECT successor_id,origin,metadata_json FROM event_replacements WHERE predecessor_id=?",
                                       (predecessor,)).fetchone()
            if tuple(saved) != (successor, origin, encode(edge)):
                raise Conflict(f"Replacement differs: {predecessor}")
            edge_checks.append({"predecessor": predecessor, "successor": successor,
                                "predecessor_resolution": event_resolution(store, predecessor, excluded),
                                "successor_resolution": event_resolution(store, successor, excluded)})
        cycles = []
        for start in successors:
            visited, target = set(), start
            while target in successors:
                if target in visited:
                    cycles.append(start)
                    break
                visited.add(target)
                target = successors[target]

        for receipt in snapshot["settlement_receipts"]:
            result = json.loads(receipt["result_json"])
            values = (receipt["operation_id"], receipt["request_sha256"], receipt["result_json"],
                      receipt["created_at"], origin)
            store.conn.execute("INSERT OR IGNORE INTO event_settlement_receipts VALUES (?, ?, ?, ?, ?)", values)
            saved = store.conn.execute("SELECT * FROM event_settlement_receipts WHERE operation_id=?", values[:1]).fetchone()
            if tuple(saved) != values:
                raise Conflict(f"Settlement receipt differs: {receipt['operation_id']}")
            mentioned = referenced_ids(result, ids | excluded)
            item_ids = {i["item_id"] for i in result.get("items", []) if i.get("item_id")}
            receipt_checks.append({"operation_id": receipt["operation_id"], "raw_result_exact": True,
                                   "event_ids": sorted(mentioned & ids), "excluded_ids": sorted(mentioned & excluded),
                                   "unknown_result_item_ids": sorted(item_ids - ids - excluded)})
        for link in snapshot["arc_event_links"]:
            values = (link["arc_key"], link["event_id"], origin, encode(link))
            store.conn.execute("INSERT OR IGNORE INTO event_arc_links VALUES (?, ?, ?, ?)", values)
            saved = store.conn.execute("SELECT * FROM event_arc_links WHERE arc_key=? AND event_id=?", values[:2]).fetchone()
            if tuple(saved) != values:
                raise Conflict("Arc/Event link differs from source")

        source_counts = Counter(r["item_id"] for r in refs)
        for event in events:
            state = store.surface_state(event["item_id"])
            rows.append({"id": event["item_id"], "body_exact": True, "metadata_exact": True,
                         "source_rows": source_counts[event["item_id"]], "legacy_status": event["status"],
                         "legacy_recallable": event["recallable"], "surface_state": state,
                         "legacy_covered_by_scene_id": event["covered_by_scene_id"],
                         "legacy_cover_resolution": material_resolution(store, "scene", event["covered_by_scene_id"])
                         if event["covered_by_scene_id"] else None})
        material_checks = [{"document_id": r["document_id"], "revision": r["revision"],
                            "target_id": r["target_id"], "disposition": r["disposition"],
                            "resolution": event_resolution(store, r["target_id"], excluded)}
                           for r in store.conn.execute("SELECT * FROM narrative_materials WHERE kind='event'")]
        legacy_predecessor_checks = [{"id": e["item_id"], "supersedes_item_id": e["supersedes_item_id"],
                                      "resolution": event_resolution(store, e["supersedes_item_id"], excluded)}
                                     for e in events if e["supersedes_item_id"]]
    return {"status": "ok", "origin": origin, "captured_at": snapshot.get("captured_at"), "verified_at": now(),
            "events": len(events), "inserted_events": inserted, "source_rows": len(refs), "inserted_bindings": new_bindings,
            "lifecycle_counts": dict(Counter(e["status"] for e in events)),
            "events_without_sources": sum(not source_counts[e["item_id"]] for e in events),
            "replacement_edges": len(edge_checks), "replacement_cycles_from": cycles, "edge_checks": edge_checks,
            "active_replacement_predecessors": sum(e["predecessor_resolution"] == "active" for e in edge_checks),
            "legacy_predecessor_checks": legacy_predecessor_checks,
            "settlement_receipts": len(receipt_checks), "receipt_checks": receipt_checks,
            "arc_event_links": len(snapshot["arc_event_links"]), "excluded_facts": len(excluded),
            "surface_reason_counts": dict(Counter(reason for r in rows for reason in r["surface_state"]["reasons"])),
            "surfaceable_events": sum(r["surface_state"]["can_surface"] for r in rows),
            "narrative_event_resolutions": dict(Counter(r["resolution"] for r in material_checks)),
            "material_checks": material_checks, "rows": rows,
            "settlement_replayed": False, "bridge_cursor_modified": False}
