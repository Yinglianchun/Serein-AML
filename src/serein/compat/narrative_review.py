"""Explicit inbox decisions over the same transaction as Narrative membership."""

from copy import deepcopy

from ..core.store import now
from .narratives import RevisionInbox


def review_revision(rolls, proposal_id, *, action, draft_delta='', note=''):
    inbox = RevisionInbox(rolls.store)
    raw = inbox._load()
    item = next((p for p in raw['items'] if p['proposal_id'] == proposal_id), None)
    if item is None:
        return {'status': 'not_found', 'proposal_id': proposal_id}
    if action in ('save_line', 'write'):
        return save_candidate_line(rolls, inbox, raw, item, action)
    if action != 'dismiss' or item.get('proposal_kind') == 'new_roll_candidate':
        return inbox.review(proposal_id, action=action, draft_delta=draft_delta, note=note)
    if item.get('status') == 'absorbed':
        return {'status': 'conflict', 'reason': 'revision_already_absorbed'}
    if not item.get('material_withdrawal_done'):
        result = withdraw_additions(rolls, item)
        if result['status'] == 'conflict':
            return result
        item['withdrawn_material_ids'] = result['removed']
        item['material_withdrawal_done'] = True
    item.update(status='dismissed', review_note=note, reviewed_at=now(), updated_at=now())
    inbox._save(raw)
    return {'status': 'updated', 'item': item, 'removed_material_ids': item['withdrawn_material_ids']}


def withdraw_additions(rolls, item):
    """Withdraw only this hint's additions, never sources of the published body."""
    key = item.get('narrative_id')
    current = rolls.read(key)
    if current.get('status') != 'ok':
        return {'status': 'conflict', 'reason': 'narrative_unavailable'}
    if current['document_sha256'] != item.get('baseline_document_sha256'):
        return {'status': 'conflict', 'reason': 'narrative_revision_changed'}
    entry = deepcopy(rolls.store.read(key)['metadata']['legacy_registry'])
    previous = deepcopy(entry)
    removed = {'event_ids': [], 'scene_ids': []}
    for kind in ('event', 'scene'):
        # These sources were already present when the written revision was saved.
        published = set(current.get(f'linked_{kind}_ids') or [])
        for source_id in item.get(f'source_{kind}_ids') or []:
            if source_id in published:
                continue
            if kind == 'event':
                link = rolls.store.conn.execute('SELECT 1 FROM fact_event_arc_links WHERE arc_key=? AND event_id=?',
                                                (entry['arc_key'], source_id)).fetchone()
                if not link:
                    continue
                for table in ('fact_event_arc_links', 'event_arc_links'):
                    rolls.store.conn.execute(f'DELETE FROM {table} WHERE arc_key=? AND event_id=?',
                                             (entry['arc_key'], source_id))
                removed['event_ids'].append(source_id)
                excluded = entry.setdefault('excluded_event_ids', [])
                if source_id not in excluded:
                    excluded.append(source_id)
    if any(removed.values()):
        previous.pop('history', None)
        entry['history'] = [*entry.get('history', []), previous]
        entry['revision'] += 1
        entry['source_file'] = f"sqlite:{key}/revision-{entry['revision']:04d}"
        # Membership changes invalidate previews, but do not refresh the body's
        # publication date or manufacture a new narrative text.
        rolls._persist(entry, current['full_document'])
    return {'status': 'updated', 'removed': removed}


def save_candidate_line(rolls, inbox, raw, item, action):
    if item.get('proposal_kind') != 'new_roll_candidate':
        return {'status': 'invalid', 'reason': 'not_a_new_roll_candidate'}
    existing = item.get('resolved_narrative_id')
    if existing:
        return {'status': 'updated', 'narrative_id': existing, 'next_action': action, 'item': item}
    if item.get('status') != 'pending':
        return {'status': 'conflict', 'reason': 'candidate_not_pending'}
    sources = {kind: list(dict.fromkeys(item.get(f'source_{kind}_ids') or [])) for kind in ('event', 'scene')}
    dates = []
    for kind, ids in sources.items():
        for key in ids:
            source = rolls.store.read(key)
            if not source or source['kind'] != kind or source['lifecycle'] != 'active':
                return {'status': 'conflict', 'reason': 'candidate_material_unavailable', 'source_id': key}
            date = str(source['metadata'].get('local_date') or source['metadata'].get('date') or '')[:10]
            if date:
                dates.append(date)
    key = 'narrative_' + item['proposal_id']
    title = item.get('narrative_title') or item['source_title']
    document = f'# {title}\n\n## 第一人称叙事\n\n## 来源账\n\n' + '\n'.join(
        f'- {kind}: {key}' for kind, ids in sources.items() for key in ids) + '\n'
    result = rolls.publish(narrative_id=key, document=document, expected_revision=0, title=title,
        arc_key='arc:' + item['proposal_id'], source_event_ids=sources['event'], source_scene_ids=sources['scene'],
        query_cues=[title], time_start=min(dates) if dates else '', time_end=max(dates) if dates else '',
        current_status_cue=item.get('source_excerpt') or '', publication_status='collecting')
    if result['status'] != 'created':
        return result
    item.update(status='absorbed', resolution='saved_line', resolved_narrative_id=key,
                absorbed_revision=result['revision'], reviewed_at=now(), updated_at=now())
    inbox._save(raw)
    return {'status': 'created', 'narrative_id': key, 'next_action': action, 'item': item}
