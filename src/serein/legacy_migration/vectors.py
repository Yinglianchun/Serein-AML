import json
from pathlib import Path
import sqlite3
import re
from ..core.store import digest, encode
from ..deployment import task_model
from ..recall.index import Search, refresh_index, unit_vector
from ..recall.passages import prepare_passages
from ..recall.scene import evidence_text
from .models import load_models


def reuse_legacy(settings,profile,plan,ids):
    """Only stored input text with a matching legacy model profile can be reused."""
    result={'whole_reused':0,'passages_reused':0,'unverifiable_whole':0,'invalid_rows':0}
    root=Path(plan['root'])
    originals={}
    for item in plan['items']:
        raw=(root/item['path']).read_text('utf-8-sig')
        originals[item['old_id']]=re.sub(r'\A---\s*\n.*?\n---\s*(?:\n|$)','',raw,count=1,flags=re.S)
    old_models,_=load_models(root)
    old=next((m for m in old_models['models'] if m['id']=='legacy-embedding'),None)
    selected=task_model(settings.database,'embedding')
    compatible=bool(old and selected and all(old.get(k,'')==selected.get(k,'') for k in
        ('model','base_url','document_instruction','query_instruction')))
    refresh_index(settings.database,settings.index,set(ids.values()))
    prepare_passages(settings,document_ids=set(ids.values()))
    with Search(settings.database,settings.index) as search, sqlite3.connect(Path(settings.index).resolve().as_uri()+'?mode=rw',uri=True) as writable:
        dimension=json.loads(search.conn.execute("SELECT value FROM settings WHERE key='embedding_dimension'").fetchone()[0])
        for file in (Path(plan['buckets'])/'embeddings.db',root/'state'/'memory_passage_embeddings.sqlite'):
            if not file.is_file():continue
            with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True) as source:
                source.row_factory=sqlite3.Row
                tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'embeddings' in tables:
                    result['unverifiable_whole']+=source.execute('SELECT count(*) FROM embeddings').fetchone()[0]
                if not compatible:continue
                for table,id_field in [('scene_embedding_chunks','scene_id'),('memory_passage_embeddings','owner_id')]:
                    if table not in tables:continue
                    for row in source.execute('SELECT * FROM '+table):
                        key=ids.get(str(row[id_field]))
                        if not key:continue
                        text=row['text']
                        original=originals.get(str(row[id_field]),'')
                        hash_matches=row['content_hash']==digest(text) or (text in original and row['content_hash'] in (digest(original),digest(original.strip())))
                        # Old engines clamped the input limit to at least 500 characters.
                        # Stay below that lower bound, including the instruction prefix.
                        instruction=profile.get('document_instruction','')
                        prepared=f'Instruct: {instruction}\nDocument: {text}' if instruction else text
                        if len(prepared)>min(500,profile.get('max_chars',12000)):continue
                        if row['model']!=profile['model'] or row['dimension']!=dimension or not hash_matches:continue
                        try:vector=unit_vector(json.loads(row['embedding']),dimension)
                        except (ValueError,TypeError):result['invalid_rows']+=1;continue
                        doc=search.reader.store.read(key)
                        if doc and evidence_text(doc)==text:
                            writable.execute('INSERT OR IGNORE INTO vectors VALUES (?,?,?)',(key,encode(vector),dimension))
                            result['whole_reused']+=writable.execute('SELECT changes()').fetchone()[0]
                        if search.has_passages:
                            writable.execute("UPDATE passages SET embedding=?,dimension=?,origin='legacy_exact_input' WHERE document_id=? AND text=? AND embedding IS NULL",
                                (encode(vector),dimension,key,text))
                            result['passages_reused']+=writable.execute('SELECT changes()').fetchone()[0]
    result['profile_compatible']=compatible
    return result


def maintain(settings,mode):
    from ..configured_models import effective_settings, prepare_selected
    effective=effective_settings(settings,preparing=True)
    if mode=='clean':
        with Search(settings.database,effective.index) as search:
            live={r[0] for r in search.reader.store.conn.execute("SELECT id FROM documents WHERE lifecycle!='deleted'")}
            obsolete={r[0] for r in search.conn.execute('SELECT id FROM documents')}-live
        refresh_index(settings.database,effective.index,obsolete)
        with sqlite3.connect(Path(effective.index).resolve().as_uri()+'?mode=rw',uri=True) as writable:
            writable.execute('DELETE FROM vectors WHERE id NOT IN (SELECT id FROM documents)')
        return {'cleaned_owners':len(obsolete),'canonical_writes':0}
    def clear(current,_profile):
        with sqlite3.connect(Path(current.index).resolve().as_uri()+'?mode=rw',uri=True) as writable:
            writable.execute('DELETE FROM vectors')
            tables={r[0] for r in writable.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'passages' in tables:writable.execute('UPDATE passages SET embedding=NULL,dimension=NULL')
    return prepare_selected(settings,before_fill=clear if mode=='rebuild' else None)
