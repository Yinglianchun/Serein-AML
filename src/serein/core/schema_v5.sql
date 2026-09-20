-- Notebook identity remains the original integer ID, independent of Scene IDs.
CREATE TABLE diary_entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    author TEXT NOT NULL,
    day TEXT NOT NULL,
    title TEXT,
    body_md TEXT NOT NULL,
    visibility TEXT NOT NULL,
    unlock_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX diary_by_source ON diary_entries(source_id);

-- Original owner IDs also survive if an old owner is absent; import reports it.
CREATE TABLE diary_comments (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX diary_comments_by_entry ON diary_comments(entry_id);

CREATE TABLE diary_history (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    body_md TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX diary_history_by_entry ON diary_history(entry_id, revision);

CREATE TABLE diary_sessions (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER,
    locked_at TEXT NOT NULL,
    unlock_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

-- A legacy source ledger can identify an exact diary snapshot, not just a day.
CREATE TABLE diary_source_refs (
    id TEXT PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

PRAGMA user_version = 5;
