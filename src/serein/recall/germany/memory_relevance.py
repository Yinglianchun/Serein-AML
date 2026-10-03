"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
import jieba
from .identity import identity_names
from .query_terms import RECALL_SYSTEM_META_TERMS, WEAK_AUTOMATION_TOPIC_TERMS
DEFAULT_FACET_ALIASES = {'relationship_identity': ('human-ai relationship', 'ai relationship', 'ai companion', 'digital companion', 'relationship identity', 'companion identity', '人机恋', '人机关系'), 'intimacy': ('intimacy',), 'embodiment': ('embodiment', 'embodied', 'physical body', 'physical form', 'robot body', 'avatar body', '具身', '身体', '形体'), 'hardware_protocol': ('hardware', 'protocol', 'bluetooth', 'ble', '硬件', '协议', '蓝牙'), 'communication_action': ('email', 'e-mail', 'mail', 'gmail', 'send email', 'send mail', 'message', 'reply', 'notify', 'notification', 'dm', 'sms', '发邮件', '发信', '邮件', '邮箱', '回邮件', '回复邮件', '发消息', '私信', '短信', '通知', '联系'), 'career': ('career', 'job search', 'job hunting', 'interview', 'resume', 'cv', 'offer', 'recruiter', 'hr', 'internship', 'layoff', 'resign', 'resignation', '求职', '找工作', '找实习', '面试', '简历', '投递', '岗位', '招聘', '实习', '入职', '离职', '被裁', '裁员', '薪资', '工资', 'offer', 'hr'), 'old_or_resolved': ('old version', 'legacy', 'deprecated', 'resolved', 'obsolete', 'superseded', 'conflict', 'blocked', '旧版', '旧方案', '以前', '之前', '已解决', '已合并', '已经合并', '已废弃', '废弃', '过时', '不再使用', '不应该继续', '冲突', '阻断')}
DEFAULT_SECTION_HINTS: dict[str, tuple[str, ...]] = {}
DEFAULT_CONTEXT_TERMS = ('user', 'assistant', '用户', '对方', '我', '你', '她', '他', 'ta')
QUERY_DUPLICATED_CJK_FOLD_DENY_CHARS = frozenset('爸妈爷奶姥哥姐弟妹宝娃人天年')
STOPWORDS_DIR = Path(__file__).resolve().parent / 'resources' / 'stopwords'
QUERY_BASE_STOPWORDS_FILE = STOPWORDS_DIR / 'query_base_stopwords.txt'
QUERY_KEEPWORDS_FILE = STOPWORDS_DIR / 'query_keepwords.txt'
QUERY_EXTRA_STOPWORDS_FILE = STOPWORDS_DIR / 'query_extra_stopwords.txt'

def _load_stopword_file(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip() and (not line.lstrip().startswith('#'))}
QUERY_BASE_STOPWORDS = frozenset(_load_stopword_file(QUERY_BASE_STOPWORDS_FILE))
QUERY_KEEPWORDS = frozenset(_load_stopword_file(QUERY_KEEPWORDS_FILE))
QUERY_EXTRA_STOPWORDS = frozenset(_load_stopword_file(QUERY_EXTRA_STOPWORDS_FILE))
QUERY_BASE_TOKEN_STOPWORDS = frozenset((term for term in QUERY_BASE_STOPWORDS if not re.fullmatch('[\\u4e00-\\u9fff]', term)))
QUERY_WATER_STOPWORDS = frozenset((QUERY_BASE_TOKEN_STOPWORDS | QUERY_EXTRA_STOPWORDS | RECALL_SYSTEM_META_TERMS) - QUERY_KEEPWORDS)
QUERY_TERM_STOPWORDS = QUERY_WATER_STOPWORDS
_CONTENT_TERMS_CACHE: dict[tuple[int, str], tuple[str, ...]] = {}
_CONTENT_TERMS_CACHE_MAX = 512
PROTECTED_PHRASE_MAX_CHARS = 48
PROTECTED_PHRASE_PAIRS = (('“', '”'), ('"', '"'), ('「', '」'), ('『', '』'), ('《', '》'), ('（', '）'), ('(', ')'), ('【', '】'), ('[', ']'), ('‘', '’'), ('`', '`'))
ASSOCIATIVE_PROMPT_MARKERS = ('想到', '想起', '联想', '记得', '说', '提到', '问到', '讲到')
ASSOCIATIVE_PROMPT_VAGUE_FOCUS = {'你', '你会', '我', '我们', '这个', '那个', '什么'}
ASSOCIATIVE_PROMPT_EMPTY_QUERY = {'会想到什么', '你会想到什么', '想到什么', '会想起什么', '你会想起什么', '想起什么', '联想到什么', '你会联想到什么'}

