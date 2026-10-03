"""Programmatic Arc revision scan plus material access for manual theme discovery."""

from pathlib import Path
from datetime import datetime
import hashlib

from .background import SceneReader, germany_config
from .narratives import narrative_transaction, RevisionInbox, Uploads
from .events import Events
from .diaries import Diaries
from .germany.narrative_scan import NarrativeScout
from .germany.narrative_http import NarrativeHTTP
from .narrative_cleanup import cleanup_retired_bindings
from ..core.notebook import resolve_entry
from ..core.store import Store


class StoreCalls:
    def __init__(self, database, kind):
        self.database, self.kind = database, kind

    def __getattr__(self, name):
        def call(*args, **kwargs):
            write = self.kind == 'inbox' and name not in ('list', 'scan_metadata', 'candidate_contexts')
            with narrative_transaction(self.database, write=write) as rolls:
                target = RevisionInbox(rolls.store) if self.kind == 'inbox' else rolls
                return getattr(target, name)(*args, **kwargs)
        return call


class Scout(NarrativeScout):
    def __init__(self, settings):
        self.settings = settings
        self.config = germany_config(settings)
        self.rolls = StoreCalls(settings.database, 'rolls')
        self.inbox = StoreCalls(settings.database, 'inbox')
        self.events = Events(settings.database)
        self.diaries = Diaries(settings.database)
        self.scenes = SceneReader(settings.database)

    async def _scan_narrative_revision_inbox(self, *, include_external=True, force_external=False):
        self.config = germany_config(self.settings)
        with narrative_transaction(self.settings.database, write=True) as rolls:
            cleaned = cleanup_retired_bindings(rolls)
        result = await super()._scan_narrative_revision_inbox(
            include_external=include_external, force_external=force_external)
        result['retired_material_bindings_removed'] = cleaned
        result['narrative_writes_performed'] = [
            *result.get('narrative_writes_performed', []),
            *({'type': 'retired_material_cleanup', **change, 'body_unchanged': True} for change in cleaned)]
        return result

    async def read_narrative(self, key):
        with narrative_transaction(self.settings.database) as rolls:
            api = NarrativeHTTP()
            api.rolls, api.events, api.uploads = rolls, self.events, Uploads(rolls.store)
            return await api._read_narrative_memory(key)

    async def _narrative_material_freshness(self, narrative, scan_timezone):
        materials = await super()._narrative_material_freshness(narrative, scan_timezone)
        with Store(self.settings.database, read_only=True) as store:
            return [item for item in materials if item['source_type'] != 'event'
                    or not store.promoted_scene(item['source_id'])]

    async def _active_narrative_material_inventory(self, *, exclude_covered_events=False):
        materials = await super()._active_narrative_material_inventory(
            exclude_covered_events=exclude_covered_events)
        with Store(self.settings.database, read_only=True) as store:
            return [item for item in materials if item['source_type'] != 'event'
                    or not store.promoted_scene(item['source_id'])]

    def role_rules(self):
        filename = self.config.get('narrative_rolls',{}).get('scout_role_file') or Path(__file__).resolve().parents[1]/'resources'/'narrative-scout.md'
        rules = Path(filename).read_text('utf-8').strip()
        if not rules:
            raise ValueError('Narrative Scout role file is empty')
        from ..deployment import identity
        from .germany.identity import render_identity_template
        rendered = render_identity_template(rules, identity(self.settings.database))
        return rendered + ('\n\n当前自动 Arc 整理任务：判断新增 Event、Scene、日记应续接输入中的哪个已有 Arc，'
                           '或由至少两份材料形成新的空白 collecting Arc。只返回指定 JSON；host 校验并写材料关系。'
                           '不得写叙事正文，不得编造材料 ID 或 narrative_id。')

    def apply_arc_candidates(self, candidates, *, model):
        """Apply model routing as material-only Arc changes; Narrative prose is never generated."""
        changes = []
        with narrative_transaction(self.settings.database, write=True) as rolls:
            for candidate in candidates:
                ids = {kind: list(dict.fromkeys(candidate.get(f'source_{kind}_ids') or []))
                       for kind in ('event', 'scene', 'diary')}
                valid = True
                dates = []
                for kind, values in ids.items():
                    for raw in values:
                        key = int(raw) if kind == 'diary' else str(raw)
                        document = rolls.store.read(str(key)) if kind != 'diary' else None
                        available = (resolve_entry(rolls.store, key, kind='diary')['resolution'] == 'active'
                                     if kind == 'diary' else bool(document and document['kind'] == kind and document['lifecycle'] == 'active'))
                        if not available:
                            valid = False
                            break
                        stamp = (str(document['metadata'].get('local_date') or document['metadata'].get('date') or '')[:10]
                                 if document else '')
                        if stamp:
                            dates.append(stamp)
                    if not valid:
                        break
                if not valid:
                    continue
                target = str(candidate.get('target_narrative_id') or '')
                if target:
                    result = rolls.append_materials_without_body(target,
                        {f'{kind}_ids': values for kind, values in ids.items()}, model=model)
                    if result.get('status') == 'updated':
                        changes.append({'type': 'existing_arc_materials', **result})
                    continue
                if sum(map(len, ids.values())) < 2:
                    continue
                material_keys = sorted(f'{kind}:{key}' for kind, values in ids.items() for key in values)
                stamp = hashlib.sha256('\n'.join(material_keys).encode()).hexdigest()[:24]
                narrative_id = 'narrative_auto_' + stamp
                title = str(candidate.get('title') or '').strip()[:16]
                if not title:
                    continue
                ledger = '\n'.join(f'- {key}' for key in material_keys)
                result = rolls.publish(narrative_id=narrative_id, expected_revision=0, title=title,
                    document=f'# {title}\n\n## 第一人称叙事\n\n## 来源账\n\n{ledger}\n',
                    arc_key='arc:auto:' + stamp, publication_status='collecting', query_cues=[title],
                    current_status_cue=str(candidate.get('reason') or '')[:500],
                    time_start=min(dates) if dates else '', time_end=max(dates) if dates else '',
                    source_event_ids=ids['event'], source_scene_ids=ids['scene'],
                    source_diary_ids=[int(value) for value in ids['diary']])
                if result.get('status') == 'created':
                    changes.append({'type': 'new_collecting_arc', 'narrative_id': narrative_id,
                                    'revision': result['revision'], 'body_unchanged': True,
                                    'material_ids': ids, 'model': model})
        return changes

    async def run_due(self, current=None):
        config = self._narrative_revision_scan_settings()
        current = current or datetime.now(config['timezone'])
        previous = self._narrative_timestamp(self.inbox.scan_metadata().get('last_scan_at'), config['timezone'])
        target = current.replace(hour=config['hour'], minute=config['minute'], second=0, microsecond=0)
        if (not config['enabled'] or current < target or
                previous and previous.astimezone(config['timezone']).date() == current.date()):
            return {'status': 'not_due'}
        return await self._scan_narrative_revision_inbox()
