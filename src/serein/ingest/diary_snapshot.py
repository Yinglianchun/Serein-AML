"""Read the canonical diary database and retain older Darkroom artifacts."""

import base64
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export_diaries(state):
    state = Path(state).resolve()
    with sqlite3.connect((state / "diary.db").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {name: [dict(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY id")]
                  for name in ("diaries", "comments", "diary_revisions", "darkroom_sessions")}
    files, absent = [], []
    for name in ("darkroom/entries.jsonl", "darkroom/state.json"):
        path = state / name
        if not path.is_file():
            absent.append(name)
            continue
        raw = path.read_bytes()
        files.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                      "bytes_b64": base64.b64encode(raw).decode("ascii")})
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"format": "serein-diary-snapshot-v1", "origin": "legacy-diaries-" + timestamp,
            "captured_at": timestamp, **tables, "legacy_files": files, "absent_legacy_files": absent,
            "consistency": "One read-only diary transaction; legacy files read separately"}
