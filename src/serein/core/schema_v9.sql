CREATE TABLE personal_records (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    document_id TEXT REFERENCES documents(id),
    payload_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope,key)
);
CREATE INDEX personal_document ON personal_records(document_id,scope,deleted);
INSERT INTO personal_records(scope,key,document_id,payload_json,created_at,updated_at)
SELECT 'favorite',d.id,d.id,'{"favorite":true}',d.created_at,d.updated_at
FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision
WHERE json_extract(r.metadata_json,'$.favorite')=1
   OR EXISTS(SELECT 1 FROM json_each(r.metadata_json,'$.tags') WHERE value='serein_favorite');
PRAGMA user_version=9;
