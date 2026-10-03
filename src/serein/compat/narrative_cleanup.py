"""Retire material bindings without publishing text or creating revision hints."""

from copy import deepcopy

from .narratives import RevisionInbox
from ..core.store import now


def retired_bindings(rolls):
    result = []
    for current in rolls._load():
        if current.get('integrity_status') != 'ok':
            continue
        arc = current.get('arc_key') or ''
        events = set(current.get('linked_event_ids') or [])
        for table in ('fact_event_arc_links', 'event_arc_links'):
            events.update(row[0] for row in rolls.store.conn.execute(
                f'SELECT event_id FROM {table} WHERE arc_key=?', (arc,)))
        removed = {'event_ids': [], 'scene_ids': []}
        for key in sorted(events):
            event = rolls.store.conn.execute('SELECT status FROM fact_events WHERE item_id=?', (key,)).fetchone()
            if event and event['status'] in ('archived', 'superseded'):
                removed['event_ids'].append(key)
        for key in current.get('linked_scene_ids') or []:
            scene = rolls.store.read(key)
            if scene and scene['kind'] == 'scene' and scene['lifecycle'] == 'archived':
                removed['scene_ids'].append(key)
        if any(removed.values()):
            result.append({'narrative_id': current['narrative_id'], 'title': current['title'], 'removed': removed})
    return result


def cleanup_retired_bindings(rolls):
    """Caller owns the write transaction; missing/deleted sources stay for review."""
    changes = retired_bindings(rolls)
    inbox = RevisionInbox(rolls.store)
    raw = inbox._load()
    hints_changed = False
    for change in changes:
        key, removed = change['narrative_id'], change['removed']
        current = rolls.read(key)
        entry = deepcopy(rolls.store.read(key)['metadata']['legacy_registry'])
        previous = deepcopy(entry)
        for kind in ('event', 'scene'):
            retired = set(removed[f'{kind}_ids'])
            entry[f'linked_{kind}_ids'] = [value for value in current[f'linked_{kind}_ids'] if value not in retired]
            # The unchanged document still mentions historical sources. Exclude
            # them explicitly so Markdown inference cannot restore membership.
            entry[f'excluded_{kind}_ids'] = list(dict.fromkeys([
                *entry.get(f'excluded_{kind}_ids', []), *removed[f'{kind}_ids']]))
        for event_id in removed['event_ids']:
            for table in ('fact_event_arc_links', 'event_arc_links'):
                rolls.store.conn.execute(f'DELETE FROM {table} WHERE arc_key=? AND event_id=?',
                                         (entry.get('arc_key') or '', event_id))
        previous.pop('history', None)
        entry['history'] = [*entry.get('history', []), previous]
        entry['revision'] += 1
        entry['source_file'] = f"sqlite:{key}/revision-{entry['revision']:04d}"
        entry['material_cleanup'] = {'at': now(), 'removed': removed, 'body_unchanged': True}
        rolls._persist(entry, current['full_document'])
        # A membership-only CAS revision must not recreate an already handled
        # freshness hint. Preserve its status, timestamps and review decision.
        for hint in raw['items']:
            if (hint.get('narrative_id') == key
                    and hint.get('source_type') == 'material_freshness'
                    and hint.get('baseline_revision') == current['revision']
                    and hint.get('baseline_document_sha256') == current['document_sha256']):
                hint['previous_proposal_ids'] = [*hint.get('previous_proposal_ids', []), hint['proposal_id']]
                hint['baseline_revision'] = entry['revision']
                hint['proposal_id'] = inbox._proposal_id(key, 'material_freshness',
                    str(entry['revision']), current['document_sha256'])
                hints_changed = True
        change['revision'] = entry['revision']
    if hints_changed:
        inbox._save(raw)
    return changes
