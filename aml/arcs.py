"""Public material-only Arc organization with bounded format correction."""
from __future__ import annotations

import asyncio
import hashlib

from serein.compat.scout import Scout
from serein.compat.germany.narrative_revision_scout import (
    build_keyword_corridors, build_new_roll_candidate_prompt, normalize_new_roll_candidates,
)
from serein.compat.germany.narrative_scan import _bool_value
from serein.compat.narrative_candidates import add_current_entities, index_revision, supplement_candidates, VERSION
from serein.core.store import Store, encode
from serein.deployment import task_model
from serein.model_runtime import TaskClient


async def propose(settings, scout, model, corridors, rolls, fingerprint):
    from .runtime import stage_json, relaxed_content_review
    messages = build_new_roll_candidate_prompt(corridors, role_rules=scout.role_rules(),
                                               existing_candidates=[], existing_rolls=rolls)
    constraints = {'existing_narrative_ids': [row['narrative_id'] for row in rolls],
                   'existing_proposal_ids': []}
    reminder = ('\nHOST_IDS: ' + encode(constraints) +
        '\nUse only the material IDs provided above. New Arcs have empty target_narrative_id '
        'and existing_proposal_id, and materials must include the seed plus at least one other '
        'provided material. An existing target must be in HOST_IDS. Do not invent IDs. '
        'You may return candidates=[]; the host does not require a grouping decision.')
    messages[-1]['content'] += reminder
    with Store(settings.database) as store:
        store.conn.execute('CREATE TABLE IF NOT EXISTS aml_scout_attempts ('
            'id INTEGER PRIMARY KEY, input_sha256 TEXT, attempt INTEGER, raw_text TEXT, error TEXT)')
        if 'validation_json' not in {row['name'] for row in store.conn.execute('PRAGMA table_info(aml_scout_attempts)')}:
            store.conn.execute('ALTER TABLE aml_scout_attempts ADD COLUMN validation_json TEXT')
    client = TaskClient(settings.database, 'narrative_scout')
    try:
        for attempt in range(3):
            response = await client.create(model=model['model'], messages=messages,
                                            response_format={'type': 'json_object'}, temperature=0, store=False)
            raw = response.choices[0].message.content if response.choices else ''
            error = ''
            validation = None
            try:
                output = stage_json(raw, 'narrative_scout')
                candidates = normalize_new_roll_candidates(output, corridors, [], rolls)
                proposed = output['candidates']
                if relaxed_content_review():
                    # Public normalization already bounds material references and
                    # filters unusable candidates. Keep that safe subset without
                    # buying another reply to justify or restore rejected rows.
                    validation = {'policy': 'safe_subset', 'proposed': len(proposed),
                                  'accepted': len(candidates), 'candidates': candidates}
                elif len(proposed) > 24 or len(candidates) != len(proposed):
                    raise ValueError('Arc candidate has an invalid ID, incomplete materials, conflicting '
                                     'ownership, missing reason/title, or unsupported confidence. '
                                     'Use the supplied IDs and include the seed in materials.')
                # The public normalizer can discard unknown extra material refs.
                # Ask the model to correct these rather than silently hiding them.
                for item, normalized in ([] if relaxed_content_review() else zip(proposed, candidates)):
                    requested = {(str(row.get('source_type') or '').strip().lower(), str(row.get('source_id') or '').strip())
                                 for row in item.get('materials') or [] if isinstance(row, dict)}
                    accepted = {(kind, str(key)) for kind in ('event', 'scene', 'diary')
                                for key in normalized[f'source_{kind}_ids']}
                    if requested != accepted:
                        raise ValueError('materials contains a reference outside its supplied seed corridor')
            except (ValueError, KeyError, TypeError) as rejected:
                error = str(rejected)
            with Store(settings.database) as store:
                store.conn.execute('INSERT INTO aml_scout_attempts(input_sha256,attempt,raw_text,error,validation_json) '
                                   'VALUES (?,?,?,?,?)', (fingerprint, attempt, raw, error, encode(validation)))
            if not error:
                return candidates
            if attempt == 2:
                raise RuntimeError('Public Arc Scout output did not pass validation: ' + error)
            messages = [*messages[:2], {'role': 'assistant', 'content': raw[:10000]},
                        {'role': 'user', 'content': 'Host validation failed: ' + error +
                         '\nReturn the complete corrected JSON. Preserve the grounded semantic decision; '
                         'do not force a grouping. Use only the original input IDs.'}]
    finally:
        await client.close()


