"""Import an explicitly selected offline Scene snapshot, never a live directory."""

import base64
import json
import re

import yaml

from serein.core.store import Conflict, digest, encode, now
from .scene_references import import_evidence, import_relations


CONTRACTS = {"scene-migration-v2", "write-scene-v1", "close-window-scene-v2", "close-window-scene-v3"}


def jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda x: x.isoformat()))


def parse_scene(raw):
    text = raw.decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise ValueError("Scene lacks YAML frontmatter")
    metadata = jsonable(yaml.safe_load(match.group(1)))
    if not isinstance(metadata, dict) or not metadata.get("id"):
        raise ValueError("Scene lacks its original ID")
    if metadata.get("write_contract") not in CONTRACTS:
        raise ValueError("Not an authored Scene; legacy buckets need separate review")
    return metadata, text[match.end():]


def scene_lifecycle(metadata):
    if metadata.get("deleted_at"):
        return "deleted"
    status = metadata.get("scene_status")
    if status:
        if status not in {"active", "archived", "superseded", "deleted"}:
            raise ValueError(f"Unsupported Scene lifecycle: {status}")
        return status
    if metadata.get("type") == "archived" or metadata.get("active") is False:
        return "archived"
    if metadata.get("deprecated") is True:
        raise ValueError("Deprecated Scene without explicit lifecycle needs review")
    return "active"


def import_snapshot(store, snapshot):
    if snapshot.get("format") != "serein-scene-snapshot-v1":
        raise ValueError("Expected a serein-scene-snapshot-v1 file")
    origin = snapshot["origin"]
    scenes = snapshot["scenes"]
    ids = set()
    expected = []
    inserted = 0
    with store.transaction():
        # Save the entire export, including evidence action history, for audit.
        store.save_import_record(origin, "snapshot.json", encode(snapshot).encode("utf-8"))
        for entry in scenes:
            raw = base64.b64decode(entry["bytes_b64"], validate=True)
            if digest(raw) != entry["sha256"]:
                raise ValueError(f"Source file checksum mismatch: {entry['path']}")
            meta, body = parse_scene(raw)
            document_id = meta["id"]
            if document_id in ids:
                raise Conflict(f"Duplicate Scene ID in snapshot: {document_id}")
            ids.add(document_id)
            lifecycle = scene_lifecycle(meta)
            title = str(meta.get("name") or meta.get("title") or document_id)
            revision = int(meta.get("scene_revision") or 1)
            if store.save_import_record(origin, entry["path"], raw):
                store.create(document_id, "scene", title, body, lifecycle=lifecycle,
                             manual_surface=meta.get("recallable", True), metadata=meta,
                             revision=revision, created_at=meta.get("created"),
                             updated_at=meta.get("updated_at") or meta.get("last_active"))
                # Import actual old bodies only; do not manufacture missing revisions.
                for historical in meta.get("scene_revision_history") or []:
                    number = int(historical["revision"])
                    if number >= revision or number <= 0:
                        raise ValueError(f"Invalid historical revision for {document_id}")
                    store._add_revision(document_id, number, str(historical.get("title") or title),
                                        historical["content"], historical,
                                        historical.get("saved_at") or historical.get("updated_at") or "")
                inserted += 1
            expected.append((entry, meta, body, lifecycle, title, revision))

        actions = snapshot.get("evidence_actions", [])
        for tombstone in snapshot.get("tombstones", []):
            raw = base64.b64decode(tombstone["bytes_b64"], validate=True)
            if digest(raw) != tombstone["sha256"]:
                raise ValueError("Tombstone checksum mismatch")
            if store.save_import_record(origin, tombstone["path"], raw):
                deleted = json.loads(raw)
                store.record_deletion(deleted["id"], deleted.get("deleted_at") or "", deleted)

        evidence_report = import_evidence(store, snapshot, ids)
        relations_report = import_relations(store, snapshot)

        rows = []
        for entry, meta, body, lifecycle, title, revision in expected:
            actual = store.read(meta["id"])
            deleted = store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (meta["id"],)).fetchone()
            history_exact = True
            for old in meta.get("scene_revision_history") or []:
                saved = store.read(meta["id"], revision=int(old["revision"]))
                history_exact = history_exact and saved is not None and (
                    saved["body_md"] == old["content"] and saved["metadata"] == old
                    and saved["title"] == str(old.get("title") or title)
                )
            checks = {"body_exact": actual["body_md"] == body,
                      "metadata_exact": actual["metadata"] == meta,
                      "title_exact": actual["title"] == title,
                      "revision_exact": actual["revision"] == revision,
                      "history_exact": history_exact,
                      "manual_surface_exact": actual["manual_surface"] == meta.get("recallable", True),
                      "lifecycle_exact": actual["lifecycle"] == ("deleted" if deleted else lifecycle)}
            if not all(checks.values()):
                raise Conflict(f"Imported Scene differs from source: {meta['id']}")
            rows.append({"id": meta["id"], "path": entry["path"], "source_sha256": entry["sha256"],
                         "body_sha256": actual["body_sha256"], "lifecycle": actual["lifecycle"],
                         "historical_revisions": len(meta.get("scene_revision_history") or []),
                         "checks": checks})
    return {"status": "ok", "origin": origin, "captured_at": snapshot.get("captured_at"),
            "verified_at": now(), "scenes": len(rows), "inserted_scenes": inserted,
            "evidence_rows": len(snapshot.get("evidence", [])), **evidence_report,
            "evidence_actions": len(actions), "tombstones": len(snapshot.get("tombstones", [])),
            "relations": relations_report,
            "excluded_documents": snapshot.get("excluded_documents", []),
            "canonical_writes": "local_test_database_only", "rows": rows}
