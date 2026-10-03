-- Legacy references are preserved even when an endpoint was deleted or excluded.
-- They are not foreign keys to newly invented placeholder documents.
CREATE TABLE scene_relations (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    source_scene_id TEXT NOT NULL,
    target_scene_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL
);

CREATE TABLE scene_proposals (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    source_scene_id TEXT NOT NULL,
    target_scene_id TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE detached_import_records (
    origin TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (origin, record_type, record_id)
);

PRAGMA user_version = 2;
