"""Read legacy Narrative files and Arc links without opening legacy writers."""

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export_narratives(state):
    state = Path(state).resolve()
    root = state / "narrative_rolls"
    files = []
    # Registry pointers determine published revisions; directory contents are
    # also retained, but never promoted merely because a file exists.
    for name in ("registry.json", "revision_inbox.json", "uploads/index.json"):
        if not (root / name).is_file():
            raise FileNotFoundError(root / name)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Narrative file leaves source root: {path}")
        raw = path.read_bytes()
        files.append({"path": path.relative_to(root).as_posix(),
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "bytes_b64": base64.b64encode(raw).decode("ascii")})
    with sqlite3.connect((state / "fact_events.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        links = [dict(row) for row in db.execute(
            "SELECT l.* FROM fact_event_arc_links l JOIN fact_events e "
            "ON e.item_id=l.event_id WHERE e.item_type='event' ORDER BY l.arc_key,l.event_id"
        )]
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"format": "serein-narrative-snapshot-v1", "origin": "legacy-narratives-" + timestamp,
            "captured_at": timestamp, "files": files, "arc_event_links": links,
            "consistency": "Live files plus a read-only Arc database transaction; not a global snapshot"}
