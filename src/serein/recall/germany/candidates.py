"""Read-only candidate selection extracted from Germany 9c99c591; see PROVENANCE.md."""
from __future__ import annotations
import re
from typing import Any
from .gateway import GatewayService
from .memory_recall.typed_candidate_shadow import build_scene_lane,build_event_lane,balanced_typed_pool,rerank_lane_with_freshness

class CandidateGateway(GatewayService):

    def _passage_candidate_shadow_debug(self, query: str, query_embedding: list[float]) -> dict[str, Any]:
        if not getattr(self, 'passage_candidate_shadow_enabled', False):
            return {'status': 'disabled', 'decision_applied': False, 'live_injection_enabled': False}
        required = ('passage_shadow_index', 'cue_passage_shadow_index', 'fact_event_lexical_shadow_index', 'fact_event_semantic_index')
        if any((not hasattr(self, name) for name in required)):
            return {'status': 'unavailable', 'reason': 'shadow_indexes_unavailable', 'decision_applied': False, 'live_injection_enabled': False}
        catalog = getattr(self, '_passage_candidate_shadow_catalog', {})
        global_scene_ids = {item_id for item_id, row in catalog.items() if row.get('owner_kind') == 'scene'}
        global_event_ids = {item_id for item_id, row in catalog.items() if row.get('owner_kind') == 'event'}
        entity_scope = self.observed_entity_shadow_index.resolve_query(query) if hasattr(self, 'observed_entity_shadow_index') else {'status': 'unavailable', 'operator': 'none', 'retrieval_allowed': False, 'decision_applied': False}
        scope_status = str(entity_scope.get('status') or '')
        scope_operator = str(entity_scope.get('operator') or 'none')
        scope_arc_key = str((entity_scope.get('scope_anchor') or {}).get('arc_key') or '')
        scope_members = set(getattr(self, '_passage_candidate_shadow_arc_members', {}).get(scope_arc_key, set()))
        specific_term_reader = getattr(getattr(self, 'recall_policy', None), 'specific_query_terms', None)
        raw_global_terms = specific_term_reader(query) if callable(specific_term_reader) else re.findall('[A-Za-z0-9_.-]{2,}|[\\u4e00-\\u9fff]{2,}', str(query or ''))
        specific_global_terms = [term for term in raw_global_terms if self._matched_query_term_is_specific(term)]
        deictic_scope_missing = bool(scope_status == 'insufficient_scope' and scope_operator in {'latest_relevant_member', 'timeline', 'member_search'})
        # A name is a retrieval hint even without a recall phrase. Multiple
        # possible Arc scopes fall back to ordinary global Event/Scene search.
        global_named_fallback = bool((deictic_scope_missing or scope_status == 'ambiguous_scope') and specific_global_terms)
        if global_named_fallback:
            fallback = 'ambiguous_scope_global_event_scene' if scope_status == 'ambiguous_scope' else 'specific_term_global_event_scene'
            entity_scope = {**entity_scope, 'status': 'global_recall', 'operator': 'none', 'retrieval_allowed': True, 'scope_fallback': fallback, 'decision_applied': False}
            scope_status = 'global_recall'
            scope_operator = 'none'
        hard_scope_block = bool(deictic_scope_missing and not global_named_fallback)
        if hard_scope_block or (not scope_arc_key and not specific_global_terms):
            return {'status': 'not_retrieved', 'reason': 'scope_required_for_deictic_intent' if hard_scope_block else 'global_query_lacks_specific_terms', 'mode': 'simulation_shadow', 'decision_applied': False, 'live_injection_enabled': False, 'entity_scope': entity_scope, 'candidate_count': 0, 'candidates': [], 'lanes': {'scene': {'matches': []}, 'event': {'matches': []}}}
        allowed_owner_keys = scope_members if scope_arc_key else {*(('scene', owner_id) for owner_id in global_scene_ids), *(('event', owner_id) for owner_id in global_event_ids)}
        allowed_scene_ids = {owner_id for kind, owner_id in allowed_owner_keys if kind == 'scene'}
        allowed_event_ids = {owner_id for kind, owner_id in allowed_owner_keys if kind == 'event'}
        scene_passage_search = self.passage_shadow_index.search_by_embedding(query_embedding, top_k=10, owner_kinds=('scene',), passages_per_owner=2, allowed_owner_ids=allowed_owner_keys)
        scene_passage_rows = list(scene_passage_search.get('matches') or [])
        scene_whole_matches = []
        scene_whole_search = getattr(getattr(self, 'embedding_engine', None), 'search_scene_whole_by_embedding', None)
        if callable(scene_whole_search):
            scene_whole_matches = scene_whole_search(query_embedding, scene_ids=allowed_scene_ids, top_k=10)
        scene_whole_rows = []
        for match in scene_whole_matches:
            scene_id = str(match.get('scene_id') or '')
            item = catalog.get(scene_id) or {}
            body = str(item.get('body') or '')
            score = float(match.get('score') or 0.0)
            scene_whole_rows.append({'owner_kind': 'scene', 'owner_id': scene_id, 'score': score, 'passages': [{'ordinal': 0, 'start_offset': 0, 'end_offset': len(body), 'text': body, 'score': score}], 'candidate_only': True, 'decision_applied': False})
        event_passage_search = self.passage_shadow_index.search_by_embedding(query_embedding, top_k=10, owner_kinds=('event',), passages_per_owner=2, allowed_owner_ids=allowed_owner_keys)
        event_passage_rows = list(event_passage_search.get('matches') or [])
        cue_search = self.cue_passage_shadow_index.search_by_embedding(query_embedding, top_k=10, allowed_scene_ids=allowed_scene_ids)
        cue_rows = list(cue_search.get('matches') or [])
        fact_search = self.fact_event_semantic_index.search_by_embedding(query_embedding, top_k=20, memory_kinds=('event',), allowed_memory_ids=allowed_event_ids if scope_arc_key else None)
        body_rows: list[dict[str, Any]] = []
        for match in fact_search.get('matches') or []:
            item_id = str(match.get('memory_id') or '')
            item = catalog.get(item_id) or {}
            body = str(item.get('body') or '')
            body_rows.append({'owner_kind': str(match.get('memory_kind') or ''), 'owner_id': item_id, 'score': float(match.get('score') or 0.0), 'passages': [{'ordinal': 0, 'start_offset': 0, 'end_offset': len(body), 'text': body, 'score': float(match.get('score') or 0.0)}], 'candidate_only': True, 'decision_applied': False})
        lexical_search = self.fact_event_lexical_shadow_index.search(query, top_k=20, memory_kinds=('event',), allowed_memory_ids=allowed_event_ids if scope_arc_key else None)
        lexical_rows = list(lexical_search.get('matches') or [])
        scene_lane = build_scene_lane(scene_passage_rows, cue_rows, scene_whole_rows)
        event_lane = build_event_lane(event_passage_rows, body_rows, lexical_rows)

        def decorate_lane(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for row in rows:
                item = catalog.get(str(row.get('owner_id') or '')) or {}
                row['title'] = str(item.get('title') or '')
                row['memory_date'] = str(item.get('memory_date') or '')
                row['recallable'] = item.get('recallable')
            return rerank_lane_with_freshness(rows, query=query)
        scene_lane = decorate_lane(scene_lane)
        event_lane = decorate_lane(event_lane)
        pool = balanced_typed_pool([('scene', scene_lane, 3), ('event', event_lane, 3)], limit=6)
        owner_query_matcher = getattr(getattr(self, 'observed_entity_shadow_index', None), 'owner_query_matches', None)
        owner_entity_matches = owner_query_matcher(query, owner_keys={(str(row.get('owner_kind') or ''), str(row.get('owner_id') or '')) for row in pool if self._typed_owner_ref(row)}) if callable(owner_query_matcher) else []
        member_to_arcs: dict[tuple[str, str], list[str]] = {}
        for arc_key, members in getattr(self, '_passage_candidate_shadow_arc_members', {}).items():
            for member in members:
                member_to_arcs.setdefault(member, []).append(arc_key)
        for row in pool:
            item = catalog.get(str(row.get('owner_id') or '')) or {}
            row['title'] = str(item.get('title') or '')
            row['recallable'] = item.get('recallable')
            cards = [dict(card) for arc_key in sorted(member_to_arcs.get((str(row.get('owner_kind') or ''), str(row.get('owner_id') or '')), []))[:3] if (card := getattr(self, '_passage_candidate_shadow_arc_cards', {}).get(arc_key))]
            if cards:
                row['arc_cards'] = cards
            if hasattr(self, 'observed_entity_shadow_index'):
                row['arc_link_candidates'] = self.observed_entity_shadow_index.link_candidates(str(row.get('owner_kind') or ''), str(row.get('owner_id') or ''))
        return {'status': 'ok', 'mode': 'simulation_shadow', 'decision_applied': False, 'live_injection_enabled': False, 'sync': getattr(self, '_passage_candidate_shadow_sync', {}), 'policy': {'pool_limit': 6, 'lane_quotas': {'scene': 3, 'event': 3}, 'duplicate_score_boost': False, 'within_owner_embedding_score': 'max', 'embedding_routes': ['whole', 'passage_for_long_owner'], 'cross_lane_score_comparison': False, 'freshness_rerank': 'bounded_within_lane', 'cue_contributes_score': False, 'lexical_contributes_score': False, 'scope_applied_before_candidate_search': bool(scope_arc_key), 'scope_arc_key': scope_arc_key, 'global_specific_terms': specific_global_terms, 'global_named_fallback': global_named_fallback, 'event_eligibility': 'all_active'}, 'lanes': {'scene': {'matches': scene_lane, 'whole_search': {'status': 'ok', 'candidate_count': len(scene_whole_rows), 'matches': scene_whole_rows}, 'passage_search': scene_passage_search, 'cue_search': cue_search}, 'event': {'matches': event_lane, 'passage_search': event_passage_search, 'body_search': fact_search, 'lexical_search': lexical_search}}, 'entity_scope': entity_scope, 'owner_entity_matches': owner_entity_matches, 'candidate_count': len(pool), 'candidates': pool}

    @staticmethod
    def _typed_owner_ref(row: dict[str, Any]) -> str:
        kind = str(row.get('owner_kind') or '').strip().lower()
        owner_id = str(row.get('owner_id') or '').strip()
        return f'{kind}:{owner_id}' if kind and owner_id else ''

    @staticmethod
    def _typed_reranker_document(row: dict[str, Any]) -> str:
        title = str(row.get('title') or '').strip()
        passages = [str(passage.get('text') or '').strip() for passage in row.get('passages') or [] if isinstance(passage, dict) and str(passage.get('text') or '').strip()]
        body = '\n'.join(passages[:2])
        return f"title: {title}\nbody: {body}"[:4000]
