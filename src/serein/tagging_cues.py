"""Shared Scene cue rules for explicitly requested import tagging."""
import re

CUE_PROMPT='''额外返回 cues 数组：从当前 Scene 正文提取 1 至 6 个具体经历线索，每条 2 至 60 字。
不要照搬旧 tags，不要用泛泛的情绪作线索，不生成正文中没有的事实。
cues 中禁止出现 forbidden_names 内任一用户或 AI 的名字及别名，也不使用 User/AI 作为人名占位。
这项禁名只针对 cues，正文不替换名字，实体依旧按原文提取。
无法提取可靠 cue 时返回 []。'''


def forbidden_names(value):
    """Return only configured names/aliases, never descriptions or nested values."""
    if isinstance(value,dict):
        values=[value.get(key) for key in ('user_name','ai_name','user_display_name','ai_display_name')]
        values.extend(value.get(key) for key in ('user_aliases','ai_aliases','aliases'))
    else:values=list(value) if isinstance(value,(list,tuple,set)) else [value]
    result=[]
    for item in values:
        items=item if isinstance(item,(list,tuple,set)) else [item]
        for name in items:
            if isinstance(name,str) and name.strip() and name.strip() not in result:result.append(name.strip())
    return result


def validate_cues(raw,names):
    if not isinstance(raw,list):raise ValueError('缺少 cues 数组')
    blocked=[*forbidden_names(names),'User','AI']
    result=[]
    for cue in raw:
        if not isinstance(cue,str) or not 2<=len(cue.strip())<=60:raise ValueError('cue 长度或格式错误')
        cue=cue.strip()
        rejected=False
        for name in blocked:
            pattern=re.escape(name)
            if name.isascii():pattern=r'(?<![A-Za-z0-9_])'+pattern+r'(?![A-Za-z0-9_])'
            if re.search(pattern,cue,re.I):
                rejected=True;break
        if rejected:continue
        if cue not in result:result.append(cue)
    if len(result)>6:raise ValueError('cues 过多')
    return result