@dataclass(frozen=True)
class MemoryRelevanceOptions:
    aliases: dict[str, tuple[str, ...]] = field(default_factory=lambda: {facet: tuple(values) for facet, values in DEFAULT_FACET_ALIASES.items()})
    blocked_facets: frozenset[str] = frozenset()
    section_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    context_terms: tuple[str, ...] = DEFAULT_CONTEXT_TERMS
    user_terms: tuple[str, ...] = ()

def memory_relevance_options_from_config(config: dict | None=None) -> MemoryRelevanceOptions:
    aliases = {facet: list(values) for facet, values in DEFAULT_FACET_ALIASES.items()}
    section_hints = {key: list(values) for key, values in DEFAULT_SECTION_HINTS.items()}
    context_terms = list(DEFAULT_CONTEXT_TERMS)
    user_terms: list[str] = []
    blocked: set[str] = set()
    identity_values = identity_names(config if isinstance(config, dict) else None)
    context_terms.extend([identity_values.get('ai_name'), identity_values.get('user_name'), identity_values.get('user_display_name'), *(identity_values.get('user_aliases') or [])])
    user_terms.extend([identity_values.get('user_name'), identity_values.get('user_display_name'), *(identity_values.get('user_aliases') or [])])
    identity = (config or {}).get('identity', {}) if isinstance(config, dict) else {}
    if isinstance(identity, dict):
        for key in ('ai_name', 'user_name', 'user_display_name'):
            context_terms.extend(_list_text(identity.get(key)))
        context_terms.extend(_list_text(identity.get('user_aliases')))
    for cfg in _relevance_config_sections(config):
        _merge_alias_config(aliases, cfg.get('aliases'))
        _merge_facet_defs(aliases, cfg.get('facets'))
        _merge_section_hints(section_hints, cfg.get('section_hints'))
        blocked.update(_list_text(cfg.get('blocked_facets')))
        blocked.update(_list_text(cfg.get('disabled_facets')))
        context_terms.extend(_list_text(cfg.get('context_terms')))
    blocked = {str(facet).strip() for facet in blocked if str(facet).strip()}
    normalized_aliases = {}
    for facet, values in aliases.items():
        facet = str(facet).strip()
        if not facet or facet in blocked:
            continue
        normalized_aliases[facet] = tuple(_unique((_normalize_alias(value) for value in values)))
    normalized_hints = {}
    for section, facets in section_hints.items():
        section = _normalize_section(section)
        if not section:
            continue
        kept = [facet for facet in _list_text(facets) if facet not in blocked]
        if kept:
            normalized_hints[section] = tuple(_unique(kept))
    return MemoryRelevanceOptions(aliases=normalized_aliases, blocked_facets=frozenset(blocked), section_hints=normalized_hints, context_terms=tuple(_unique((_normalize_alias(term) for term in context_terms))), user_terms=tuple(_unique((_normalize_alias(term) for term in user_terms))))

def content_terms_for_query(query: str, options: MemoryRelevanceOptions | None=None) -> list[str]:
    options = options or memory_relevance_options_from_config()
    raw_query = str(query or '')
    cache_key = (id(options), raw_query)
    cached = _CONTENT_TERMS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    # Filter full weak phrases before tokenization, so "影分身" cannot leak
    # back into keyword retrieval as "分身". Model inputs remain untouched.
    for term in WEAK_AUTOMATION_TOPIC_TERMS:
        raw_query = raw_query.replace(term, ' ')
    query = raw_query
    protected_phrases = extract_protected_phrases(raw_query)
    unprotected_query = strip_protected_phrases(raw_query) if protected_phrases else raw_query
    focus = recall_focus_query(query, options)
    topic = recall_topic_query(unprotected_query, options)
    terms: list[str] = []
    terms.extend(protected_phrases)
    if topic:
        terms.extend(_query_terms(topic, allow_single_cjk=True))
    if focus and focus != topic and (_normalize_alias(focus) not in {_normalize_alias(phrase) for phrase in protected_phrases}):
        for term in _query_terms(focus):
            stripped = _strip_query_water_terms(term, options, include_context=False)
            if stripped != re.sub('[^0-9a-z\\u4e00-\\u9fff_.:-]+', '', _normalize_alias(term)):
                continue
            terms.append(term)
    content_terms = [term for term in terms if not _is_context_term(term, options.context_terms)]
    result = _unique(content_terms or terms)
    if len(_CONTENT_TERMS_CACHE) >= _CONTENT_TERMS_CACHE_MAX:
        _CONTENT_TERMS_CACHE.clear()
    _CONTENT_TERMS_CACHE[cache_key] = tuple(result)
    return result

