"""Resolve user-side person references for relevance scoring, not source text."""

import re


_QUOTED = re.compile(r'''(“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*"|'[^']*'|`+[^`]*`+)''')
# Only possessives identify whose detail is being requested. Bare pronouns and
# shared references stay conversational rather than adding names to the query.
_PERSON_REFERENCE = re.compile(r'(?<!迷)你的|(?<![自忘])我的')
_PLACEHOLDERS = frozenset({'', 'AI', 'User', '用户'})


def resolve_person_references(query, identity):
    """The query is a user utterance; quoted words keep their own perspective."""
    text = str(query or '').strip()
    names = identity or {}
    assistant = str(names.get('ai_name') or '').strip()
    user = next((name for key in ('user_display_name', 'user_name')
                 if (name := str(names.get(key) or '').strip()) not in _PLACEHOLDERS), '')
    replacements = {}
    if assistant not in _PLACEHOLDERS:
        replacements['你的'] = assistant + '的'
    if user not in _PLACEHOLDERS:
        replacements['我的'] = user + '的'
    parts = _QUOTED.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _PERSON_REFERENCE.sub(lambda match: replacements.get(match[0], match[0]), parts[index])
    return ''.join(parts)
