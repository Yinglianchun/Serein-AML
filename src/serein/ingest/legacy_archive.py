"""Retain historical works and deletion facts without reviving old workflows."""

import base64
from collections import Counter
import json
import re

import yaml

from serein.core.store import Conflict, digest, encode, now
from .legacy_scene import jsonable


def save_work(store, origin, identifier, kind, revision, title, body, metadata, path):
    values = (identifier, kind, revision, title, body, digest(body), encode(metadata), origin, path)
    store.conn.execute("INSERT OR IGNORE INTO historical_works VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    saved = store.conn.execute("SELECT * FROM historical_works WHERE id=?", (identifier,)).fetchone()
    if tuple(saved) != values:
        raise Conflict(f"Historical work differs from the imported source: {identifier}")


def resolution(store, identifier):
    if store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (identifier,)).fetchone():
        return "deleted"
    row = store.conn.execute("SELECT lifecycle FROM documents WHERE id=?", (identifier,)).fetchone()
    if row:
        return row[0]
    if store.conn.execute("SELECT 1 FROM historical_works WHERE id=?", (identifier,)).fetchone():
        return "historical"
    return "not_imported_or_missing"


def import_archives(store, snapshot):
    if snapshot.get("format") != "serein-archive-snapshot-v1":
        raise ValueError("Expected a serein-archive-snapshot-v1 file")
    origin = snapshot["origin"]
    works, references, logs = [], [], []
    seen = set()
    with store.transaction():
        fresh = store.save_import_record(origin, "archive-snapshot.json", encode(snapshot).encode("utf-8"))
        for row in snapshot["window_shadows"]:
            identifier = row["window_id"]
            if identifier in seen:
                raise Conflict("Duplicate archive ID")
            seen.add(identifier)
            save_work(store, origin, identifier, "shadow", row["revision_number"], identifier,
                      row["content"], row, "window_shadows.sqlite/" + identifier)
            works.append({"id": identifier, "kind": "shadow", "body_exact": True, "metadata_exact": True})
            for field in ("parent_shadow_id", "continue_scene_id", "supersedes_window_id", "revision_root_id"):
                if row.get(field):
                    references.append({"owner_id": identifier, "field": field, "target_id": row[field]})
            for target in json.loads(row["moment_bucket_ids_json"]):
                references.append({"owner_id": identifier, "field": "moment_bucket_ids", "target_id": target})
        files = set()
        for entry in snapshot["files"]:
            path = entry["path"]
            if path in files:
                raise Conflict("Duplicate archive file path")
            files.add(path)
            raw = base64.b64decode(entry["bytes_b64"], validate=True)
            if digest(raw) != entry["sha256"]:
                raise ValueError("Historical artifact checksum mismatch")
            store.save_import_record(origin, path, raw)
            if re.fullmatch(r"dreams/dream_[^/]+\.md", path):
                text = raw.decode("utf-8")
                match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
                if not match:
                    raise ValueError("Dream lacks its original frontmatter")
                meta = jsonable(yaml.safe_load(match.group(1)))
                identifier = meta["dream_id"]
                if identifier in seen:
                    raise Conflict("Duplicate archive ID")
                seen.add(identifier)
                save_work(store, origin, identifier, "dream", 1, meta.get("title") or identifier,
                          text[match.end():], meta, path)
                works.append({"id": identifier, "kind": "dream", "body_exact": True, "metadata_exact": True})
                for field in ("source_bucket_ids", "source_raw_event_ids"):
                    for target in meta.get(field) or []:
                        references.append({"owner_id": identifier, "field": field, "target_id": str(target)})
                for field in ("old_echo_id", "identity_anchor_id"):
                    if meta.get(field):
                        references.append({"owner_id": identifier, "field": field, "target_id": meta[field]})
            elif path == "dreams/logs/events.jsonl":
                for number, line in enumerate(raw.splitlines(), 1):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    work_id = event.get("dream_id") or ""
                    values = (origin, number, work_id, event["event"], encode(event))
                    store.conn.execute("INSERT OR IGNORE INTO historical_work_events VALUES (?, ?, ?, ?, ?)", values)
                    saved = store.conn.execute("SELECT * FROM historical_work_events WHERE origin=? AND line_number=?", values[:2]).fetchone()
                    if tuple(saved) != values:
                        raise Conflict("Historical dream event differs")
                    if event["event"] == "deleted":
                        if not work_id.startswith("dream_"):
                            raise ValueError("Dream deletion log must identify a dream")
                        store.record_deletion(work_id, event["deleted_at"], event)
                    logs.append(event)
        for ref in references:
            ref["resolution"] = ("raw_source_not_imported" if ref["field"] == "source_raw_event_ids"
                                 else resolution(store, ref["target_id"]))
        for work in works:
            work["status"] = resolution(store, work["id"])
    return {"status": "ok", "origin": origin, "captured_at": snapshot["captured_at"], "verified_at": now(),
            "works": len(works), "inserted_works": len(works) if fresh else 0,
            "kinds": dict(Counter(w["kind"] for w in works)), "original_files": len(files),
            "dream_log_rows": len(logs), "dream_event_counts": dict(Counter(e["event"] for e in logs)),
            "deleted_dream_ids": sorted({e["dream_id"] for e in logs if e["event"] == "deleted"}),
            "reference_states": dict(Counter(r["resolution"] for r in references)),
            "references": references, "rows": works, "handoff_enabled": False, "dream_generation_enabled": False}
