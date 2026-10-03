"""Refresh Germany's scope index from current canonical bound originals."""

import json
from pathlib import Path
from contextlib import closing

from ..core.reader import Reader
from .germany.observed_entities import ObservedEntityShadowIndex
from .narratives import Narratives
from .background import germany_config


def update_scope(settings):
    if not settings.recall.get('germany_policy_file'):
        return []
    filename=Path(settings.recall['germany_policy_file'])
    profile=json.loads(filename.read_text('utf-8'))
    if not profile.get('entity_database'):return []
    target=Path(profile['entity_database'])
    if not target.is_absolute():target=filename.parent/target
    index=ObservedEntityShadowIndex(germany_config(settings))
    index.db_path=str(target)
    with Reader(settings.database) as reader:
        reader.store.conn.execute('BEGIN')
        owners=[]
        for row in reader.store.conn.execute("SELECT id,kind FROM documents WHERE kind IN ('scene','event') AND lifecycle='active'"):
            obj=reader.read(row['id'],with_evidence=True)
            owners.append({'owner_kind':row['kind'],'owner_id':row['id'],'source_refs':[
                {**ref['metadata'],'content':ref['content']} for ref in obj['evidence']]})
        profiles=Narratives(reader.store).recall_scope_profiles()
        for profile in profiles:
            for row in reader.store.conn.execute('SELECT event_id FROM event_arc_links WHERE arc_key=?',(profile['arc_key'],)):
                member={'owner_kind':'event','owner_id':row[0]}
                if member not in profile['members']:profile['members'].append(member)
    index.sync(owners=owners,arc_profiles=profiles)
    with closing(index._connect(readonly=True)) as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM observed_entities')]