def extract_protected_phrases(query: str, *, max_chars: int=PROTECTED_PHRASE_MAX_CHARS) -> list[str]:
    text = str(query or '')
    if not text:
        return []
    phrases: list[tuple[int, str]] = []
    seen = set()
    for opener, closer in PROTECTED_PHRASE_PAIRS:
        pattern = re.compile(re.escape(opener) + f'\\s*(.{{1,{max_chars}}}?)\\s*' + re.escape(closer), flags=re.DOTALL)
        for match in pattern.finditer(text):
            value = _clean_protected_phrase(match.group(1), max_chars=max_chars)
            key = _normalize_alias(value)
            if not value or not key or key in seen:
                continue
            seen.add(key)
            phrases.append((match.start(), value))
    phrases.sort(key=lambda item: item[0])
    return [value for _start, value in phrases]

def strip_protected_phrases(query: str, replacement: str=' ') -> str:
    text = str(query or '')
    if not text:
        return ''
    for opener, closer in PROTECTED_PHRASE_PAIRS:
        pattern = re.compile(re.escape(opener) + f'\\s*.{{1,{PROTECTED_PHRASE_MAX_CHARS}}}?\\s*' + re.escape(closer), flags=re.DOTALL)
        text = pattern.sub(replacement, text)
    return text

def recall_focus_query(query: str, options: MemoryRelevanceOptions | None=None) -> str:
    """Return the concrete anchor inside prompts like: 如果我说“X”，你会想到什么."""
    options = options or memory_relevance_options_from_config()
    text = str(query or '').strip()
    if not text:
        return ''
    compact = re.sub('[\\s，。！？、,.!?:：;；~～（）()\\[\\]【】「」『』“”\\"\'`]+', '', text)
    if compact in ASSOCIATIVE_PROMPT_EMPTY_QUERY:
        return ''
    if not any((marker in text for marker in ASSOCIATIVE_PROMPT_MARKERS)):
        return text
    quoted = re.search('[“\\"\'「『]([^”\\"\'」』]{1,32})[”\\"\'」』]', text)
    if quoted:
        focus = _clean_recall_focus(quoted.group(1))
        if focus:
            return focus
    memory_match = re.search('(?:记得|记不记得|还记得)(?P<focus>.+?)(?:的)?(?:那次|这次|时候|事情|事)(?:吗|么|嘛)?', text)
    if memory_match:
        focus = _clean_recall_focus(memory_match.group('focus'))
        if focus:
            return _role_focus_term(focus) or focus
    speaker = _recall_speaker_pattern(options)
    patterns = (f'^(?:如果|假如|要是)?\\s*(?:{speaker})?\\s*(?:说|提到|问到|讲到)\\s*(?P<focus>.+?)(?:[，,。？?\\s]*(?:你)?(?:会)?(?:想到|想起|联想到|联想|记得).*)?$', '^(?P<focus>.+?)(?:[，,。？?\\s]*(?:你)?(?:会)?(?:想到|想起|联想到|联想).*)$')
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        focus = _clean_recall_focus(match.group('focus'))
        if focus:
            return focus
    return text

def recall_topic_query(query: str, options: MemoryRelevanceOptions | None=None) -> str:
    """Return query text with recall shell/filler words removed."""
    options = options or memory_relevance_options_from_config()
    text = str(query or '').strip()
    if not text:
        return ''
    focus = recall_focus_query(text, options)
    focus_topic = _strip_query_water_terms(focus, options)
    raw_topic = _strip_query_water_terms(text, options)
    if focus != text and focus_topic and raw_topic and (len(raw_topic) < len(focus_topic)):
        return raw_topic
    return focus_topic or raw_topic

def _relevance_config_sections(config: dict | None) -> list[dict]:
    if not isinstance(config, dict):
        return []
    sections = []
    for key in ('recall_facets', 'memory_relevance'):
        value = config.get(key)
        if isinstance(value, dict):
            sections.append(value)
    return sections

