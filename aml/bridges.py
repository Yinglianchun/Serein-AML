"""One bounded lookup round over literal, current entity evidence.

These rules propose retrieval candidates, never facts, aliases or stored edges.
The final answer remains the evaluator's job. Unsupported questions decline.
"""
from __future__ import annotations

import re

from serein.core.store import digest
from serein.tagging_entities import current_entities, entity_key, name_position, snapshot, validate


MAX_SEEDS = 5
MAX_PLANS = 3
# Every template describes a missing relationship, not an answer or a place.
RELATIONS = (
    ('factory', r'factory|manufactur|\b(?:made|built|produced)\b|工厂|制造|生产',
     'factory manufacturing origin', '工厂 制造 生产',
     r'factory|manufactur|\b(?:made|built|produced)\b|工厂|制造|生产'),
    ('location', r'\bwhere\b|location|address|\bcit(?:y|ies)\b|based|headquarter|哪(?:里|儿|座|个城市)|在哪|地点|所在地|地址|搬到',
     'location city address moved based', '所在地 城市 地址 搬迁',
     r'\b(?:located|based|moved|relocated|headquarter\w*|address|location)\b|所在地|地址|位于|坐落|搬|迁至|设在'),
    ('affiliation', r'\b(?:who|which|what)\b.*(?:employ|company|organization|studio|work for)|哪.*(?:公司|工作室|组织)|雇主|为谁工作',
     'employer company organization joined', '雇主 公司 组织 加入',
     r'employ|\b(?:joined|works? for|working for|hired)\b|加入|就职|任职|雇主'),
    ('date', r'\bwhen\b|what (?:date|year)|何时|什么时候|哪一年|几月|日期',
     'date year time happened', '日期 年 时间 发生',
     r'\b(?:19|20)\d{2}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b|\d{1,4}[年/月日-]'),
)
_EN_NAMES = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*(?:[ \t]+(?:[A-Z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*|of|the)){0,3}\b")
_ZH_NAMES = re.compile(r'([\u4e00-\u9fffA-Za-z0-9·]{2,30}(?:工作室|实验室|公司|大学|学院|书店|医院|基金会|集团|事务所|中心|项目))')
_GENERIC = {'the', 'this', 'that', 'today', 'yesterday', 'now', 'currently', 'later', 'factory',
            'location', 'city', 'address', 'company', 'studio', 'organization', 'user', 'assistant'}


def text(hit):
    return hit['content'] if hit['kind'] == 'original' else hit['document']['body_md']


def _rule_names(value):
    names = [re.sub(r'^(?:The|This|That|My|Our)\s+', '', match.group().strip())
             for match in _EN_NAMES.finditer(value)]
    for match in _ZH_NAMES.finditer(value):
        # Keep the literal name after an explicit introduction, not the subject/verb.
        name = re.split(r'加入了?|就职于|任职于|入职了?|名为|叫做?|在|的', match[1])[-1]
        if len(name) >= 2:
            names.append(name)
    names.extend(re.findall(r'[《「“"]([^》」”"\n]{2,80})[》」”"]', value))
    return [name for name in dict.fromkeys(names)
            if 2 <= len(name) <= 80 and entity_key(name) not in _GENERIC]


def grounded_entities(reader, hit):
    """Recompute grounding from canonical material; no metadata or alias trust."""
    if hit['kind'] == 'original':
        materials = [{'source_id': hit['id'], 'text': hit['content'],
                      'source_hash': digest(hit['content']), 'kind': 'raw_original'}]
        prior = []
        stamp = digest(hit['content'])
        body = hit['content']
    else:
        obj = reader.read(hit['id'], kind=hit['kind'], with_evidence=False)
        if not obj['readable'] or obj['document']['revision'] != hit['document']['revision']:
            return [], None
        doc = obj['document']
        if doc['kind'] not in {'event', 'scene'}:
            return [], None
        materials, stamp = snapshot(reader.store, doc)
        prior = current_entities(reader.store, doc)
        body = doc['body_md']
    raw = [{'name': item['name'], 'type': item['type'],
            'supports': item.get('supports', [])} for item in prior
           if isinstance(item, dict) and isinstance(item.get('name'), str) and 'type' in item]
    for material in materials:
        # Bounded exact sentences also limit quotation/provenance size.
        for sentence in re.findall(r'[^\n。！？.!?]+[。！？.!?]?', material['text'][:12000]):
            sentence = sentence.strip()
            if not sentence or len(sentence) > 1000:
                continue
            for name in _rule_names(sentence):
                # A source may contain unrelated dialogue omitted from this Event.
                if not name_position(name, body):
                    continue
                raw.append({'name': name, 'type': 'other',
                            'supports': [{'source_id': material['source_id'], 'quote': sentence}]})
                if len(raw) >= 40:
                    break
            if len(raw) >= 40:
                break
        if len(raw) >= 40:
            break
    entities, _ = validate(raw, materials)
    # AML returns the canonical body. A source-only name would leave its half
    # of the delivered chain unsupported even though the extraction is valid.
    return [item for item in entities if name_position(item['name'], body)], stamp


def plans(reader, query, seed_hits):
    relation = next((row for row in RELATIONS if re.search(row[1], query, re.I)), None)
    if relation is None:
        return []
    chinese = bool(re.search(r'[\u4e00-\u9fff]', query))
    terms = relation[3] if chinese else relation[2]
    timing = re.findall(r'\b(?:now|current|currently|latest|before|after|in \d{4})\b|现在|目前|最新|之前|之后|\d{4}年', query, re.I)
    result, seen = [], set()
    for rank, hit in enumerate(seed_hits[:MAX_SEEDS]):
        entities, stamp = grounded_entities(reader, hit)
        for entity in entities:
            name = entity['name']
            # Query-side names are the starting subject; new literal names are bridges.
            if name_position(name.casefold(), query.casefold()) or entity_key(name) in seen:
                continue
            anchor_quote = next((sentence.strip() for sentence in re.findall(
                r'[^\n。！？.!?]+[。！？.!?]?', text(hit)) if name_position(name, sentence)), name)
            if relation[0] == 'location' and re.search(r'\bwork\w*\b|employ|工作|上班', query, re.I):
                # Mentioning a studio in a hobby or aside does not make it an employer.
                if not re.search(r'\b(?:join\w*|work\w*|employ\w*|hired)\b|加入|入职|就职|任职|供职|工作', anchor_quote, re.I):
                    continue
            seen.add(entity_key(name))
            result.append({'anchor': hit['id'], 'anchor_rank': rank, 'entity': name,
                           'entity_supports': entity['supports'], 'anchor_stamp': stamp,
                           'anchor_quote': anchor_quote,
                           'relation': relation[0], 'target_pattern': relation[4],
                           'time_conditions': timing,
                           'query': ' '.join([name, terms, *timing]),
                           'lexical_queries': [name + ' ' + terms.split()[0], name]})
            if len(result) >= MAX_PLANS:
                return result
    return result


def matching_quote(plan, hit):
    """A conservative shared-name + requested-predicate test, not entailment."""
    for sentence in re.findall(r'[^\n。！？.!?]+[。！？.!?]?', text(hit)):
        sentence = sentence.strip()
        if name_position(plan['entity'].casefold(), sentence.casefold()) and re.search(plan['target_pattern'], sentence, re.I):
            if len(sentence) <= 1000:
                return sentence
    return None


def select(records, ordered, groups, limit):
    """Reserve complete validated groups within the cap, then fill ordinary ranks."""
    chosen = []
    if limit >= 2:
        for group in sorted(groups, key=lambda row: row['route'] != 'arc_menu'):
            ids = list(dict.fromkeys(group['ids']))
            if not all(key in records for key in ids):
                continue
            missing = [key for key in ids if key not in chosen]
            if len(chosen) + len(missing) <= limit:
                chosen.extend(missing)
    for key in ordered:
        if len(chosen) >= limit:
            break
        if key in records and key not in chosen:
            chosen.append(key)
    return [records[key] for key in chosen]


def excerpt(content, allowance, focus=()):
    """Keep literal bridge evidence when a long record needs an excerpt."""
    content = content.lstrip()
    if len(content) <= allowance:
        return content
    quotes = [(content.find(quote), quote) for quote in focus if quote and quote in content]
    if not quotes:
        return content[:allowance]
    position, quote = quotes[0]
    start = max(0, position - max(0, allowance - len(quote)) // 4)
    start = min(start, len(content) - allowance)
    return content[start:start + allowance]
