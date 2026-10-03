"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

DEFAULT_AI_NAME = 'AI'
DEFAULT_USER_NAME = 'User'
DEFAULT_USER_DISPLAY_NAME = '用户'
DEFAULT_USER_ALIASES = ['对方']

def _clean_string(value, default: str) -> str:
    text = str(value or '').strip()
    return text or default

def _clean_list(value, default: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(',')]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = []
    return [item for item in items if item] or list(default)

def identity_names(config: dict | None=None) -> dict:
    cfg = {}
    if isinstance(config, dict) and isinstance(config.get('identity'), dict):
        cfg = config['identity']
    aliases = _clean_list(cfg.get('user_aliases'), DEFAULT_USER_ALIASES)
    ai_name = _clean_string(cfg.get('ai_name'), DEFAULT_AI_NAME)
    user_name = _clean_string(cfg.get('user_name'), DEFAULT_USER_NAME)
    user_display_name = _clean_string(cfg.get('user_display_name') or cfg.get('human_name'), DEFAULT_USER_DISPLAY_NAME)
    relationship_terms = list(dict.fromkeys([ai_name, user_name, user_display_name, *aliases]))
    return {'ai_name': ai_name, 'user_name': user_name, 'user_display_name': user_display_name, 'user_aliases': aliases, 'user_aliases_text': '、'.join(aliases), 'relationship_terms': relationship_terms}
