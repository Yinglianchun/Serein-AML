"""Export historical Window Shadows and dreams without their old runtime."""

import base64
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export_archives(state):
    state = Path(state).resolve()
    with sqlite3.connect((state / "window_shadows.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        shadows = [dict(row) for row in db.execute("SELECT * FROM window_shadows ORDER BY window_id")]
    root = state / "dreams"
    if not root.is_dir():
        raise FileNotFoundError(root)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ValueError("Dream source leaves the selected directory")
            raw = path.read_bytes()
            files.append({"path": path.relative_to(state).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes_b64": base64.b64encode(raw).decode("ascii")})
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"format": "serein-archive-snapshot-v1", "origin": "legacy-archives-" + timestamp,
            "captured_at": timestamp, "window_shadows": shadows, "files": files,
            "consistency": "Read-only Window Shadow transaction; dream files read separately"}
