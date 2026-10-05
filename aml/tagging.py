"""Drain the unchanged public metadata tagger before an opt-in AML Add settles."""
from __future__ import annotations

from contextvars import ContextVar

from serein.core.store import Store
from serein.deployment import task_model
from serein.imports import initialize_imports
from serein import import_tagging, model_runtime


_ACTIVE = ContextVar("aml_tagging_request", default=False)


def transport_policy():
    # Public tag_one imports complete inside each task. Install a persistent,
    # context-local shim; restoring a global function would race other users.
    # Calls outside this AML operation keep their original payload and transport.
    previous = model_runtime.complete
    if getattr(previous, "_aml_tagging_policy", False):
        return

    async def complete(model, payload, **kwargs):
        if _ACTIVE.get():
            payload = {**payload, "store": False}
        return await previous(model, payload, **kwargs)

    complete._aml_tagging_policy = True
    model_runtime.complete = complete


def queue(database):
    with Store(database, read_only=True) as store:
        outbox = store.conn.execute("SELECT count(*) FROM tagging_outbox").fetchone()[0]
        jobs = {row["status"]: row["n"] for row in store.conn.execute(
            "SELECT j.status,count(*) n FROM import_tag_jobs j JOIN documents d ON d.id=j.document_id "
            "JOIN revisions r ON r.document_id=d.id AND r.number=d.revision "
            "WHERE j.upload_id='' AND d.kind IN ('event','scene') AND d.lifecycle='active' "
            "AND COALESCE(json_extract(r.metadata_json,'$.legacy_tagging_pending'),0)=0 GROUP BY j.status")}
    return outbox, jobs


async def run(settings):
    if not task_model(settings.database, "operit_tagging"):
        raise RuntimeError("Public memory tagging model is not configured")
    initialize_imports(settings.database)
    # Each invocation is an explicit Add attempt, like the public retry endpoint.
    # Retain cumulative attempt counts; never retry a paid failure in this loop.
    with Store(settings.database) as store:
        store.conn.execute("UPDATE import_tag_jobs SET status='pending' "
                           "WHERE upload_id='' AND status='failed'")
    transport_policy()
    token = _ACTIVE.set(True)
    try:
        for _ in range(1000):
            await import_tagging.process(settings.database)
            outbox, jobs = queue(settings.database)
            if jobs.get("failed", 0):
                raise RuntimeError("Public memory tagging did not complete; retry this Add")
            if not outbox and not jobs.get("pending", 0) and not jobs.get("stale", 0):
                return {"status": "ok", "tagged": jobs.get("done", 0)}
            # A stale version without a new outbox entry has no runnable work.
            # Stop instead of acknowledging incomplete metadata or busy-waiting.
            if not outbox and not jobs.get("pending", 0):
                raise RuntimeError("Public memory tagging has stale work; retry this Add")
        raise RuntimeError("Public memory tagging batch limit reached; retry this Add")
    finally:
        _ACTIVE.reset(token)


async def refresh(settings):
    from serein.compat.entity_scope import update_scope
    from serein.recall.entities import rebuild_entities

    entities = update_scope(settings)
    result = {"entities": rebuild_entities(settings, legacy_rows=entities)}
    if (settings.embedding and settings.background.get("germany_config_file") and
            settings.recall.get("germany_policy_file")):
        from serein.recall.legacy_indexes import refresh as refresh_legacy

        # Scene cues use the public binder only where the public tagger requested
        # them. Existing bodies/cues and failed binding pause rules stay intact.
        transport_policy()
        token = _ACTIVE.set(True)
        try:
            result["legacy"] = await refresh_legacy(settings)
        finally:
            _ACTIVE.reset(token)
        # Keep the public binder's durable pause/retry behavior. Its safe status
        # is retained in the Add receipt rather than silently resetting retries.
    return result
