"""Model-suggested names, with exact, current evidence; no alias merging or graph edges."""
import re
import unicodedata

from .core.store import digest, encode

VERSION = 1
TAGGING_FIELDS = ('canonical_domain', 'domain', 'operit_tagging_status', 'operit_tagging_model',
                  'tagged_entities', 'entity_extraction_version', 'entity_input_hash',
                  'entity_rejected_count', 'entity_materials_truncated')
TYPES = {'person', 'place', 'organization', 'work', 'project', 'product', 'other'}
GENERIC = {'我', '你', '他', '她', '我们', '你们', '他们', '今天', '昨天', '关系', '生活', '情绪'}


def entity_key(name):
    return unicodedata.normalize('NFKC', name).strip().casefold()


def snapshot(store, doc):
    materials = [dict(row) for row in store.conn.execute(
        "SELECT DISTINCT s.id AS source_id,s.content AS text,s.content_sha256 AS source_hash "
        "FROM evidence_bindings b JOIN sources s ON s.id=b.source_id "
        "WHERE b.document_id=? AND b.active=1 ORDER BY s.id", (doc['id'],))]
    for item in materials:
        item['kind'] = 'bound_source'
    if not materials:
        materials = [{'source_id': 'body:' + doc['id'], 'text': doc['body_md'],
                      'source_hash': digest(doc['body_md']), 'kind': 'memory_body'}]
    stamp = 'entities-v1:' + digest(encode([doc['title'], doc['body_md'],
        [(item['source_id'], item['source_hash']) for item in materials]]))
    return materials, stamp


def prompt_materials(materials, budget=24000):
    result = []
    for item in materials:
        if budget <= 0:
            break
        text = item['text'][:budget]
        result.append({'source_id': item['source_id'], 'kind': item['kind'], 'text': text,
                       'source_hash': item['source_hash']})
        budget -= len(text)
    return result


def name_position(name, quote):
    pattern = re.escape(name)
    if name.isascii():
        pattern = r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])'
    return re.search(pattern, quote)


def validate(raw, materials):
    if not isinstance(raw, list):
        raise ValueError('实体结果必须是数组')
    sources = {item['source_id']: item for item in materials}
    found, rejected = {}, 0
    for item in raw[:40]:
        if not isinstance(item, dict):
            rejected += 1
            continue
        name = item.get('name')
        kind = item.get('type')
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 80 or not isinstance(kind, str) or kind not in TYPES:
            rejected += 1
            continue
        name = name.strip()
        if name in GENERIC or name.isdigit():
            rejected += 1
            continue
        supports = []
        raw_supports = item.get('supports')
        raw_supports = raw_supports if isinstance(raw_supports, list) else []
        for support in raw_supports[:8]:
            if not isinstance(support, dict):
                continue
            source_id = support.get('source_id')
            source = sources.get(source_id) if isinstance(source_id, str) else None
            quote = support.get('quote')
            if not source or not isinstance(quote, str) or not quote or len(quote) > 1000:
                continue
            start = source['text'].find(quote)
            match = name_position(name, quote)
            if start < 0 or not match:
                continue
            row = {'source_id': source['source_id'], 'kind': source['kind'],
                   'source_hash': source.get('source_hash', digest(source['text'])), 'quote': quote,
                   'start_offset': start, 'end_offset': start + len(quote),
                   'name_start': start + match.start(), 'name_end': start + match.end()}
            if row not in supports:
                supports.append(row)
        if not supports:
            rejected += 1
            continue
        aliases = item.get('aliases', [])
        aliases = aliases if isinstance(aliases, list) else []
        aliases = sorted({alias.strip() for alias in aliases if isinstance(alias, str)
                          and 2 <= len(alias.strip()) <= 80 and entity_key(alias) != entity_key(name)
                          and any(name_position(alias.strip(), support['quote']) for support in supports)})[:8]
        key = entity_key(name)
        entry = found.setdefault(key, {'key': key, 'name': name, 'type': kind, 'supports': [], 'alias_suggestions': []})
        for support in supports:
            if support not in entry['supports']:
                entry['supports'].append(support)
        entry['supports'] = entry['supports'][:8]
        entry['alias_suggestions'] = sorted(set(entry['alias_suggestions']) | set(aliases))[:8]
    return list(found.values())[:20], rejected + max(0, len(raw) - 40)


def current_entities(store, doc):
    """Old extraction snapshots stop applying after a body/title/binding change."""
    if doc['lifecycle'] != 'active':
        return []
    materials, stamp = snapshot(store, doc)
    meta = doc['metadata']
    if meta.get('entity_extraction_version') != VERSION or meta.get('entity_input_hash') != stamp:
        return []
    return meta.get('tagged_entities', [])
