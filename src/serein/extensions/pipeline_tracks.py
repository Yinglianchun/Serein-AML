"""Continuation state matching the verified Bridge message-level Track router."""
import json
import re
from ..core.store import digest, encode
from . import pipeline_latest as latest


def scope_for(source, session):
    return digest(encode([source, session]))[:20]


def next_ordinal(scope, cards):
    numbers = [int(match[1]) for card in cards if
               (match := re.fullmatch(r'session_' + re.escape(str(scope)) + r'_track_([0-9]+)', card['track_id']))]
    return max(numbers, default=0) + 1


def load_tracks(store, source, session, before_id, message):
    # Opaque public window IDs are ordered by first visible message, not by the
    # most recent reply to an older window. Preserve runtime/workspace boundaries.
    visible = "role IN ('user','assistant') AND TRIM(text)<>'' AND COALESCE(json_extract(metadata_json,'$.draft'),0)=0 AND COALESCE(json_extract(metadata_json,'$.discarded'),0)=0"
    windows = store.conn.execute('SELECT session_id,MIN(id) AS first_id FROM raw_events WHERE source=? AND '
                                + visible + ' GROUP BY session_id ORDER BY first_id', (source,)).fetchall()
    current = next((row for row in windows if row['session_id'] == session), None)
    def context(row):
        meta = json.loads(store.conn.execute('SELECT metadata_json FROM raw_events WHERE id=?', (row['first_id'],)).fetchone()[0])
        return meta, (meta.get('runtime', ''), meta.get('workspace_root', ''))
    previous = None
    if current:
        metadata, boundary = context(current)
        candidates = [row for row in windows if row['first_id'] < current['first_id'] and context(row)[1] == boundary]
        previous = candidates[-1]['session_id'] if candidates else None
        if metadata.get('previous_session_id') is not None:
            previous = next((row['session_id'] for row in candidates
                             if str(row['session_id']) == str(metadata['previous_session_id'])), None)
    scope = scope_for(source, session)
    previous_scope = scope_for(source, previous) if previous is not None else scope
    cards = []
    routes = store.conn.execute('SELECT r.*,p.route_json FROM pipeline_routes p JOIN raw_events r ON r.id=p.raw_id '
                                'WHERE r.source=? AND r.id<? AND r.session_id IN (?,?) ORDER BY r.id DESC',
                                (source, before_id, session, previous if previous is not None else session)).fetchall()
    for row in store.conn.execute('SELECT card_json,scope FROM pipeline_tracks WHERE scope IN (?,?) ORDER BY id', (scope, previous_scope)):
        card = json.loads(row['card_json'])
        card.setdefault('last_session_id', row['scope'])
        match = re.fullmatch(r'session_(.+)_track_[0-9]+', card['track_id'])
        card.setdefault('origin_session_id', match[1] if match else row['scope'])
        anchors = list(card.get('recent_source_message_ids') or [])
        if not anchors:
            primary = [r['id'] for r in routes if json.loads(r['route_json'])['primary_track_id'] == card['track_id']]
            context_ids = [r['id'] for r in routes if card['track_id'] in json.loads(r['route_json'])['context_track_ids']]
            anchors = (primary or context_ids)[:1]
        originals = [message(raw) for key in anchors if (raw := next((r for r in routes if r['id'] == key), None))]
        card['recent_source_message_ids'] = [m['id'] for m in originals]
        card['recent_turns'] = latest.transcript_payload(originals)
        cards.append(card)
    all_ids = [{'track_id': row[0]} for row in store.conn.execute('SELECT id FROM pipeline_tracks')]
    return cards, next_ordinal(scope, all_ids)


def parked(cards):
    return [{**card, 'status': 'parked'} if card.get('status') == 'active' else dict(card) for card in cards]


def update_cards(cards, assignments, updates, messages, scope):
    current = {card['track_id']: card for card in parked(cards)}
    by_id = {m['id']: m for m in messages}
    for update in updates:
        key = update['track_id']
        primary = [a['source_message_id'] for a in assignments if a['primary_track_id'] == key]
        context = [a['source_message_id'] for a in assignments if key in a['context_track_ids']]
        anchor = (primary or context)[-1]
        previous = current.get(key, {})
        current[key] = {**previous, **update, 'origin_session_id': previous.get('origin_session_id', scope),
                        'last_session_id': scope, 'recent_source_message_ids': [anchor],
                        'recent_turns': latest.transcript_payload([by_id[anchor]])}
    return list(current.values())


def persist(conn, cards, scope, *, preserve_newer=False):
    for card in cards:
        if preserve_newer:
            row = conn.execute('SELECT card_json FROM pipeline_tracks WHERE id=?', (card['track_id'],)).fetchone()
            if row:
                previous = [key for key in json.loads(row[0]).get('recent_source_message_ids', []) if type(key) is int]
                incoming = [key for key in card.get('recent_source_message_ids', []) if type(key) is int]
                if previous and (not incoming or max(incoming) < max(previous)):
                    continue
        # Unused parked cards still belong to the last window that referenced
        # them. Saving a different batch must not move their continuation scope.
        conn.execute('INSERT INTO pipeline_tracks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET '
                     'scope=excluded.scope,card_json=excluded.card_json',
                     (card['track_id'], card.get('last_session_id', scope), encode(card)))
