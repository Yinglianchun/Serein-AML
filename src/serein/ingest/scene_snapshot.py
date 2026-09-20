"""Read-only legacy Scene export. Runs with Python and PyYAML on either host."""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import yaml


SCENE_CONTRACTS = {"scene-migration-v2", "write-scene-v1", "close-window-scene-v2", "close-window-scene-v3"}


def _artifact(root, path, raw):
    return {"path": path.relative_to(root).as_posix(),
            "bytes_b64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest()}


def _read_tables(path, tables):
    # Never create or initialize a legacy database, even if a path is wrong.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        return {table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
                for table in tables}
    finally:
        conn.close()


def export_snapshot(buckets, state):
    buckets, state = Path(buckets), Path(state)
    if not buckets.is_dir() or not state.is_dir():
        raise ValueError("Both legacy buckets and state directories must exist")
    scenes, excluded = [], []
    for directory in ("dynamic", "permanent", "archive", "feel"):
        for path in sorted((buckets / directory).rglob("*.md")):
            raw = path.read_bytes()
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", raw.decode("utf-8"), re.S)
            meta = (yaml.safe_load(match.group(1)) or {}) if match else {}
            if isinstance(meta, dict) and meta.get("write_contract") in SCENE_CONTRACTS:
                scenes.append(_artifact(buckets, path, raw))
            else:
                excluded.append({"path": path.relative_to(buckets).as_posix(),
                                 "id": meta.get("id") if isinstance(meta, dict) else None,
                                 "reason": "not_an_identified_authored_scene"})
    evidence = _read_tables(state / "scene_evidence.sqlite", ["scene_evidence", "scene_evidence_events"])
    relations = _read_tables(state / "scene_edge_proposals.sqlite", ["scene_edges", "scene_edge_proposals"])
    tombstones = [_artifact(buckets, path, path.read_bytes())
                  for path in sorted((buckets / ".tombstones").glob("*.json"))]
    stamp = datetime.now(timezone.utc).isoformat()
    return {"format": "serein-scene-snapshot-v1", "origin": "legacy-scenes-" + stamp,
            "captured_at": stamp, "scope": "all identified authored Scenes, evidence, relations, and deletion markers",
            "consistency": "per-database read transactions; live Markdown captured without stopping writers",
            "scenes": scenes, "excluded_documents": excluded,
            "evidence": evidence["scene_evidence"], "evidence_actions": evidence["scene_evidence_events"],
            "scene_relations": relations["scene_edges"], "scene_proposals": relations["scene_edge_proposals"],
            "tombstones": tombstones}


def write_snapshot(snapshot, target):
    # x mode prevents an old rehearsal input from being overwritten.
    with Path(target).open("x", encoding="utf-8") as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
