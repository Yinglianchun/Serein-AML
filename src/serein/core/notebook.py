"""Notebook ID resolution and reads; no clock-driven writes or unlocking."""

from datetime import datetime, timezone
import json
import re
from .store import digest


def resolve_entry(store, identifier, *, kind=None, at=None):
    text = str(identifier)
    proof = store.conn.execute("SELECT entry_id,content_sha256 FROM diary_source_refs WHERE id=?", (text,)).fetchone()
    match = re.fullmatch(r"(?:diary-vps-)?(\d+)", text)
    if proof:
        rows = store.conn.execute("SELECT * FROM diary_entries WHERE id=?", (proof["entry_id"],)).fetchall()
    elif match:
        rows = store.conn.execute("SELECT * FROM diary_entries WHERE id=?", (int(match[1]),)).fetchall()
    else:
        source = "legacy_darkroom:" + text if text.startswith("dr_") else text
        rows = store.conn.execute("SELECT * FROM diary_entries WHERE source_id=?", (source,)).fetchall()
    result = {"target_id": text, "entry_id": None, "resolution": "missing"}
    if not rows:
        if text.startswith("diary_source_"):
            result["resolution"] = "unresolved_legacy_source"
        return result
    if len(rows) > 1:
        return {**result, "resolution": "ambiguous_source", "candidate_ids": [r["id"] for r in rows]}
    row = rows[0]
    result.update(entry_id=row["id"], kind=row["kind"])
    state = row["visibility"]
    if kind is not None and row["kind"] != kind:
        state = "wrong_kind"
    elif row["deleted_at"] or state == "deleted":
        state = "deleted"
    elif proof and digest(row["body_md"]) != proof["content_sha256"]:
        state = "historical_source"
    elif state == "active" and row["unlock_at"]:
        try:
            unlock = datetime.fromisoformat(row["unlock_at"])
            clock = datetime.fromisoformat(at) if isinstance(at, str) else at or datetime.now(timezone.utc)
            if unlock.tzinfo is None or clock.tzinfo is None:
                state = "unresolved_unlock_time"
            elif unlock > clock:
                state = "locked"
        except ValueError:
            state = "unresolved_unlock_time"
    result["resolution"] = state
    return result


def read_entry(store, identifier, *, kind=None, at=None):
    """Return readable current prose and comments, without exposing locked metadata."""
    result = resolve_entry(store, identifier, kind=kind, at=at)
    if result["resolution"] != "active":
        return {**result, "entry": None, "comments": []}
    row = store.conn.execute("SELECT metadata_json FROM diary_entries WHERE id=?", (result["entry_id"],)).fetchone()
    comments = [json.loads(r[0]) for r in store.conn.execute(
        "SELECT metadata_json FROM diary_comments WHERE entry_id=? ORDER BY created_at,id", (result["entry_id"],))]
    return {**result, "entry": json.loads(row[0]), "comments": comments}
