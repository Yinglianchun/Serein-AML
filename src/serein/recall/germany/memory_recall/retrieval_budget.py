"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

import re
from typing import Mapping
from typing import Any
BUDGET_SHALLOW = 'shallow'
BUDGET_NORMAL = 'normal'
BUDGET_SKIP = 'skip'
BUDGET_DEEP = 'deep'
BUDGET_ORDER = {BUDGET_SKIP: -1, BUDGET_SHALLOW: 0, BUDGET_NORMAL: 1, BUDGET_DEEP: 2}
PURE_CHITCHAT_ROUTE_NAMES = frozenset({'present_chitchat'})
PURE_CHITCHAT_MIN_CONFIDENCE = 0.84
PURE_CHITCHAT_MIN_MARGIN = 0.08
DEFAULT_SENTINEL_RESCUE_FLOOR = 0.55
_SURFACE_ONLY_ADDRESS_KEYS = frozenset({'助手'})
_SURFACE_ONLY_CONTACT_RE = re.compile('^(?:(?:亲亲)|(?:抱抱)|(?:贴贴)|(?:摸摸)|(?:蹭蹭))+$')
REFERENCE_MARKERS = ('昨天那个', '前天那个', '上次那个', '刚才那个', '之前那个', '提到的', '说过的', '记得那个', '那个', '这件', '那件', '这条', '那条', '这次', '那次', '上次')
CONTINUATION_MARKERS = ('还没有', '还没', '还在', '没完', '继续', '仍然', '又', '再')
RECALL_MARKERS = ('还记得', '记得', '想起', '回忆', '原话', '那天', '之前', '上次', '以前', '找出来', '翻一下', '看那段', '提到', '说过', '回望', '记忆', '后来', '当时为什么')
DEEP_RECALL_MARKERS = ('记不记得', '是否记得', '还记得', '想起来', '回忆一下', '找出来', '翻一下', '搜一下', '看那段', '原话')
_RECALL_REACTION_MARKERS = ('竟然还记得', '居然还记得', '原来还记得', '竟然记得', '居然记得')
_PRIOR_MENTION_QUESTION_RE = re.compile('(?:我|你|我们).{0,12}(?:之前|以前|上次)?.{0,12}(?:提过|说过|聊过|讲过).{0,48}[吗嘛么？?]')
_QUOTED_PHRASE_RE = re.compile('[\\"“‘\'「『《]([^\\"”’\'」』》]{2,64})[\\"”’\'」』》]')
_EXPLICIT_ID_RE = re.compile('(?<![A-Za-z0-9])(?:scene|bucket|moment)(?:[_:-])[A-Za-z0-9_.:-]+(?![A-Za-z0-9])|\\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\\b')
_PROPER_PHRASE_RE = re.compile('\\b[A-Z][A-Za-z0-9._-]{1,}(?:\\s+[A-Z][A-Za-z0-9._-]{1,})+\\b')
_MIXED_CLAUSE_RE = re.compile('[,，;；\\n]')

def _compact(value: object) -> str:
    return re.sub('[^0-9a-z\\u4e00-\\u9fff_.:-]+', '', str(value or '').strip().lower())

