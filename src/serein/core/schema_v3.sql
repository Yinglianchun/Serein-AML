CREATE TABLE narrative_uploads (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY (origin, path) REFERENCES import_records(origin, path)
);

-- A declaration, an appended Arc link, and an ID merely mentioned in prose
-- have different authority. Missing targets do not become placeholder objects.
CREATE TABLE narrative_materials (
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    locator TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('linked', 'excluded', 'mentioned', 'appended')),
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (document_id, revision, locator, kind, target_id),
    FOREIGN KEY (document_id, revision) REFERENCES revisions(document_id, number)
);

CREATE TABLE narrative_proposals (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    narrative_id TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

PRAGMA user_version = 3;