def _merge_alias_config(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for facet, values in raw.items():
        facet = str(facet).strip()
        if not facet:
            continue
        target.setdefault(facet, []).extend(_list_text(values))

def _merge_facet_defs(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for facet, definition in raw.items():
        if isinstance(definition, dict):
            values = definition.get('aliases') or definition.get('phrases') or []
        else:
            values = definition
        facet = str(facet).strip()
        if facet:
            target.setdefault(facet, []).extend(_list_text(values))

def _merge_section_hints(target: dict[str, list[str]], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for section, facets in raw.items():
        section = _normalize_section(section)
        if section:
            target.setdefault(section, []).extend(_list_text(facets))

def _is_context_term(term: str, context_terms: tuple[str, ...]) -> bool:
    normalized = _normalize_alias(term)
    return bool(normalized and normalized in set(context_terms or ()))

def _clean_recall_focus(value: Any) -> str:
    focus = re.sub('\\s+', '', str(value or '').strip())
    focus = re.sub('^[，。！？、,.!?:：;；~～（）()\\[\\]【】「」『』“”\\"\'`]+', '', focus)
    focus = re.sub('[，。！？、,.!?:：;；~～（）()\\[\\]【】「」『』“”\\"\'`]+$', '', focus)
    if not focus:
        return ''
    if len(focus) > 32:
        return ''
    normalized = _normalize_alias(focus)
    if normalized in ASSOCIATIVE_PROMPT_VAGUE_FOCUS:
        return ''
    if normalized.endswith('会') and len(normalized) <= 3:
        return ''
    if any((marker in normalized for marker in ('想到什么', '想起什么', '联想到什么', '联想什么'))):
        return ''
    return focus

def _clean_protected_phrase(value: Any, *, max_chars: int=PROTECTED_PHRASE_MAX_CHARS) -> str:
    phrase = re.sub('\\s+', ' ', str(value or '').strip())
    phrase = re.sub('^[，。！？、,.!?:：;；~～（）()\\[\\]【】「」『』“”\\"\'`]+', '', phrase)
    phrase = re.sub('[，。！？、,.!?:：;；~～（）()\\[\\]【】「」『』“”\\"\'`]+$', '', phrase)
    if not phrase or '\n' in phrase:
        return ''
    if len(phrase) > max_chars:
        return ''
    normalized = _normalize_alias(phrase)
    if normalized in ASSOCIATIVE_PROMPT_VAGUE_FOCUS:
        return ''
    return phrase

def _role_focus_term(text: str) -> str:
    for pattern in ('(?:当|做|成为|变成|扮成)(?P<focus>[\\u4e00-\\u9fffA-Za-z0-9_.:-]{2,16})',):
        match = re.search(pattern, str(text or ''))
        if match:
            return _clean_recall_focus(match.group('focus'))
    return ''

def _recall_speaker_pattern(options: MemoryRelevanceOptions) -> str:
    speakers = ['我']
    speakers.extend((str(term or '').strip() for term in options.user_terms or ()))
    kept = []
    seen = set()
    for speaker in speakers:
        if not speaker:
            continue
        key = _normalize_alias(speaker)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(re.escape(speaker))
    return '|'.join(kept) or re.escape('我')

def _query_terms(query: str, *, allow_single_cjk: bool=False) -> list[str]:
    raw = str(query or '').strip()
    protected_terms = extract_protected_phrases(raw)
    scan_raw = strip_protected_phrases(raw) if protected_terms else raw
    protected_keys = {_normalize_alias(term) for term in protected_terms if _normalize_alias(term)}
    raw_terms = list(protected_terms)
    raw_terms.extend((part for part in re.split('[\\s,，。！？!?;；:：/\\\\|]+', scan_raw) if part))
    raw_terms.extend(jieba.lcut(scan_raw, cut_all=False))
    raw_terms.extend(re.findall('[A-Za-z0-9_\\-]+|[\\u4e00-\\u9fff]{2,}', scan_raw))
    terms = []
    for part in raw_terms:
        normalized = _normalize_alias(part)
        if normalized and normalized in protected_keys:
            terms.append(part)
        else:
            terms.extend(_query_term_variants(part))
    kept = []
    seen = set()
    for term in terms:
        normalized = _normalize_alias(term)
        if not normalized or normalized in seen:
            continue
        compact = re.sub('[^0-9a-z\\u4e00-\\u9fff_.:-]+', '', normalized)
        if not compact or compact in QUERY_TERM_STOPWORDS:
            continue
        if not re.search('[0-9a-z\\u4e00-\\u9fff]', compact):
            continue
        if re.fullmatch('[a-z0-9_\\-]+', normalized) and len(normalized) < 3:
            continue
        if re.fullmatch('[\\u4e00-\\u9fff]+', normalized) and len(normalized) < 2 and (not allow_single_cjk):
            continue
        seen.add(normalized)
        kept.append(term)
    return kept

def _query_term_variants(value: str) -> list[str]:
    text = str(value or '').strip()
    if not text:
        return []
    variants = [text]
    normalized = _normalize_alias(text)
    if normalized not in QUERY_WATER_STOPWORDS:
        compact = re.sub('[^0-9a-z\\u4e00-\\u9fff_.:-]+', '', normalized)
        if re.fullmatch('([\\u4e00-\\u9fff])\\1', compact) and compact[0] not in QUERY_DUPLICATED_CJK_FOLD_DENY_CHARS:
            variants.append(compact[0])
    stripped = re.sub('(?:期望|希望|想要|需要|应该|不应该)?(?:召回|命中|查到|查一下|找一下|搜到|回忆|记忆)(?:的)?(?:是|到)?', ' ', text)
    stripped = re.sub('^(?:的是|是|到)', '', stripped).strip()
    for part in re.split('(?:以及|还有|或者|和|与|及|跟|同|、|\\+)+', stripped):
        part = re.sub('^[\\s，。！？、,.!?:：;；~～♡❤♥（）()\\[\\]【】「」『』“”\\"\'`-]+|[\\s，。！？、,.!?:：;；~～♡❤♥（）()\\[\\]【】「」『』“”\\"\'`-]+$', '', part.strip())
        if part:
            variants.append(part)
    return variants

def _strip_query_water_terms(query: str, options: MemoryRelevanceOptions | None=None, *, include_context: bool=True) -> str:
    text = str(query or '').strip()
    if not text:
        return ''
    options = options or memory_relevance_options_from_config()
    stop_terms = set(QUERY_TERM_STOPWORDS)
    keep_terms = set(QUERY_KEEPWORDS)
    if include_context:
        stop_terms.update((str(term or '').strip().lower() for term in options.context_terms or ()))
    protected_cjk_stop_terms = {char for term in keep_terms if re.fullmatch('[\\u4e00-\\u9fff]+', term) for char in term}
    stop_terms.difference_update(protected_cjk_stop_terms)
    fragment_stop_terms = set(QUERY_EXTRA_STOPWORDS)
    fragment_stop_terms.difference_update(protected_cjk_stop_terms)
    tokens: list[str] = []
    removed_water = False
    scan_text = text
    phrase_stop_terms = [term for term in QUERY_EXTRA_STOPWORDS if len(term) > 1 and re.fullmatch('[\\u4e00-\\u9fff]+', term)]
    for term in sorted(phrase_stop_terms, key=len, reverse=True):
        if term in scan_text:
            scan_text = scan_text.replace(term, ' ')
            removed_water = True
    for part in jieba.lcut(scan_text, cut_all=False):
        for token in re.findall('[A-Za-z]+[A-Za-z0-9_.:-]*|\\d+(?:\\.\\d+)+|[\\u4e00-\\u9fff]+', str(part or '')):
            raw_token = str(token or '').strip()
            normalized = _normalize_alias(raw_token)
            if not normalized:
                continue
            compact_key = re.sub('[^0-9a-z\\u4e00-\\u9fff_.:-]+', '', normalized)
            compact_value = re.sub('[^0-9A-Za-z\\u4e00-\\u9fff_.:-]+', '', raw_token)
            if not compact_key:
                continue
            if compact_key in stop_terms:
                removed_water = True
                continue
            if re.fullmatch('[\\u4e00-\\u9fff]+', compact_key):
                stripped = _strip_cjk_water_fragments(compact_key, fragment_stop_terms)
                if stripped != compact_key:
                    removed_water = True
                compact_key = stripped
                compact_value = stripped
                if not compact_key or compact_key in stop_terms:
                    continue
            if re.fullmatch('[a-z0-9_.:-]+', compact_key):
                if re.fullmatch('[\\d.:-]+', compact_key):
                    continue
            tokens.append(compact_value or compact_key)
    if not removed_water:
        return text
    if not tokens:
        return ''
    if all((re.fullmatch('[\\u4e00-\\u9fff]+', token) for token in tokens)):
        return ''.join(tokens)
    return ' '.join(tokens)

def _strip_cjk_water_fragments(text: str, stop_terms: set[str]) -> str:
    cleaned = str(text or '')
    if not cleaned:
        return ''
    cjk_stop_terms = [term for term in stop_terms if term and re.fullmatch('[\\u4e00-\\u9fff]+', term)]
    for term in sorted(cjk_stop_terms, key=len, reverse=True):
        if term and term != cleaned:
            cleaned = cleaned.replace(term, '')
            if not cleaned:
                return ''
    return cleaned

def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if str(item).strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]

def _normalize_text(text: Any) -> str:
    return re.sub('\\s+', ' ', str(text or '').lower()).strip()

def _normalize_alias(value: Any) -> str:
    return _normalize_text(value)

def _normalize_section(value: Any) -> str:
    return re.sub('[\\s\\-]+', '_', str(value or '').strip().lower())

def _unique(values) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        value = str(value or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