def _clean_list(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = ' '.join(str(value or '').split()).strip()
        key = _compact(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output

def _contains_any(text: str, markers: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for marker in sorted(markers, key=len, reverse=True):
        if marker and marker in text and (not any((marker in existing for existing in found))):
            found.append(marker)
    return found

def _explicit_deep_recall_markers(text: str) -> list[str]:
    """Return only unmistakable requests to retrieve earlier material.

    Broad temporal words such as ``之前`` and ``上次`` still veto a hard skip,
    but they do not justify deep retrieval by themselves.
    """
    found = _contains_any(text, DEEP_RECALL_MARKERS)
    if any((marker in text for marker in _RECALL_REACTION_MARKERS)):
        found = [marker for marker in found if marker not in {'还记得'}]
    if _PRIOR_MENTION_QUESTION_RE.search(text):
        found.append('prior_mention_question')
    return list(dict.fromkeys(found))

def _append_facet(facets: list[dict[str, Any]], *, kind: str, value: str, strength: str, source: str) -> None:
    value = ' '.join(str(value or '').split()).strip()
    if not value:
        return
    key = (kind, _compact(value))
    if any(((item.get('kind'), _compact(item.get('value'))) == key for item in facets)):
        return
    facets.append({'kind': kind, 'value': value, 'strength': strength, 'source': source})

def _term_in_residue(term: str, residue: str) -> bool:
    term_key = _compact(term)
    residue_key = _compact(residue)
    return bool(term_key and residue_key and (term_key in residue_key))

def _bounded_float(value: object, default: float=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))

def surface_only_query_kind(query: object) -> str:
    """Identify a bare address or affectionate gesture with no recall payload."""
    key = re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', str(query or '').strip().lower())
    if key in _SURFACE_ONLY_ADDRESS_KEYS:
        return 'address_only'
    if key and _SURFACE_ONLY_CONTACT_RE.fullmatch(key):
        return 'intimate_contact_only'
    for address in sorted(_SURFACE_ONLY_ADDRESS_KEYS, key=len, reverse=True):
        if key.startswith(address) and _SURFACE_ONLY_CONTACT_RE.fullmatch(key[len(address):]):
            return 'address_and_intimate_contact_only'
    return ''

def _budget_channels(budget: str) -> list[str]:
    if budget == BUDGET_SKIP:
        return []
    if budget == BUDGET_SHALLOW:
        return ['exact_anchor', 'lexical', 'authored_cue', 'body_semantic']
    if budget == BUDGET_NORMAL:
        return ['exact_anchor', 'lexical', 'authored_cue', 'body_semantic', 'cue_semantic']
    return ['exact_anchor', 'lexical', 'authored_cue', 'body_semantic', 'cue_semantic', 'cue_expansion', 'evidence', 'relations']

def _budget_top_k(budget: str) -> int:
    return {BUDGET_SKIP: 0, BUDGET_SHALLOW: 3, BUDGET_NORMAL: 8, BUDGET_DEEP: 10}.get(budget, 8)

def build_retrieval_budget(query: str, *, route: str='', route_action: str='recall', semantic_debug: Mapping[str, Any] | None=None, planner: Mapping[str, Any] | None=None, anchor_plan: Mapping[str, Any] | None=None, date_hint: Mapping[str, Any] | bool | None=None, absolute_floor: float=0.55, reranker_entry_floor: float=0.5, rescue_floor: float=DEFAULT_SENTINEL_RESCUE_FLOOR) -> dict[str, Any]:
    """Return a simulation/shadow budget without making an injection decision.

    A pure-chitchat decision is intentionally only a high-confidence prior at
    this stage.  ``finalize_retrieval_budget`` must see the sentinel result
    before this budget can become ``skip``.
    """
    text = ' '.join(str(query or '').split()).strip()
    surface_only_kind = surface_only_query_kind(text)
    semantic_debug = semantic_debug if isinstance(semantic_debug, Mapping) else {}
    planner = planner if isinstance(planner, Mapping) else {}
    anchor_plan = anchor_plan if isinstance(anchor_plan, Mapping) else {}
    route_name = str(route or '').strip()
    route_action = str(route_action or 'recall').strip().lower() or 'recall'
    if not route_name:
        route_name = str(semantic_debug.get('route') or '').strip()
    if route_action == 'recall':
        route_action = str(semantic_debug.get('route_action') or route_action).strip().lower() or 'recall'
    date_present = bool(date_hint) if isinstance(date_hint, bool) else bool(date_hint)
    date_value = 'date'
    if isinstance(date_hint, Mapping):
        date_value = str(date_hint.get('date') or date_hint.get('reference') or 'date').strip() or 'date'
    facets: list[dict[str, Any]] = []
    route_is_surface_candidate = route_action == 'skip' and route_name in PURE_CHITCHAT_ROUTE_NAMES
    if route_is_surface_candidate:
        _append_facet(facets, kind='surface_route', value=route_name, strength='prior', source='semantic_prototype_route')
    if date_present:
        _append_facet(facets, kind='date', value=date_value, strength='neutral', source='date_hint')
    residue_terms = _clean_list(planner.get('locatable_terms'))
    specific_terms = _clean_list(planner.get('specific_terms'))
    text_key = _compact(text)
    concrete_terms: list[str] = []
    for term in residue_terms:
        term_key = _compact(term)
        if not term_key or not text_key or term_key not in text_key:
            continue
        if term_key == text_key and len(term_key) <= 3 and (len(specific_terms) <= 1):
            continue
        concrete_terms.append(term)
    for term in concrete_terms:
        _append_facet(facets, kind='entity', value=term, strength='strong', source='query_planner.locatable_terms')
    quoted_phrases = [match.group(1).strip() for match in _QUOTED_PHRASE_RE.finditer(text) if _term_in_residue(match.group(1), text)]
    proper_phrases = [match.group(0).strip() for match in _PROPER_PHRASE_RE.finditer(text) if _term_in_residue(match.group(0), text)]
    explicit_ids = [match.group(0).strip() for match in _EXPLICIT_ID_RE.finditer(text)]
    title_terms = _clean_list(anchor_plan.get('title_anchor_terms'))
    exact_terms = _clean_list(anchor_plan.get('exact_anchor_terms'))
    for phrase in [*quoted_phrases, *proper_phrases]:
        _append_facet(facets, kind='protected_phrase', value=phrase, strength='strong', source='query_phrase')
    for value in [*explicit_ids, *title_terms, *exact_terms]:
        _append_facet(facets, kind='exact_anchor', value=value, strength='strong', source='explicit_anchor')
    references = _contains_any(text, REFERENCE_MARKERS)
    continuations = _contains_any(text, CONTINUATION_MARKERS)
    for marker in references:
        _append_facet(facets, kind='reference', value=marker, strength='supporting', source='query_reference_marker')
    for marker in continuations:
        _append_facet(facets, kind='continuation', value=marker, strength='supporting', source='query_continuation_marker')
    if references and concrete_terms:
        _append_facet(facets, kind='reference_entity', value=' + '.join([references[0], concrete_terms[0]]), strength='strong', source='reference_plus_entity')
    strong_kinds = {'entity', 'protected_phrase', 'exact_anchor', 'reference_entity'}
    strong_facets = [facet for facet in facets if facet.get('kind') in strong_kinds]
    anchor_reasons: list[str] = []
    for facet in strong_facets:
        kind = str(facet.get('kind') or '')
        if kind == 'entity':
            anchor_reasons.append('rare_or_specific_entity')
        elif kind == 'protected_phrase':
            anchor_reasons.append('protected_phrase')
        elif kind == 'exact_anchor':
            anchor_reasons.append('explicit_or_title_anchor')
        elif kind == 'reference_entity':
            anchor_reasons.append('reference_plus_entity')
    anchor_reasons = list(dict.fromkeys(anchor_reasons))
    anchor_override = bool(anchor_reasons)
    date_only = date_present and (not strong_facets)
    recall_markers = _contains_any(text, RECALL_MARKERS)
    mixed_clause = bool(_MIXED_CLAUSE_RE.search(text))
    short_surface_shape = bool(text_key and len(text_key) <= 24 and (not mixed_clause))
    prototype_confidence = _bounded_float(semantic_debug.get('confidence'), 0.0)
    prototype_margin = _bounded_float(semantic_debug.get('margin'), 0.0)
    route_threshold = _bounded_float(semantic_debug.get('threshold'), 0.0)
    confidence_floor = max(PURE_CHITCHAT_MIN_CONFIDENCE, route_threshold)
    prototype_high_confidence = route_is_surface_candidate and prototype_confidence >= confidence_floor and (prototype_margin >= PURE_CHITCHAT_MIN_MARGIN)
    structural_vetoes: list[str] = []
    if mixed_clause:
        structural_vetoes.append('mixed_clause')
    if references:
        structural_vetoes.append('reference_marker')
    if continuations:
        structural_vetoes.append('continuation_marker')
    if recall_markers:
        structural_vetoes.append('explicit_recall_marker')
    if date_present and concrete_terms:
        structural_vetoes.append('date_plus_entity')
    pure_chitchat_prior = bool(prototype_high_confidence and short_surface_shape and (not structural_vetoes) and (not strong_facets))
    pure_prior_reasons = ['semantic_prototype_route', 'high_confidence', 'clean_surface_shape'] if pure_chitchat_prior else []
    route_is_shallow = route_action == 'skip' or route_name in {'simple_contact', 'present_chitchat'} or date_only
    route_budget = BUDGET_SHALLOW if route_is_shallow else BUDGET_NORMAL
    effective_budget = BUDGET_NORMAL if anchor_override or BUDGET_ORDER[route_budget] >= BUDGET_ORDER[BUDGET_NORMAL] else route_budget
    surface_route = route_name or ('date_only' if date_only else 'mixed_query')
    explicit_deep_reasons: list[str] = []
    if route_name == 'recall_needed':
        explicit_deep_reasons.append('semantic_recall_route')
    if any((facet.get('kind') in {'exact_anchor', 'reference_entity'} for facet in strong_facets)):
        explicit_deep_reasons.append('exact_or_reference_entity')
    deep_recall_markers = _explicit_deep_recall_markers(text)
    if deep_recall_markers:
        explicit_deep_reasons.append('explicit_recall_language')
    initial_budget = BUDGET_SHALLOW
    final_budget = BUDGET_DEEP if explicit_deep_reasons else BUDGET_SHALLOW
    memory_need = 'bypass' if surface_only_kind else 'required' if explicit_deep_reasons else 'optional'
    absolute_floor = _bounded_float(absolute_floor, 0.55)
    reranker_entry_floor = min(absolute_floor, _bounded_float(reranker_entry_floor, 0.5))
    rescue_floor = _bounded_float(rescue_floor, DEFAULT_SENTINEL_RESCUE_FLOOR)
    return {'mode': 'simulation_shadow', 'surface_route': surface_route, 'surface_only_kind': surface_only_kind, 'route_budget': route_budget, 'anchor_override': anchor_override, 'anchor_override_reasons': anchor_reasons, 'effective_budget': effective_budget, 'initial_budget': initial_budget, 'final_budget': final_budget, 'memory_need': memory_need, 'budget_decision_source': 'query_structure' if explicit_deep_reasons else 'default_shallow_probe', 'escalation_reason': explicit_deep_reasons[0] if explicit_deep_reasons else '', 'explicit_deep_reasons': explicit_deep_reasons, 'pure_surface_chitchat': False, 'pure_chitchat_prior': pure_chitchat_prior, 'pure_chitchat_prior_reasons': pure_prior_reasons, 'prototype_prior': {'source': 'semantic_route_clean_prototype_shadow', 'candidate': route_is_surface_candidate, 'high_confidence': prototype_high_confidence, 'confidence': round(prototype_confidence, 4), 'confidence_floor': round(confidence_floor, 4), 'margin': round(prototype_margin, 4), 'margin_floor': PURE_CHITCHAT_MIN_MARGIN, 'shape_clean': short_surface_shape, 'mixed_clause': mixed_clause, 'structural_vetoes': structural_vetoes, 'status': 'ready' if pure_chitchat_prior else 'vetoed_or_low_confidence'}, 'recall_markers': recall_markers, 'deep_recall_markers': deep_recall_markers, 'budget_order': {'skip': BUDGET_ORDER[BUDGET_SKIP], 'shallow': BUDGET_ORDER[BUDGET_SHALLOW], 'normal': BUDGET_ORDER[BUDGET_NORMAL], 'deep': BUDGET_ORDER[BUDGET_DEEP]}, 'query_facets': facets, 'surface_matches': [], 'surface_residue': text, 'date_only': date_only, 'absolute_floor': round(absolute_floor, 4), 'reranker_entry_floor': round(reranker_entry_floor, 4), 'rescue_floor': round(rescue_floor, 4), 'channels': _budget_channels(effective_budget), 'semantic_top_k': _budget_top_k(effective_budget), 'sentinel': {'called': False, 'top_k': 2, 'rescue_floor': round(rescue_floor, 4), 'candidate_count': 0, 'floor_qualified_count': 0, 'candidates': [], 'skip_allowed': False, 'reason': 'pending_pure_chitchat_prior' if pure_chitchat_prior else 'not_applicable'}, 'skip_ready': False, 'rerank': {'policy': 'after_reranker_entry_floor', 'mode': 'shadow', 'enabled': False, 'would_call': False, 'called': False, 'eligible_candidate_count': 0}, 'cheap_retrieval': {'candidate_count': 0, 'floor_qualified_count': 0, 'gray_zone_count': 0, 'reranker_eligible_count': 0, 'stop_reason': 'pending_candidates'}, 'route_skip_deferred': False}
