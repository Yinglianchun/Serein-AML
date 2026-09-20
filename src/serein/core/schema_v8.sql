CREATE TABLE background_state (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE scene_jobs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id TEXT NOT NULL
);
CREATE TABLE injection_debug (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TRIGGER queue_new_scene AFTER INSERT ON documents WHEN NEW.kind='scene'
BEGIN
    INSERT INTO scene_jobs(scene_id) VALUES(NEW.id);
END;
CREATE TRIGGER queue_changed_scene AFTER UPDATE OF revision,lifecycle ON documents
WHEN NEW.kind='scene' AND (OLD.revision!=NEW.revision OR OLD.lifecycle!=NEW.lifecycle)
BEGIN
    INSERT INTO scene_jobs(scene_id) VALUES(NEW.id);
END;
PRAGMA user_version=8;
