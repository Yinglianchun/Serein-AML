"""Apply the router's final action, with explicit read-only scope data."""

import json
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .germany.gateway import GatewayService
from .germany.identity import identity_names
from .germany.memory_relevance import MemoryRelevanceOptions
from .germany.memory_recall.observed_entities import ObservedEntityShadowIndex
from .germany.recall_policy import RecallPolicy


@cache
def load_gate(profile=None, user_name=None, ai_name=None):
    # Profiles are deployment configuration; restart after replacing one.
    return SurfaceGate(profile, user_name=user_name, ai_name=ai_name)


class SurfaceGate:
    def __init__(self, profile=None, *, user_name=None, ai_name=None):
        data = json.loads(Path(profile).read_text(encoding='utf-8')) if profile else {}
        if user_name is not None and ai_name is not None:
            data['identity'] = {'user_name': user_name, 'user_display_name': user_name, 'ai_name': ai_name, 'user_aliases': []}
        self.engine = GatewayService()
        self.engine.identity = identity_names({'identity': data.get('identity', {})})
        self.engine.gateway_tz = ZoneInfo(data.get('timezone', 'Asia/Shanghai'))
        raw = data.get('relevance_options')
        if raw:
            names = self.engine.identity
            raw = {**raw,
                   'context_terms': [*raw.get('context_terms', []), names['user_name'].casefold(), names['ai_name'].casefold()],
                   'user_terms': [*raw.get('user_terms', []), names['user_name'].casefold()]}
        options = MemoryRelevanceOptions(**raw) if raw else None
        self.engine.recall_policy = RecallPolicy(options, ai_reaction_names=[self.engine.identity['ai_name']])
        self.engine.observed_entity_shadow_index = None
        if data.get('entity_database'):
            path = Path(data['entity_database'])
            if not path.is_absolute():
                path = Path(profile).resolve().parent / path
            if not path.is_file():
                raise ValueError('Configured Germany scope index does not exist')
            index = ObservedEntityShadowIndex({'identity': data.get('identity', {})})
            index.db_path = str(path)
            self.engine.observed_entity_shadow_index = index

    @staticmethod
    def debug(decision, *, user_utterance=False):
        winner = next((row for row in decision.get('scores', [])
                       if row['name'] == decision.get('route')), None)
        return {**decision, 'route_action': decision['action'],
                'template_action': winner['action'] if winner else None,
                'applied_action': decision['action'], 'user_utterance': user_utterance}

    def before_candidates(self, text, decision, *, user_utterance=False):
        return self.engine._typed_pre_candidate_surface_gate(text, self.debug(decision, user_utterance=user_utterance))

    def after_candidates(self, text, decision, candidates, *, user_utterance=False):
        index = self.engine.observed_entity_shadow_index
        scope = index.resolve_query(text) if index else {}
        result = self.engine._typed_surface_reranker_gate(
            text, scope, self.debug(decision, user_utterance=user_utterance))
        return {**result, 'entity_scope': scope}
