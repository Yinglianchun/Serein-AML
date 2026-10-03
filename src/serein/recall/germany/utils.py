"""Read-only Germany policy slice; provenance: PROVENANCE.md."""
from __future__ import annotations

import re
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo
LOCAL_TZ = ZoneInfo('Asia/Shanghai')

def _date_hint(year: int, month: int, day: int, label: str, tz=LOCAL_TZ) -> dict[str, str] | None:
    try:
        target = datetime(year, month, day, tzinfo=tz).date()
    except ValueError:
        return None
    return {'date': target.isoformat(), 'label': label}

def _reference_now(now: datetime | None=None, tz=LOCAL_TZ) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)

def parse_human_date_reference(text: str, *, now: datetime | None=None, tz=LOCAL_TZ) -> dict[str, str] | None:
    """Parse common human date references into YYYY-MM-DD."""
    value = str(text or '').strip()
    if not value:
        return None
    base = _reference_now(now, tz)
    explicit = re.search('(?<!\\d)(20\\d{2})\\s*(?:[-/.]|年)\\s*(\\d{1,2})\\s*(?:[-/.]|月)\\s*(\\d{1,2})\\s*(?:日|号)?(?!\\d)', value)
    if explicit:
        year, month, day = (int(part) for part in explicit.groups())
        return _date_hint(year, month, day, explicit.group(0), tz)
    short_year = re.search('(?<!\\d)(\\d{2})\\s*年\\s*(\\d{1,2})\\s*月\\s*(\\d{1,2})\\s*(?:日|号)?', value)
    if short_year:
        year, month, day = (int(part) for part in short_year.groups())
        return _date_hint(2000 + year, month, day, short_year.group(0), tz)
    month_day = re.search('(?<![\\d年/-])(\\d{1,2})\\s*月\\s*(\\d{1,2})\\s*(?:日|号)?', value)
    if month_day:
        month, day = (int(part) for part in month_day.groups())
        return _date_hint(base.year, month, day, month_day.group(0), tz)
    relative_days = [('大前天', -3), ('前天', -2), ('昨晚', -1), ('昨天', -1), ('昨日', -1), ('今晚', 0), ('今天', 0)]
    for label, offset in relative_days:
        if label in value:
            return {'date': (base + timedelta(days=offset)).date().isoformat(), 'label': label}
    return None

def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] → word
    去除 Obsidian 双链括号
    """
    return re.sub('\\[\\[([^\\]]+)\\]\\]', '\\1', text) if text else text
_AFFECT_ANCHOR_RE = re.compile('(?ims)^###\\s*affect_anchor\\s*$.*?(?=^###\\s+|\\Z)')
_FOLLOWUP_SECTION_RE = re.compile('(?ims)^#{2,6}\\s*(?:followup|followups|follow-up|followup_log|followups_log|followup-log|todo|to-do|todo_log|todo-log|next|后续|后续待办|后续记录|待办|待办事项|待办记录)\\s*$.*?(?=^#{2,6}\\s+|\\Z)')

def strip_affect_anchor(text: str) -> str:
    """Remove the display-only affect anchor block from searchable text."""
    if not text:
        return text
    return _AFFECT_ANCHOR_RE.sub('', str(text)).strip()

def strip_followup_sections(text: str) -> str:
    """Remove followup/todo blocks from ordinary recall text."""
    if not text:
        return text
    return _FOLLOWUP_SECTION_RE.sub('', str(text)).strip()

def bucket_content_for_recall(bucket: dict) -> str:
    """Build bucket body text for ordinary recall/search, excluding task-only blocks."""
    if not isinstance(bucket, dict):
        return ''
    text = strip_wikilinks(str(bucket.get('content') or ''))
    meta = bucket.get('metadata', {}) if isinstance(bucket.get('metadata'), dict) else {}
    if str(meta.get('memory_value_source') or '') == 'authored_scene':
        text = _strip_legacy_scene_wrapper(text)
    text = strip_affect_anchor(text)
    return strip_followup_sections(text).strip()

def _strip_legacy_scene_wrapper(text: str) -> str:
    """Read old authored Scenes as prose without rewriting their stored file."""
    raw = str(text or '').strip()
    return re.sub('\\A\\s{0,3}#{2,6}\\s+(?:scene|场景)(?:\\s*[:：|｜-]\\s*[^\\n]*)?\\s*(?:\\r?\\n)+', '', raw, count=1, flags=re.IGNORECASE).strip()

def normalize_scene_cues(value: object, *, limit: int=8, max_chars: int=80) -> list[str]:
    """Normalize sidecar recall entrances without turning them into memory prose."""
    if isinstance(value, str):
        raw_values = re.split('[\\r\\n|｜]+', value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = []
    generic = {'以前', '过去', '关系', '记忆', '事情', '开心', '难过', '情绪', 'something', 'memory'}
    cues: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        cue = re.sub('\\s+', ' ', str(raw or '')).strip(' \t\r\n-—*•、，,。.!！?？:：;；"\'“”‘’')
        if not cue:
            continue
        cue = cue[:max(1, int(max_chars))].rstrip()
        key = re.sub('[\\s\\W_]+', '', cue.lower())
        if len(key) < 2 or key in generic or key in seen:
            continue
        seen.add(key)
        cues.append(cue)
        if len(cues) >= max(1, int(limit)):
            break
    return cues

def bucket_text_for_embedding(bucket: dict) -> str:
    """
    Build the text sent to the embedding model for a bucket.
    Legacy buckets may include title/cues; canonical Scene vectors are body-only.
    """
    if not isinstance(bucket, dict):
        return ''
    meta = bucket.get('metadata', {})
    if not isinstance(meta, dict):
        meta = {}
    if str(meta.get('memory_value_source') or '') == 'authored_scene':
        raw_body = str(bucket.get('content') or '')
        if re.match('\\A\\s{0,3}#{2,6}\\s+(?:scene|场景)(?:\\s*[:：|｜-]\\s*[^\\n]*)?\\s*(?:\\r?\\n)+', raw_body, flags=re.IGNORECASE):
            return _strip_legacy_scene_wrapper(raw_body)
        return raw_body
    title = strip_wikilinks(str(meta.get('name') or '')).strip()
    scene_cues = normalize_scene_cues(meta.get('scene_cues'))
    body = bucket_content_for_recall(bucket)
    parts = []
    if title:
        parts.append(f'Title: {title}')
        if body:
            parts.append(f'Content: {body}')
    elif body:
        parts.append(body)
    if scene_cues:
        parts.append('Recall cues: ' + ' | '.join(scene_cues))
    return '\n'.join(parts).strip()
