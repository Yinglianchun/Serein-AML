"""Retain evidence and relation history without resurrecting absent owners."""

from collections import Counter

from serein.core.store import Conflict, digest, encode


def resolution(store, snapshot, scene_id):
    document = store.read(scene_id)
    if document and document["kind"] == "scene":
        return document["lifecycle"]
    if store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (scene_id,)).fetchone():
        return "deleted"
    if any(row.get("id") == scene_id for row in snapshot.get("excluded_documents", [])):
        return "excluded_legacy_object"
    return "missing"


def _detach(store, snapshot, record_type, record, reason):
    key = (snapshot["origin"], record_type, str(record["id"]))
    store.conn.execute(
        "INSERT OR IGNORE INTO detached_import_records VALUES (?, ?, ?, ?, ?)",
        (*key, reason, encode(record)),
    )
    saved = store.conn.execute(
        "SELECT metadata_json FROM detached_import_records "
        "WHERE origin=? AND record_type=? AND record_id=?", key,
    ).fetchone()
    if saved[0] != encode(record):
        raise Conflict("Detached history differs from the original snapshot")


def import_evidence(store, snapshot, scene_ids):
    actions = snapshot.get("evidence_actions", [])
    references = snapshot.get("evidence", [])
    reference_ids = {ref["id"] for ref in references}
    new_bindings = 0
    detached = Counter()
    evidence_report = []
    for ref in references:
        content = ref["content"]
        if ref.get("content_sha256") and digest(content) != ref["content_sha256"]:
            raise ValueError(f"Evidence checksum mismatch: {ref['id']}")
        history = sorted([action for action in actions if action["evidence_id"] == ref["id"]],
                         key=lambda action: action["id"])
        if any(a["action"] not in {"bind", "unbind"} or
               (a.get("scene_id") and a["scene_id"] != ref["scene_id"]) for a in history):
            raise ValueError("Evidence action does not match its owner or supported action")
        if ref["scene_id"] not in scene_ids:
            reason = "owner_" + resolution(store, snapshot, ref["scene_id"])
            _detach(store, snapshot, "evidence", ref, reason)
            for action in history:
                _detach(store, snapshot, "evidence_action", action, reason)
            detached[reason] += 1
            evidence_report.append({"legacy_id": ref["id"], "scene_id": ref["scene_id"],
                                    "storage": "detached_history", "reason": reason,
                                    "actions": len(history), "metadata_exact": True})
            continue
        identity = encode([ref["source_system"], ref.get("session_id") or ref.get("thread_id") or "",
                           str(ref.get("message_id") or "")])
        if not ref.get("message_id"):
            raise ValueError("Evidence without message ID needs an explicit source mapping")
        source_id = store.add_source(identity, content, metadata=ref)
        binding_id = "legacy_" + digest(encode([snapshot["origin"], "scene_evidence", ref["id"]]))
        active = not history or history[-1]["action"] == "bind"
        if not store.conn.execute("SELECT 1 FROM evidence_bindings WHERE id=?", (binding_id,)).fetchone():
            store.bind(ref["scene_id"], source_id, binding_id=binding_id, metadata=ref,
                       active=active, record_action=False)
            for action in history:
                store.add_evidence_action(
                    binding_id, action["action"], actor=action.get("actor") or "",
                    action_id="legacy_" + digest(encode([snapshot["origin"], "action", action["id"]])),
                    occurred_at=action["created_at"], metadata=action,
                )
            new_bindings += 1
        saved = store.conn.execute(
            "SELECT b.active,b.metadata_json,s.content,s.content_sha256 FROM evidence_bindings b "
            "JOIN sources s ON s.id=b.source_id WHERE b.id=?", (binding_id,),
        ).fetchone()
        if (saved["active"] != active or saved["metadata_json"] != encode(ref) or
                saved["content"] != content or saved["content_sha256"] != digest(content)):
            raise Conflict("Evidence binding differs from the source snapshot")
        for action in history:
            action_id = "legacy_" + digest(encode([snapshot["origin"], "action", action["id"]]))
            saved_action = store.conn.execute("SELECT metadata_json FROM evidence_actions WHERE id=?",
                                               (action_id,)).fetchone()
            if not saved_action or saved_action[0] != encode(action):
                raise Conflict("Evidence action differs from source")
        evidence_report.append({"legacy_id": ref["id"], "scene_id": ref["scene_id"],
                                "storage": "binding", "active": active,
                                "actions": len(history), "metadata_exact": True})
    for action in actions:
        if action["evidence_id"] not in reference_ids:
            _detach(store, snapshot, "evidence_action", action, "binding_missing")
    return {"inserted_bindings": new_bindings, "detached_evidence": dict(detached),
            "orphan_actions": sum(a["evidence_id"] not in reference_ids for a in actions),
            "evidence_checks": evidence_report}


def import_relations(store, snapshot):
    result = {}
    for table, section, key in (("scene_relations", "scene_relations", "edge_id"),
                                 ("scene_proposals", "scene_proposals", "proposal_id")):
        rows = snapshot.get(section, [])
        checks = []
        for record in rows:
            values = (record[key], snapshot["origin"], record["source_scene_id"], record["target_scene_id"])
            if table == "scene_relations":
                store.conn.execute("INSERT OR IGNORE INTO scene_relations VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (*values, record["lifecycle_status"], record["active"], encode(record)))
            else:
                store.conn.execute("INSERT OR IGNORE INTO scene_proposals VALUES (?, ?, ?, ?, ?, ?)",
                                   (*values, record["status"], encode(record)))
            saved = store.conn.execute(f"SELECT metadata_json FROM {table} WHERE id=?", (record[key],)).fetchone()
            if saved[0] != encode(record):
                raise Conflict(f"Imported {table} record differs: {record[key]}")
            source = resolution(store, snapshot, record["source_scene_id"])
            target = resolution(store, snapshot, record["target_scene_id"])
            checks.append({"id": record[key], "source_id": record["source_scene_id"],
                           "target_id": record["target_scene_id"], "source_resolution": source,
                           "target_resolution": target, "metadata_exact": True,
                           "original_status": record.get("lifecycle_status") or record.get("status"),
                           "original_active": record.get("active")})
        result[section] = {"count": len(rows), "checks": checks,
                           "missing_endpoints": sum(c["source_resolution"] == "missing" or
                                                    c["target_resolution"] == "missing" for c in checks),
                           "deleted_endpoints": sum(c["source_resolution"] == "deleted" or
                                                    c["target_resolution"] == "deleted" for c in checks)}
        if table == "scene_relations":
            result[section]["active_with_unavailable_endpoints"] = sum(
                bool(c["original_active"]) and (c["source_resolution"] != "active" or
                                               c["target_resolution"] != "active") for c in checks)
    return result
