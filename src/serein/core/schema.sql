PRAGMA foreign_keys = ON;

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('scene', 'event', 'narrative')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'archived', 'superseded', 'deleted')),
    manual_surface INTEGER CHECK (manual_surface IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE revisions (
    document_id TEXT NOT NULL REFERENCES documents(id),
    number INTEGER NOT NULL CHECK (number > 0),
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (document_id, number)
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE (source_key, content_sha256)
);

CREATE TABLE evidence_bindings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL
);
CREATE INDEX evidence_by_document ON evidence_bindings(document_id, active);

CREATE TABLE evidence_actions (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES evidence_bindings(id),
    action TEXT NOT NULL CHECK (action IN ('bind', 'unbind')),
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE deletions (
    document_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

-- Original bytes are retained independently of normalized rows.
CREATE TABLE import_records (
    origin TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (origin, path)
);

PRAGMA user_version = 1;
PRAGMA application_id = 1397902897;
