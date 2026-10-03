"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from .query_understanding import query_intent_rules
from .query_understanding import query_intent_terms
from .query_terms import DEFAULT_AI_ADDRESS_TERMS
from .query_terms import GENERIC_LEXICAL_STOPWORDS
from .query_terms import QUERY_PLANNER_GENERIC_TERMS
from .query_terms import identity_address_terms
from .utils import parse_human_date_reference
from .utils import strip_wikilinks
GENERIC_KEYWORD_MATCH_TERMS = frozenset({'game', 'games', '玩法', '游戏', '今天', '以前', '之前', '刚刚', '刚才', '当前', '最近', '现在', '玩', '玩过', '看到', '看到哪', '读到', '读到哪', '做到', '做到哪', '进行', '进行到哪', '进展', '追到', '追到哪', '哪', '后来', '后续', '之后', '发展', '演变', '时间线', '怎么样', '如何', '发生了什么', '有什么'})
IDENTITY_NAME_INTENT_MARKERS = query_intent_terms('identity_name.intent_markers')
IDENTITY_NAME_EVENT_MARKERS = query_intent_terms('identity_name.event_markers')
DATE_RECALL_CHAT_MARKERS = query_intent_terms('date_recall.chat_markers')

class GatewayService:

    def _query_has_identity_name_intent(self, query: str) -> bool:
        compact = self._compact_lookup_key(query)
        return bool(compact and any((marker in compact for marker in IDENTITY_NAME_INTENT_MARKERS)))

    def _query_has_name_origin_intent(self, query: str) -> bool:
        compact = self._compact_lookup_key(query)
        if not compact:
            return False
        has_name_event = any((marker in compact for marker in ('名字', '叫', '取名', '起名', '命名', '称呼')))
        has_origin_question = any((marker in compact for marker in ('为什么', '为何', '为啥', '原因', '由来', '来历', '怎么来', '从哪来', '从哪里来', '谁取', '谁起', '谁选')))
        return bool(has_name_event and has_origin_question)

    def _name_origin_search_terms(self, query: str) -> list[str]:
        text = str(query or '').strip()
        if not self._query_has_name_origin_intent(text):
            return []
        reader = getattr(getattr(self, 'recall_policy', None), 'specific_query_terms', None)
        raw_terms = list(reader(text)) if callable(reader) else []
        raw_terms.extend(re.findall('[A-Za-z][A-Za-z0-9_.-]{1,31}', text))
        noise = ('从哪里来的', '从哪来的', '名字怎么来的', '为什么', '怎么会', '怎么来', '这个名字', '那个名字', '名字的由来', '名字的来历', '为何', '为啥', '谁取', '谁起', '谁选', '取名', '起名', '命名', '称呼', '由来', '来历', '名字', '叫做', '叫', '原因', '谁')
        generic_keys = {self._compact_lookup_key(value) for value in ('我', '你', '他', '她', '它', '我们', '你们', '他们', '这个', '那个', *identity_address_terms(getattr(self, 'identity', None) or {})) if self._compact_lookup_key(value)}
        output: list[str] = []
        seen: set[str] = set()
        for raw_term in raw_terms:
            residue = self._compact_lookup_key(raw_term)
            for fragment in noise:
                fragment_key = self._compact_lookup_key(fragment)
                if fragment_key:
                    residue = residue.replace(fragment_key, '')
            residue = residue.strip('的了呢吗呀啊吧')
            if not residue or residue in generic_keys or residue in seen:
                continue
            if re.fullmatch('[a-z][a-z0-9_.-]{1,31}', residue):
                pass
            elif not re.fullmatch('[\\u4e00-\\u9fff]{2,18}', residue):
                continue
            seen.add(residue)
            output.append(residue)
        return output[:8]

    def _query_prefers_identity_name_over_date_recall(self, query: str) -> bool:
        text = str(query or '').strip()
        compact = self._compact_lookup_key(text)
        if not compact or not self._query_has_identity_name_intent(text):
            return False
        if any((marker in compact for marker in DATE_RECALL_CHAT_MARKERS)):
            return False
        return any((marker in compact for marker in IDENTITY_NAME_EVENT_MARKERS)) or bool(self._query_date_recall_hint(text))

    def _identity_name_search_terms(self, query: str) -> list[str]:
        text = str(query or '').strip()
        compact = self._compact_lookup_key(text)
        if not compact or not self._query_has_identity_name_intent(text):
            return []
        ai_name = str(self.identity.get('ai_name') or '').strip()
        user_names = [str(value or '').strip() for value in (self.identity.get('user_display_name'), self.identity.get('user_name'), *(self.identity.get('user_aliases') or [])) if str(value or '').strip()]
        ai_keys = {self._compact_lookup_key(value) for value in (ai_name, *DEFAULT_AI_ADDRESS_TERMS) if self._compact_lookup_key(value)}
        user_keys = {self._compact_lookup_key(value) for value in user_names if self._compact_lookup_key(value)}
        user_self_question = any((marker in compact for marker in query_intent_terms('identity_name.user_self_question_markers')))
        ai_target = any((key and key in compact for key in ai_keys))
        user_target = user_self_question or any((key and key in compact for key in user_keys))
        if not user_target and any((marker in compact for marker in query_intent_terms('identity_name.ai_target_markers'))):
            ai_target = True
        if user_self_question:
            ai_target = False
        effective_user_target = user_target and (not ai_target)
        has_date_hint = bool(self._query_date_recall_hint(text))
        strong_name_marker = any((marker in compact for marker in query_intent_terms('identity_name.strong_markers')))
        if not (ai_target or effective_user_target or has_date_hint or strong_name_marker):
            return []
        terms: list[str] = []
        seen: set[str] = set()

        def add(value: object) -> None:
            cleaned = str(value or '').strip()
            key = self._compact_lookup_key(cleaned)
            if not key or key in seen:
                return
            seen.add(key)
            terms.append(cleaned)
        if effective_user_target:
            for value in user_names[:2]:
                add(value)
        elif ai_target or strong_name_marker or has_date_hint:
            add(ai_name)
        for match in re.findall('(?:\\d{2,4}年)?\\d{1,2}月\\d{1,2}日|\\d{4}[./-]\\d{1,2}[./-]\\d{1,2}|\\d{1,2}[./]\\d{1,2}', text):
            add(match)
        date_hint = self._query_date_recall_hint(text)
        if date_hint and date_hint.get('date'):
            add(date_hint.get('date'))
        if has_date_hint and self._query_prefers_identity_name_over_date_recall(text):
            for term in query_intent_terms('identity_name.date_search_terms'):
                add(term)
        for rule in query_intent_rules('identity_name.search_term_rules'):
            markers = [str(marker).strip() for marker in rule.get('markers') or [] if str(marker or '').strip()]
            if markers and any((marker in compact for marker in markers)):
                add(rule.get('term'))
        for term in self.recall_policy.specific_query_terms(text):
            key = self._compact_lookup_key(term)
            if not key or key in seen:
                continue
            identity_keys = ai_keys | user_keys
            if key in identity_keys or any((identity_key and identity_key in key for identity_key in identity_keys)):
                continue
            if any((marker in key for marker in query_intent_terms('identity_name.specific_term_keep_markers'))):
                add(term)
        return terms[:8]

    def _query_date_recall_hint(self, query: str) -> dict[str, str] | None:
        text = str(query or '').strip()
        if not text:
            return None
        return parse_human_date_reference(text, now=datetime.now(self.gateway_tz), tz=self.gateway_tz)

    @staticmethod
    def _compact_lookup_key(value: object) -> str:
        return re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', str(value or '').strip().lower())

    def _typed_surface_reranker_gate(self, query: str, scope: dict[str, Any], semantic_recall_debug: dict[str, Any] | None, *, candidates: list[dict[str, Any]] | None=None, owner_entity_matches: list[dict[str, Any]] | None=None) -> dict[str, Any]:
        semantic_debug = semantic_recall_debug if isinstance(semantic_recall_debug, dict) else {}
        route = str(semantic_debug.get('route') or '').strip()
        # Threshold, margin and boundary checks have already resolved the action.
        # Neither a template default nor query wording can override it here.
        route_action = str(semantic_debug.get('action') or semantic_debug.get('applied_action')
                           or semantic_debug.get('route_action') or 'recall').strip().lower()
        applied = route_action == 'skip'
        reason = (semantic_debug.get('reason') or 'matched_skip_route') if applied else 'typed_retrieval_allowed'
        return {'applied': applied, 'route': route, 'route_action': route_action,
                'template_action': semantic_debug.get('template_action'), 'reason': reason}

    def _typed_pre_candidate_surface_gate(self, query: str, semantic_recall_debug: dict[str, Any] | None) -> dict[str, Any]:
        decision = self._typed_surface_reranker_gate(query, {}, semantic_recall_debug)
        return {**decision, 'stage': 'pre_candidate'}

    @staticmethod
    def _query_has_explicit_recall_structure(query: str) -> bool:
        text = str(query or '')
        return any((marker in text for marker in ('还记得', '记不记得', '是否记得', '那次', '上次', '当时为什么', '后来', '原话', '找出来', '翻一下')))

    def _matched_query_term_is_specific(self, term: Any) -> bool:
        key = self._compact_lookup_key(term)
        if not key:
            return False
        generic_keys = getattr(self, '_generic_query_term_keys_cache', None)
        if generic_keys is None:
            generic_keys = {self._compact_lookup_key(value) for values in (GENERIC_KEYWORD_MATCH_TERMS, GENERIC_LEXICAL_STOPWORDS, QUERY_PLANNER_GENERIC_TERMS) for value in values if self._compact_lookup_key(value)}
            self._generic_query_term_keys_cache = generic_keys
        return key not in generic_keys

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        compact = ' '.join(strip_wikilinks(str(text or '')).split())
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars].rstrip() + '...'

    @staticmethod
    def _hook_recall_how_to_apply() -> str:
        return '[仅你可见]这是过去的你写下的经历，相关才提及，不要复述。 Use only if directly helpful; ignore if irrelevant or conflicting. Do not mechanically repeat or mention retrieval. 需要绑定原文时，用 read_memory(identifier=Event或Scene记忆ID, with_evidence=True) 一次读取正文及全部当前有效证据。'

    @staticmethod
    def _render_hook_recall_additional_context(cards: list[dict[str, Any]]) -> str:
        from ..dates import date_lines
        if not cards:
            return ''
        how_to_apply = GatewayService._hook_recall_how_to_apply()
        parts = ['[Serein Gateway Hook Recall]', 'Retrieved memory notes. Treat them as private context.', f'how_to_apply: {how_to_apply}']
        for card in cards:
            text = str(card.get('text') or '').strip()
            parts.extend([f"[memory_card id={card.get('id') or ''} source={card.get('source_kind') or 'unknown'}]"])
            title = str(card.get('title') or '').strip()
            if title:
                parts.append(f'title: {title}')
            parts.extend(date_lines(card))
            if str(card.get('source_kind') or '') == 'diffused':
                parts.append('association_not_current_fact: true')
            if text:
                parts.append('text: |')
                parts.extend((f'  {line}' for line in text.splitlines()))
            parts.append('[/memory_card]')
        return '\n'.join(parts).strip()

    @staticmethod
    def _render_hook_recall_full_additional_context(dynamic_context: str) -> str:
        text = str(dynamic_context or '').strip()
        if not text:
            return ''
        return '\n'.join(['[Serein Gateway Full Recall]', 'Full Gateway recall context for this turn; no response-generation request was forwarded.', text, '[/Serein Gateway Full Recall]'])