async def scan(settings):
    scout = Scout(settings)
    previous = scout.inbox.scan_metadata()
    # Keep public retired-binding cleanup and stale authored-volume hints.
    result = await scout._scan_narrative_revision_inbox(include_external=False)
    config = scout._narrative_revision_scan_settings(scout.config)
    inventory = await scout._active_narrative_material_inventory(exclude_covered_events=True)
    hybrid = _bool_value((scout.config.get('narrative_rolls') or {}).get('hybrid_candidates_enabled'), True)
    if hybrid and inventory:
        inventory = await asyncio.to_thread(add_current_entities, settings, inventory)
    seeds = sorted((row for row in inventory if row.get('is_unbound')),
                   key=scout._narrative_seed_sort_key, reverse=True)[:config['new_roll_scout_seed_limit']]
    rolls = [{key: row.get(key) for key in ('narrative_id', 'title', 'query_cues', 'current_status_cue')}
             for row in scout.rolls._load()
             if row.get('integrity_status') == 'ok' and row.get('lifecycle') == 'active']
    fingerprint = hashlib.sha256(encode(['aml-arc-format-v1', VERSION, hybrid,
        settings.embedding if hybrid else None,
        [config[key] for key in ('new_roll_scout_seed_limit', 'new_roll_scout_keywords_per_seed',
                                'new_roll_scout_candidates_per_seed')],
        index_revision(settings) if hybrid else None,
        [(row['source_type'], row['source_id'], row.get('updated_at', ''), row.get('fingerprint', ''),
          sorted(row.get('bound_narrative_ids') or []), sorted(row.get('entity_names') or []))
         for row in sorted(inventory, key=lambda row: (row['source_type'], row['source_id']))],
        sorted(rolls, key=lambda row: row['narrative_id'])]).encode()).hexdigest()
    model = task_model(settings.database, 'narrative_scout')
    status = 'no_materials' if not seeds else 'unchanged' if fingerprint == previous.get('external_input_sha256') else 'unavailable' if not model else 'ok'
    changes = []
    search = {'version': VERSION, 'semantic': {'status': 'not_run', 'failed_queries': 0}}
    try:
        if status == 'ok':
            hydrated = scout._hydrate_scout_seeds(seeds)
            by_key = {f"{row['source_type']}:{row['source_id']}": row for row in hydrated}
            search_inventory = [by_key.get(f"{row['source_type']}:{row['source_id']}", row) for row in inventory]
            corridors = build_keyword_corridors(search_inventory, list(by_key),
                max_keywords=config['new_roll_scout_keywords_per_seed'],
                max_candidates_per_seed=config['new_roll_scout_candidates_per_seed'])
            if hybrid:
                corridors, search = await supplement_candidates(settings, search_inventory, corridors)
            corridors = scout._hydrate_scout_corridors(corridors)
            candidates = await propose(settings, scout, model, corridors, rolls, fingerprint) if corridors else []
            changes = scout.apply_arc_candidates(candidates, model=model['model'])
            status = 'ok' if corridors else 'no_keyword_matches'
    except Exception:
        result.update(external_scout_status='error', external_input_sha256=previous.get('external_input_sha256', ''))
        scout.inbox.record_scan(result)
        raise
    recorded = fingerprint if status != 'unavailable' and search['semantic']['status'] not in {'partial', 'failed'} else previous.get('external_input_sha256', '')
    result.update(active_materials_searched=len(inventory), unbound_material_seeds_checked=len(seeds),
        unbound_events_checked=sum(row['source_type'] == 'event' for row in seeds),
        unbound_scenes_checked=sum(row['source_type'] == 'scene' for row in seeds),
        unbound_diaries_checked=sum(row['source_type'] == 'diary' for row in seeds),
        existing_arcs_updated=sum(row['type'] == 'existing_arc_materials' for row in changes),
        new_collecting_arcs_created=sum(row['type'] == 'new_collecting_arc' for row in changes),
        external_scout_status=status, external_model=(model or {}).get('model', ''),
        external_base_url=(model or {}).get('base_url', ''), candidate_search=search,
        external_input_sha256=recorded)
    result['narrative_writes_performed'].extend(changes)
    scout.inbox.record_scan(result)
    return result
