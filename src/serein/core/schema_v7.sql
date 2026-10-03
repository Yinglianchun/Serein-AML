CREATE TABLE write_receipts (
    operation_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE index_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL
);
CREATE TABLE memory_candidates (
    id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','dismissed')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
PRAGMA user_version=7;
