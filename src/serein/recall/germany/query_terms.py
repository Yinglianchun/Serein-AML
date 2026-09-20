"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

from typing import Any
WEAK_AUTOMATION_TOPIC_TERMS = frozenset({'影分身', '误召回'})
GENERIC_LEXICAL_STOPWORDS = frozenset({'一个', '一些', '一下', '不是', '为了', '他们', '你们', '我们', '什么', '今天', '刚才', '刚刚', '这个', '那个', '这些', '那些', '然后', '现在', '自己', '事情', '东西', '内容', '相关', '记忆', '回忆', '总结', '记录', '查询', '搜索', '最近', '之前', '过去', '当前', '肯定', '可能', '应该', '觉得', '感觉', '好像', '其实', '还是', '就是', '已经', '真的', '可以', '这么', '那么', '安排', '计划', '问题', '目标', '时间', '决定', '嘿嘿', 'ai_name', 'assistant', 'display_name', 'human_name', 'user', 'user_alias', 'user_aliases', 'user_display_name', 'user_name', 'username', '对方', '用户', 'because', 'current', 'from', 'have', 'memory', 'memories', 'recent', 'related', 'search', 'status', 'that', 'this', 'thing', 'things', 'with', 'you', 'qq', 'q_q', 'qaq', 'qwq', 'qvq', 'tt', 't_t'})
DEFAULT_AI_ADDRESS_TERMS = ('助手',)
LEGACY_AI_NAME_ALIASES = ('assistant',)
RECALL_SYSTEM_META_TERMS = frozenset({'关键词', '关键字', '原文', '原话', '召回', '检索', '查记忆', '记忆', '长记忆', '命中', '注入', '召回哨兵', '主域判官', 'recalled', 'recall', 'retrieval', 'memory', 'injected', 'inject', 'diffused', 'diffusion'})
QUERY_PLANNER_GENERIC_TERMS = frozenset({*RECALL_SYSTEM_META_TERMS, 'recent', 'context', 'current', 'remember', 'emotion', 'status', 'thing', 'user', 'assistant', '最近', '记忆', '上下文', '当前', '现在', '记得', '情绪', '状态', '事情', '用户', '助手', '聊天', '对话'})

def configured_identity_terms(identity: dict[str, Any] | None) -> tuple[str, ...]:
    source = identity or {}
    values = (source.get('ai_name'), source.get('user_name'), source.get('user_display_name'), *(source.get('user_aliases') or []))
    return tuple((str(value).strip() for value in values if str(value or '').strip()))

def identity_address_terms(identity: dict[str, Any] | None, *, include_legacy_ai: bool=False) -> tuple[str, ...]:
    values = [*(LEGACY_AI_NAME_ALIASES if include_legacy_ai else ()), *DEFAULT_AI_ADDRESS_TERMS, *configured_identity_terms(identity)]
    return tuple(dict.fromkeys((str(value).strip() for value in values if str(value or '').strip())))
