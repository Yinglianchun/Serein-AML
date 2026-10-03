"""Explicit benchmark model profiles over the unchanged public runtime."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import hashlib
import json
import os
import re
import httpx

from serein.config import Settings
from serein.configured_models import effective_settings, memory_ready, prepare_selected
from serein.core.store import Store, encode
from serein.deployment import configured_models, read_settings, save_settings
from serein.extensions import pipeline


ACTIVE_DATABASE = ContextVar("aml_database", default=None)
GENERATIVE_ROLES = ("track_router", "event_curator", "event_writer", "writer", "narrative_scout")
CURATOR_FORMAT = """\nJSON structure reminder (no change to the role or evidence rules):
Each decision_review.events item has event_index and reason. Do NOT add an
evidence field to those items. Optional materials/admission use only the exact
schema in the original task. Verbatim evidence belongs to boundaries,
dispositions or bridge_exclusions, in their original specified shapes.
Return only the original task's JSON object, without additional fields.
"""


class ProfileConflict(ValueError):
    """Development data cannot silently become a competition database."""


def configuration():
    profile = os.getenv("SEREIN_AML_PROFILE", "competition")
    if profile not in {"development", "competition"}:
        raise ValueError("SEREIN_AML_PROFILE must be development or competition")
    filename = os.getenv("SEREIN_AML_MODEL_CONFIG", "")
    source = {"models": [], "upstreams": [], "assignments": {}}
    if filename:
        with open(filename, encoding="utf-8") as stream:
            source = json.load(stream)
    catalog = {m["id"]: m for m in configured_models(source)}
    assigned = source.get("assignments") or {}
    models, assignments = {}, {}
    for role in ("embedding", "reranker"):
        if assigned.get(role):
            model = catalog[assigned[role]]
            models[model["id"]] = model
            assignments[role] = model["id"]
    if profile == "development":
        if not filename:
            raise ValueError("Development requires SEREIN_AML_MODEL_CONFIG")
        for role in GENERATIVE_ROLES:
            key = assigned.get(role) or assigned.get("writer") or assigned.get("event_writer")
            model = catalog[key]
            if model["model"].split("/")[-1].startswith("gpt-4o-mini"):
                raise ValueError("Development roles must use a model other than gpt-4o-mini")
            models[model["id"]] = model
            assignments[role] = model["id"]
    else:
        key = os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("OR_key", "").strip()
        base = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
        if not base:
            base = "https://api.openai.com/v1" if os.getenv("OPENAI_API_KEY") else "https://openrouter.ai/api/v1"
        if not key:
            raise ValueError("Competition requires OPENAI_API_KEY or OR_key")
        model = {"id": "aml-mini", "model": "openai/gpt-4o-mini" if "openrouter.ai" in base else "gpt-4o-mini",
                 "base_url": base, "api_key": key, "protocol": "openai"}
        models[model["id"]] = model
        assignments.update({role: model["id"] for role in GENERATIVE_ROLES})
    # Copy only selected model connections, never features, identity or deployment data.
    fields = ("id", "model", "base_url", "api_key", "protocol", "dimension",
              "query_instruction", "document_instruction", "request_timeout_seconds")
    changes = {"models": [{k: m[k] for k in fields if k in m} for m in models.values()],
               "assignments": assignments,
               "pipeline": {"execution_mode": "api", "auto_enabled": True},
               "recall": {"passages_enabled": True},
               "features": {"narrative_nightly_organize": os.getenv("SEREIN_AML_ORGANIZE_ARCS", "0") == "1"}}
    public_profile = {"profile": profile, "pipeline_adapter": "stage-format-v4", "assignments": assignments,
                      "models": [{k: v for k, v in m.items() if k != "api_key"} for m in changes["models"]]}
    signature = hashlib.sha256(encode(public_profile).encode()).hexdigest()
    return changes, {"profile": profile, "signature": signature}


def check_profile(database):
    with Store(database, read_only=True) as store:
        row = store.conn.execute("SELECT value_json FROM background_state WHERE name='aml_profile'").fetchone()
    if row:
        changes, expected = configuration()
        if json.loads(row[0]) != expected:
            raise ProfileConflict("Use a fresh SEREIN_AML_DATA_DIR when changing model profiles")
        current = read_settings(database)
        catalog = {model['id']: model for model in configured_models(current)}
        selected = {model['id']: model for model in changes['models']}
        for role in (*GENERATIVE_ROLES, 'embedding', 'reranker'):
            identifier = changes['assignments'].get(role) or ''
            if (current['assignments'].get(role) or '') != identifier:
                raise ProfileConflict("Database model assignments no longer match the AML profile")
            if identifier and any(catalog.get(identifier, {}).get(key) != value
                                  for key, value in selected[identifier].items() if key != 'api_key'):
                raise ProfileConflict("Database model configuration no longer matches the AML profile")


def bootstrap(paths):
    changes, marker = configuration()
    with Store(paths.database) as store:
        row = store.conn.execute("SELECT value_json FROM background_state WHERE name='aml_profile'").fetchone()
        if row and json.loads(row[0]) != marker:
            raise ProfileConflict("Use a fresh SEREIN_AML_DATA_DIR when changing model profiles")
        if not row:
            raw_table = store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_events'").fetchone()
            if store.conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone() or (raw_table and store.conn.execute("SELECT 1 FROM raw_events LIMIT 1").fetchone()):
                raise ProfileConflict("The public pipeline requires a fresh AML database")
    current = read_settings(paths.database)
    if any(current.get(k) != value for k, value in changes.items() if k in ("models", "assignments")) or any(
            current[k].get(name) != value for k in ("features", "pipeline", "recall") for name, value in changes[k].items()):
        save_settings(paths.database, changes)
    with Store(paths.database) as store:
        store.conn.execute("INSERT OR REPLACE INTO background_state VALUES ('aml_profile',?)", (encode(marker),))
    from serein.bootstrap import initialize
    settings = Settings(database=paths.database, index=paths.index, writable=True)
    initialize(settings)
    pipeline.initialize(paths.database)
    return settings


def prepare_memory(settings):
    """Preparation is explicitly part of this opt-in benchmark ingestion."""
    if read_settings(settings.database)["assignments"].get("embedding") and not memory_ready(settings):
        prepare_selected(settings)
    return effective_settings(settings)


async def ingest_pipeline(settings):
    results = []
    seen = set()
    # Consume all completed public batches. Deferred/user-only tails remain originals.
    for _ in range(100):
        try:
            result = await pipeline.advance(settings.database, include_recent=True,
                                            runner=lambda role, request: run_stage(settings, role, request))
        except (ValueError, httpx.HTTPError, TimeoutError) as error:
            raise RuntimeError("Public Event pipeline did not complete") from error
        results.append(result)
        if result["status"] == "current":
            with Store(settings.database, read_only=True) as store:
                blocked = store.conn.execute("SELECT 1 FROM pipeline_batches WHERE status IN ('paused_failure','needs_repair','pending') LIMIT 1").fetchone()
            if blocked:
                raise RuntimeError("Public Event pipeline has an unfinished or paused batch")
            return results
        if result["status"] != "processed":
            raise RuntimeError("Public Event pipeline did not settle: " + result["status"])
        if result["batch_id"] in seen or not result["processed_originals"]:
            return results
        seen.add(result["batch_id"])
    raise RuntimeError("Public Event pipeline batch limit reached; retry this Add")


async def run_stage(settings, role, request):
    """Public runner seam: clarify JSON shape, then enforce public validation."""
    from serein.model_runtime import complete
    config = pipeline.snapshot(settings.database, request['batch_id'])
    model = config['models'][role]
    reminder = ''
    if role == 'event_curator':
        component = request['component']
        stable = {message['id'] for message in component['messages']}
        roots = sorted({unit['unit_root_message_id'] for unit in component['memberships']
                        if set(unit['source_message_ids']) & stable})
        constraints = {'stable_unit_roots': roots, 'primary_track_ids':component['track_ids'],
                       'available_base_event_ids':[item['event_id'] for item in component['base_event_candidates']]}
        reminder = CURATOR_FORMAT + """
