"""Offline Narrative import. Registry revisions, never directory order, win."""

import base64
import json
import re
from collections import Counter
from pathlib import PurePosixPath

from serein.core.store import Conflict, digest, encode, now
from serein.core.notebook import resolve_entry


KINDS = ("scene", "event", "diary", "darkroom", "upload")
MENTION = re.compile(r"\b(scene_[A-Za-z0-9_]+|event_[A-Za-z0-9_]+|upload_[A-Za-z0-9_]+|diary_source_[A-Za-z0-9_]+|diary-vps-\d+)\b")


def file_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Expected a path relative to narrative_rolls: {value}")
    return path.as_posix()


def material_resolution(store, kind, target):
    if kind in {"diary", "darkroom"} and store.conn.execute("SELECT 1 FROM diary_entries LIMIT 1").fetchone():
        return resolve_entry(store, target, kind=kind)["resolution"]
    if kind == "upload":
        return "available" if store.conn.execute(
            "SELECT 1 FROM narrative_uploads WHERE id=?", (target,)).fetchone() else "missing"
    document = store.read(target)
    if document:
        return document["lifecycle"] if document["kind"] == kind else "wrong_kind"
    if store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (target,)).fetchone():
        return "deleted"
    return "pending_adapter" if kind in {"event", "diary", "darkroom"} else "missing"


