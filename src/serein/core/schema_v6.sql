CREATE TABLE historical_works (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('shadow', 'dream')),
    revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_path TEXT NOT NULL
);

CREATE TABLE historical_work_events (
    origin TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    work_id TEXT NOT NULL,
    event TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (origin, line_number)
);

PRAGMA user_version = 6;
