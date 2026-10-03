-- Preserve replacement history even when an endpoint is excluded or absent.
CREATE TABLE event_replacements (
    predecessor_id TEXT PRIMARY KEY,
    successor_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

-- Historical receipts are retained; no settlement executor is attached here.
CREATE TABLE event_settlement_receipts (
    operation_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin TEXT NOT NULL
);

CREATE TABLE event_arc_links (
    arc_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (arc_key, event_id)
);

PRAGMA user_version = 4;