def add_material(store, document_id, revision, locator, kind, target, disposition, metadata):
    values = (document_id, revision, locator, kind, str(target))
    store.conn.execute("INSERT OR IGNORE INTO narrative_materials VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (*values, disposition, encode(metadata)))
    saved = store.conn.execute(
        "SELECT disposition,metadata_json FROM narrative_materials WHERE document_id=? "
        "AND revision=? AND locator=? AND kind=? AND target_id=?", values).fetchone()
    if tuple(saved) != (disposition, encode(metadata)):
        raise Conflict(f"Narrative material differs: {document_id}/{target}")


def import_narratives(store, snapshot):
    if snapshot.get("format") != "serein-narrative-snapshot-v1":
        raise ValueError("Expected a serein-narrative-snapshot-v1 file")
    origin = snapshot["origin"]
    files = {}
    for entry in snapshot["files"]:
        path = file_path(entry["path"])
        if path in files:
            raise Conflict(f"Duplicate source path: {path}")
        raw = base64.b64decode(entry["bytes_b64"], validate=True)
        if digest(raw) != entry["sha256"]:
            raise ValueError(f"File checksum mismatch: {path}")
        files[path] = raw
    registry = json.loads(files["registry.json"])
    uploads = json.loads(files["uploads/index.json"])
    inbox = json.loads(files["revision_inbox.json"])
    if (registry.get("schema_version") != "narrative-roll-registry-v1" or
            uploads.get("schema_version") != "narrative-upload-v1" or
            inbox.get("schema_version") != "narrative-revision-inbox-v1"):
        raise ValueError("Unsupported legacy Narrative registry, upload index, or inbox")
    rolls = registry["rolls"]
    if len({r["narrative_id"] for r in rolls}) != len(rolls):
        raise Conflict("Duplicate Narrative ID in registry")
    if len({u["upload_id"] for u in uploads["items"]}) != len(uploads["items"]):
        raise Conflict("Duplicate upload ID in index")
    rows, referenced_paths, local_refs = [], set(), []
    inserted = historical_count = 0
    with store.transaction():
        new_snapshot = store.save_import_record(origin, "snapshot.json", encode(snapshot).encode("utf-8"))
        for path, raw in files.items():
            store.save_import_record(origin, path, raw)
        store.conn.execute('INSERT OR IGNORE INTO background_state VALUES (?, ?)',
                           ('narrative_inbox', encode({key:value for key,value in inbox.items() if key!='items'})))

        for upload in uploads["items"]:
            path = file_path("uploads/blobs/" + upload["blob_name"])
            raw = files[path]
            if digest(raw) != upload["sha256"] or len(raw) != upload["size"]:
                raise ValueError(f"Upload index differs from original bytes: {upload['upload_id']}")
            store.conn.execute("INSERT OR IGNORE INTO narrative_uploads VALUES (?, ?, ?, ?)",
                               (upload["upload_id"], origin, path, encode(upload)))
            saved = store.conn.execute("SELECT origin,path,metadata_json FROM narrative_uploads WHERE id=?",
                                       (upload["upload_id"],)).fetchone()
            if tuple(saved) != (origin, path, encode(upload)):
                raise Conflict(f"Upload already imported from different input: {upload['upload_id']}")

        for roll in rolls:
            document_id = roll["narrative_id"]
            current = int(roll["revision"])
            history = roll.get("history") or []
            numbers = [int(r["revision"]) for r in history]
            if len(numbers) != len(set(numbers)) or any(n <= 0 or n >= current for n in numbers):
                raise ValueError(f"Invalid Narrative revision history: {document_id}")
            for entry in [roll, *history]:
                revision = int(entry["revision"])
                path = file_path(entry["source_file"])
                raw = files[path]
                # Legacy hashes cover the full document, including source ledgers.
                if entry.get("document_sha256") and digest(raw) != entry["document_sha256"]:
                    raise ValueError(f"Registry document checksum mismatch: {path}")
                body = raw.decode("utf-8")
                heading = re.search(r"^# (.+)$", body, re.M)
                title = str(entry.get("title") or (heading.group(1).strip() if heading else document_id))
                metadata = {"legacy_registry": entry, "body_format": "legacy_full_document"}
                if new_snapshot:
                    if revision == current:
                        store.create(document_id, "narrative", title, body, revision=current,
                                     lifecycle=roll["lifecycle"], manual_surface=None, metadata=metadata,
                                     created_at=roll.get("published_at"), updated_at=roll.get("published_at"))
                        inserted += 1
                    else:
                        store._add_revision(document_id, revision, title, body, metadata,
                                            entry.get("published_at") or "")
                saved = store.read(document_id, revision=revision)
                if (not saved or saved["body_md"] != body or saved["title"] != title or
                        saved["metadata"] != metadata or saved["kind"] != "narrative" or
                        saved["revision"] != current or saved["lifecycle"] != roll["lifecycle"] or
                        saved["manual_surface"] is not None):
                    raise Conflict(f"Narrative revision differs: {document_id}/{revision}")
                historical_count += revision != current
                referenced_paths.add(path)
                for kind in KINDS:
                    for disposition in ("linked", "excluded"):
                        field = f"{disposition}_{kind}_ids"
                        for target in entry.get(field) or []:
                            add_material(store, document_id, revision, "registry:" + field, kind,
                                         target, disposition, {"source_file": path})
                for target in set(MENTION.findall(body)):
                    kind = "diary" if target.startswith("diary") else target.split("_", 1)[0]
                    add_material(store, document_id, revision, "document:mention", kind, target,
                                 "mentioned", {"source_file": path})
                # File references are evidence leads only; their absence is reported,
                # never repaired by inventing or searching for substitute evidence.
                for target in sorted(set(re.findall(r"`(docs/[^`\r\n]+)`", body))):
                    local_refs.append({"document_id": document_id, "revision": revision,
                                       "path": target, "available": target in files})
                rows.append({"id": document_id, "revision": revision, "current": revision == current,
                             "path": path, "body_exact": True, "metadata_exact": True})

        unmatched_links = []
        for link in snapshot.get("arc_event_links", []):
            owners = [r for r in rolls if r.get("arc_key") == link["arc_key"] and link["arc_key"]]
            if not owners:
                unmatched_links.append(link)
            for owner in owners:
                add_material(store, owner["narrative_id"], int(owner["revision"]), "arc_event_links",
                             "event", link["event_id"], "appended", link)
        for item in inbox["items"]:
            store.conn.execute("INSERT OR IGNORE INTO narrative_proposals VALUES (?, ?, ?, ?, ?)",
                               (item["proposal_id"], origin, item.get("narrative_id") or "",
                                item["status"], encode(item)))
            saved = store.conn.execute("SELECT origin,metadata_json FROM narrative_proposals WHERE id=?",
                                       (item["proposal_id"],)).fetchone()
            if tuple(saved) != (origin, encode(item)):
                raise Conflict(f"Narrative proposal differs: {item['proposal_id']}")

        material_checks = []
        for roll in rolls:
            for ref in store.conn.execute("SELECT * FROM narrative_materials WHERE document_id=?",
                                          (roll["narrative_id"],)):
                material_checks.append({"document_id": ref["document_id"], "revision": ref["revision"],
                                        "kind": ref["kind"], "target_id": ref["target_id"],
                                        "disposition": ref["disposition"],
                                        "resolution": material_resolution(store, ref["kind"], ref["target_id"])})
        # Registry parentage is retained and checked without creating missing parents.
        parent_checks = [{"id": r["narrative_id"], "parent": r["parent_narrative_id"],
                          "resolution": material_resolution(store, "narrative", r["parent_narrative_id"])}
                         for r in rolls if r.get("parent_narrative_id")]
    return {"status": "ok", "origin": origin, "captured_at": snapshot.get("captured_at"),
            "verified_at": now(), "narratives": len(rolls), "inserted_narratives": inserted,
            "historical_revisions": historical_count, "source_files": len(files),
            "referenced_documents": len(referenced_paths),
            "unregistered_documents": sorted(p for p in files if p.endswith(".md") and p not in referenced_paths),
            "publication_states": dict(Counter(r["publication_status"] for r in rolls)),
            "uploads": len(uploads["items"]), "proposals": len(inbox["items"]),
            "proposal_states": dict(Counter(i["status"] for i in inbox["items"])),
            "arc_event_links": len(snapshot.get("arc_event_links", [])), "unmatched_arc_links": unmatched_links,
            "material_resolutions": dict(Counter(r["resolution"] for r in material_checks)),
            "material_checks": material_checks, "parent_checks": parent_checks,
            "local_file_references": local_refs, "rows": rows}
