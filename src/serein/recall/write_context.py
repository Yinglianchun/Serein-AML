"""Optional, read-only hints returned after a new Scene is saved."""

import sqlite3

from ..core.reader import Reader
from .index import Search
from .scene import evidence_text


_GENERIC = {'我们', '今天', '现在', '之前', '后来', '事情', '记忆', '相关', '继续', '记录',
            '关系', '生活', '工作', '项目', 'scene', 'event', 'memory', 'arc'}


def _phrases(document):
    cues = document['metadata'].get('scene_cues') or []
    if isinstance(cues, str):
        cues = [cues]
    values = [*cues, document['title']] if isinstance(cues, list) else [document['title']]
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        phrase = ' '.join(str(value or '').split()).strip('，。！？,.!? ')
        compact = ''.join(phrase.split())
        if (phrase and compact.casefold() not in _GENERIC and len(compact) >= 3
                and phrase not in result):
            result.append(phrase)
    return result[:9]


def _exact_match(phrases, document):
    text = (document['title'] + '\n' + evidence_text(document)).casefold()
    matched = [phrase for phrase in phrases if phrase.casefold() in text]
    return max(matched, key=len) if matched else ''


def _lexical_candidates(settings, scene_id, phrases):
    found = {}
    if settings.index is None:
        return found
    try:
        with Search(settings.database, settings.index) as search:
            for phrase in phrases:
                for hit in search.search(phrase, kind='scene', mode='surface', limit=8)['items']:
                    if hit['id'] == scene_id:
                        continue
                    old = hit['object']['document']
                    match = _exact_match(phrases, old)
                    if match:
                        found[hit['id']] = (old, match)
    except (OSError, ValueError, sqlite3.Error):
        return {}
    return found


def _semantic_candidate(settings, scene_id, document):
    """Use prepared vectors and a body reranker; weak cosine alone is not a hint."""
    try:
        import httpx
        from ..adapters.embedding import EmbeddingClient
        from ..adapters.reranker import RerankerClient
        from ..configured_models import effective_settings

        selected = effective_settings(settings)
        if not selected.embedding or not selected.reranker or selected.index is None:
            return None
        text = (document['title'] + '\n' + evidence_text(document)[:1200]).strip()
        with httpx.Client(timeout=2.5, follow_redirects=False) as client:
            embedded = EmbeddingClient(settings.database, selected.index, **selected.embedding).query(text, client=client)
            with Search(settings.database, selected.index) as search:
                hits = [hit for hit in search.search(text, kind='scene', mode='surface', limit=8,
                                                     query_embedding=embedded, min_cosine=0.55)['items']
                        if hit['id'] != scene_id]
            if not hits:
                return None
            documents = [{'ref':hit['id'], 'title':hit['object']['document']['title'],
                          'body':evidence_text(hit['object']['document'])[:2400]} for hit in hits[:5]]
            scores = RerankerClient(**selected.reranker)(text, documents, client=client)
        ranked = sorted(((scores.get(hit['id'], 0), hit) for hit in hits[:5]),
                        key=lambda pair: (-pair[0], pair[1]['id']))
        if not ranked or ranked[0][0] < 0.75 or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08):
            return None
        hit = ranked[0][1]
        return hit['id'], hit['object']['document']
    except Exception:
        # Optional provider/index errors only remove the semantic hint.
        return None


def _possible_arc(reader, related_scene_id):
    rows = reader.store.conn.execute(
        "SELECT DISTINCT n.document_id FROM narrative_materials n "
        "JOIN documents d ON d.id=n.document_id AND d.revision=n.revision "
        "WHERE n.kind='scene' AND n.target_id=? AND n.disposition IN ('linked','appended') "
        "AND d.kind='narrative' AND d.lifecycle='active' ORDER BY n.document_id",
        (related_scene_id,)).fetchall()
    if len(rows) != 1:
        return None
    arc_id = rows[0]['document_id']
    arc = reader.read(arc_id, kind='narrative', with_evidence=False)
    if not arc['readable'] or arc['status'] != 'active':
        return None
    document = arc['document']
    registry = document['metadata'].get('legacy_registry') or {}
    return {'id':arc_id, 'title':document['title'],
            'hint':'可能属于 Arc：' + document['title'],
            'arc_key':registry.get('arc_key') or document['metadata'].get('arc_key') or '',
            'based_on_scene_id':related_scene_id, 'status':'possible_membership',
            'scope_only':True}


def find_write_context(settings, scene_id):
    """Return zero or one Scene handle and, through it, zero or one Arc handle."""
    with Reader(settings.database) as reader:
        current = reader.read(scene_id, kind='scene', with_evidence=False)
        if not current['readable'] or current['status'] != 'active':
            return {}
        document = current['document']
        phrases = _phrases(document)
        lexical = _lexical_candidates(settings, scene_id, phrases)
        semantic = _semantic_candidate(settings, scene_id, document) if len(lexical) != 1 else None
        if semantic:
            related_id, old = semantic
            basis = 'semantic_body_review'
        elif lexical:
            ranked = sorted(lexical.items(), key=lambda pair: (-len(pair[1][1]), pair[0]))
            if len(ranked) > 1 and len(ranked[0][1][1]) == len(ranked[1][1][1]):
                return {}
            related_id, (old, match) = ranked[0]
            basis = 'shared_authored_phrase'
        else:
            return {}
        result = {'related_scene_candidate':{'id':related_id, 'title':old['title'],
                                              'hint':'可能相关的旧 Scene：' + old['title'],
                                              'status':'candidate', 'read_only':True,
                                              'match_basis':basis}}
        arc = _possible_arc(reader, related_id)
        if arc:
            result['possible_arc'] = arc
        return result
