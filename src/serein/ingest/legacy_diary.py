"""Preserve diary rows, authors, comments, deletions, history, and room state."""

import base64
from collections import Counter
import json
import re

from serein.core.notebook import resolve_entry
from serein.core.store import Conflict, digest, encode, now


def save_row(store, table, values):
    # Table names and column order are internal constants, never snapshot input.
    store.conn.execute(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' for _ in values)})", values)
    saved = store.conn.execute(f"SELECT * FROM {table} WHERE id=?", values[:1]).fetchone()
    if not saved or tuple(saved) != tuple(values):
        raise Conflict(f"Imported {table} record differs: {values[0]}")


def resolve_source_ledgers(store):
    pattern = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*Assistant-Diary\s+`#(\d+)`[^|]*\|\s*`(diary_source_[A-Za-z0-9_]+)`[^|]*\|\s*`([a-f0-9]{64})`")
    checks = []
    for document in store.conn.execute("SELECT r.* FROM revisions r JOIN documents d ON d.id=r.document_id WHERE d.kind='narrative'"):
        for line in document["body_md"].splitlines():
            match = pattern.match(line)
            if not match:
                continue
            day, entry_id, source_id, sha = match.groups()
            row = store.conn.execute("SELECT * FROM diary_entries WHERE id=?", (int(entry_id),)).fetchone()
            verified = bool(row and row["kind"] == "diary" and row["day"] == day and digest(row["body_md"]) == sha)
            proof = {"document_id": document["document_id"], "revision": document["number"],
                     "source_id": source_id, "entry_id": int(entry_id), "ledger_line": line,
                     "date_and_body_exact": verified}
            if verified:
                store.conn.execute("INSERT OR IGNORE INTO diary_source_refs VALUES (?, ?, ?, ?)",
                                   (source_id, int(entry_id), sha, encode(proof)))
                saved = store.conn.execute("SELECT entry_id,content_sha256 FROM diary_source_refs WHERE id=?", (source_id,)).fetchone()
                if tuple(saved) != (int(entry_id), sha):
                    raise Conflict("Legacy diary source ledger has conflicting identities")
            checks.append(proof)
    return checks


def import_diaries(store, snapshot):
    if snapshot.get("format") != "serein-diary-snapshot-v1":
        raise ValueError("Expected a serein-diary-snapshot-v1 file")
    origin, at = snapshot["origin"], snapshot["captured_at"]
    diaries = snapshot["diaries"]
    for section in ("diaries", "comments", "diary_revisions", "darkroom_sessions"):
        if len({r["id"] for r in snapshot[section]}) != len(snapshot[section]):
            raise Conflict(f"Duplicate IDs in {section}")
    entries_by_id = {r["id"]: r for r in diaries}
    inserted = 0
    with store.transaction():
        fresh = store.save_import_record(origin, "diary-snapshot.json", encode(snapshot).encode("utf-8"))
        if fresh and any(store.conn.execute("SELECT 1 FROM diary_entries WHERE id=?", (r["id"],)).fetchone()
                         for r in diaries):
            raise Conflict("Diary IDs were already imported; changed snapshots need an explicit update")
        for row in diaries:
            save_row(store, "diary_entries", (
                row["id"], row["entry_type"], row["revision"], row["author"], row["date"], row["title"],
                row["content"], row["visibility"], row["unlock_at"], row["deleted_at"],
                row["created_at"], row["updated_at"], row["source_id"], encode(row)))
            inserted += fresh
        for row in snapshot["comments"]:
            save_row(store, "diary_comments", (row["id"], row["diary_id"], row["author"], row["content"],
                                                row["created_at"], encode(row)))
        for row in snapshot["diary_revisions"]:
            save_row(store, "diary_history", (row["id"], row["diary_id"], row["revision"], row["content"], encode(row)))
        for row in snapshot["darkroom_sessions"]:
            save_row(store, "diary_sessions", (row["id"], row["diary_id"], row["locked_at"], row["unlock_at"], encode(row)))

        source_ledger_checks = resolve_source_ledgers(store)

        legacy_files = {}
        for entry in snapshot["legacy_files"]:
            raw = base64.b64decode(entry["bytes_b64"], validate=True)
            if digest(raw) != entry["sha256"]:
                raise ValueError(f"Legacy Darkroom checksum mismatch: {entry['path']}")
            if entry["path"] in legacy_files:
                raise Conflict("Duplicate legacy Darkroom path")
            legacy_files[entry["path"]] = raw
            store.save_import_record(origin, entry["path"], raw)
        old_entries = [json.loads(line) for line in legacy_files.get("darkroom/entries.jsonl", b"").splitlines() if line.strip()]
        old_state = json.loads(legacy_files["darkroom/state.json"]) if "darkroom/state.json" in legacy_files else None
        legacy_checks = []
        for old in old_entries:
            result = resolve_entry(store, old["id"], kind="darkroom", at=at)
            canonical = entries_by_id.get(result["entry_id"])
            # Only explicit source_id proves a migrated owner. Text similarity
            # and numeric IDs from a different namespace do not establish it.
            legacy_checks.append({**result, "legacy_visibility": old.get("visibility"),
                                  "content_exact": canonical["content"] == old.get("note") if canonical else None,
                                  "canonical_preferred": canonical is not None})
        owner_checks = []
        for section in ("comments", "diary_revisions", "darkroom_sessions"):
            for row in snapshot[section]:
                result = (resolve_entry(store, row["diary_id"], at=at) if row["diary_id"] is not None
                          else {"entry_id": None, "resolution": "unattached"})
                owner_checks.append({"section": section, "id": row["id"], **result})
        material_checks = [{"document_id": row["document_id"], "revision": row["revision"],
                            "disposition": row["disposition"],
                            **resolve_entry(store, row["target_id"], kind=row["kind"], at=at)}
                           for row in store.conn.execute("SELECT * FROM narrative_materials WHERE kind IN ('diary','darkroom')")]
        rows = [{"id": r["id"], "ui_id": f"diary-vps-{r['id']}", "kind": r["entry_type"],
                 "author": r["author"], "revision": r["revision"], "body_exact": True, "metadata_exact": True,
                 "resolution": resolve_entry(store, r["id"], at=at)["resolution"]} for r in diaries]
    return {"status": "ok", "origin": origin, "captured_at": at, "verified_at": now(),
            "entries": len(diaries), "inserted_entries": inserted,
            "entry_states": dict(Counter(r["kind"] + ":" + r["resolution"] for r in rows)),
            "authors": dict(Counter(r["author"] for r in rows)),
            "comments": len(snapshot["comments"]), "historical_rows": len(snapshot["diary_revisions"]),
            "darkroom_sessions": len(snapshot["darkroom_sessions"]), "legacy_darkroom_entries": len(old_entries),
            "legacy_checks": legacy_checks, "legacy_state_retained": old_state is not None,
            "legacy_state_last_entry": old_state.get("last_entry_id") if old_state else None,
            "absent_legacy_files": snapshot.get("absent_legacy_files", []),
            "owner_resolutions": dict(Counter(r["resolution"] for r in owner_checks)), "owner_checks": owner_checks,
            "narrative_material_resolutions": dict(Counter(r["resolution"] for r in material_checks)),
            "material_checks": material_checks, "rows": rows,
            "source_ledger_checks": source_ledger_checks,
            "verified_source_refs": len({p["source_id"] for p in source_ledger_checks if p["date_and_body_exact"]}),
            "source_writes": False, "legacy_darkroom_replayed": False}