Host structure constraints for this frozen task are below. These are IDs, not
evidence or a requested semantic decision. Account for every stable unit root
in the top-level events.owned_unit_roots, skip_unit_roots, or defer_unit_roots.
A decision_review disposition alone does NOT account for a skipped/deferred unit.
create requires zero base_event_ids; extend/rewrite require exactly one available
base; merge requires at least two available bases. Never use merge with no base.
Use grounded activity reasons, never copy placeholder reasons from the schema.
Keep the original bridge-overlap and parked/context-only rules.
HOST_IDS:
""" + encode(constraints)
    prompt = request['prompt'] + reminder
    maximum = config['policy']['max_prompt_chars']
    if len(prompt) + len(request['rules']) > maximum:
        raise ValueError('Public stage prompt limit exceeded')
    timeout = config['policy']['timeout_seconds']
    with Store(settings.database, read_only=True) as store:
        identifier = store.conn.execute("SELECT id FROM pipeline_jobs WHERE batch_id=? AND "
            "json_extract(request_json,'$.role')=? AND output_json IS NULL ORDER BY rowid DESC LIMIT 1",
            (request['batch_id'], role)).fetchone()[0]
    # The host owns durable jobs and settlement. Archive the exact reply; only
    # normalize its representation before enforcing the public semantic rules.
    for attempt in range(3):
        raw = ''
        received = False
        try:
            response = await asyncio.wait_for(complete({**model, 'request_timeout_seconds':timeout}, {
                'messages':[{'role':'system','content':request['rules']}, {'role':'user','content':prompt}],
                'response_format':{'type':'json_object'}, 'store':False}), timeout=timeout+20)
            received = True
            raw = response['choices'][0]['message']['content']
            output = pipeline.prepare_stage_output(request, stage_json(raw, role))
            pipeline.validate(request, output)
            pipeline.record_attempt(settings.database, identifier, raw)
            return output
        except ValueError as error:
            pipeline.record_attempt(settings.database, identifier, raw, str(error))
            if not received or attempt == 2:
                raise
            correction = '\nHost validation failed. Keep the original IDs and evidence; return the full corrected JSON.\n' + str(error)
            room = maximum - len(request['rules']) - len(request['prompt']) - len(reminder) - len(correction) - 80
            if room < 0:raise
            prompt = request['prompt'] + reminder + correction + '\nPrevious invalid output:\n' + raw[:min(room,10000)]
    raise RuntimeError('Public stage validation did not complete')


def stage_json(raw, role):
    """Tolerate formatting, never infer ownership, evidence or a decision."""
    text = str(raw).strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, flags=re.DOTALL | re.IGNORECASE)
    value = json.loads(fenced.group(1) if fenced else text)
    if isinstance(value, dict) and set(value) in ({'result'}, {'output'}):
        wrapped = next(iter(value.values()))
        if isinstance(wrapped, dict):value = wrapped
    integers = {'event_index', 'left_event_index', 'right_event_index', 'sentence_index',
                'source_message_id', 'unit_root_message_id', 'before_message_id', 'input_image'}
    integer_lists = {'owned_unit_roots', 'skip_unit_roots', 'defer_unit_roots',
                     'source_message_ids', 'parked_source_message_ids', 'picks'}
    def number(item):
        return int(item) if isinstance(item, str) and re.fullmatch(r'[0-9]+', item) else item
    def visit(item):
        if isinstance(item, list):return [visit(child) for child in item]
        if not isinstance(item, dict):return item
        return {key: number(child) if key in integers else
                [number(part) for part in child] if key in integer_lists and isinstance(child, list) else visit(child)
                for key, child in item.items()}
    value = visit(value)
    if role == 'event_curator' and isinstance(value, dict):
        review = value.get('decision_review')
        if isinstance(review, dict) and isinstance(review.get('events'), list):
            # These rows are activity reasons, not source-evidence receipts.
            # Boundary/disposition evidence and all ownership lists stay intact.
            allowed = {'event_index', 'reason', 'materials', 'admission'}
            review['events'] = [{key:child for key,child in item.items() if key in allowed}
                                if isinstance(item, dict) else item for item in review['events']]
    return value


async def organize_arcs(settings):
    if not read_settings(settings.database)["features"]["narrative_nightly_organize"]:
        return {"external_scout_status": "disabled"}
    from .arcs import scan
    result = await scan(settings)
    if result["external_scout_status"] in {"error", "unavailable"}:
        raise RuntimeError("Public Arc Scout did not complete")
    # The public automatic path creates collecting lines and material links only.
    # Prose keeps the public explicit authoring/preview/save boundary.
    return result


@contextmanager
def active(database):
    token = ACTIVE_DATABASE.set(database)
    try:
        yield
    finally:
        ACTIVE_DATABASE.reset(token)
