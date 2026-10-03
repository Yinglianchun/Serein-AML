import json
import sqlite3

import pytest

from serein.config import Settings
from serein.core import Store
from serein.core.store import digest
from serein.recall.index import build_index,Search,refresh_index
from serein.recall.passages import prepare_passages,fill_passages,slices,full_coverage
from serein.recall.entities import rebuild_entities,candidates
from serein.recall.policy import RecallPolicy
from serein.recall.query import Query


@pytest.fixture
def data(tmp_path):
    database,index=tmp_path/'data.db',tmp_path/'index.db'
    with Store(database) as store:
        store.create('s','scene','长经历','开头的事情。'*50+'\n## 评论\n隐藏的评论实体\n## 经历\n后来的事情。'*20)
        store.create('e','event','事件','无实体的摘要。')
        store.create('no_source','scene','无原文','正文里出现陆光不等于绑定原文')
        source=store.add_source('raw/1','陆光和陆光一起读《时光代理人》。')
        store.bind('e',source)
        store.create('n','narrative','叙事','陆光')
    build_index(database,index)
    profile=dict(model='test',provider_host='test.invalid',query_instruction='',document_instruction='',max_chars=6000)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('s','[0,1]',2)")
    class Client:
        dimension=2
        def __init__(self): self.profile=profile
        def documents(self,texts):
            assert all('隐藏的评论实体' not in text for text in texts)
            return [[1,0] for _ in texts]
    return Settings(database,index,recall={'passages_enabled':True}),Client(),source


def test_passages_are_exact_optional_candidates_and_invalidated_after_edit(data):
    settings,client,_=data
    with Store(settings.database) as store:
        doc=store.read('s')
    spans=slices(doc)
    assert full_coverage(doc,spans) and all(len(doc['body_md'][a:b])<=240 for a,b in spans)
    assert all('隐藏' not in doc['body_md'][a:b] for a,b in spans)
    report=fill_passages(settings,client=client)
    assert report['embedded']>1 and report['coverage']['missing']==0
    assert fill_passages(settings,client=client)['requests']==0
    with Search(settings.database,settings.index) as search:
        options=dict(query_embedding=dict(query='问题',profile=client.profile,embedding=[1,0]),min_cosine=.5)
        assert not search.search('问题',**options)['items']
        result=search.search('问题',use_passages=True,**options)
        assert result['items'][0]['id']=='s' and result['items'][0]['score_channels']['passage']==1
    with Store(settings.database) as store:
        store.revise('s',expected_revision=1,title='修改',body_md='现在很短。')
    refresh_index(settings.database,settings.index,['s'])
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute('SELECT count(*) FROM passages').fetchone()[0]==0


def test_entities_require_current_exact_bound_sources_and_never_claim_arc_membership(data):
    settings,_,source=data
    old=[dict(owner_id='e',entity_text='陆光',scope_eligible=1,confidence_basis='repeated_bound_source')]
    report=rebuild_entities(settings,legacy_rows=old)
    assert report['legacy_revalidated']==1 and report['owners_without_bound_evidence']==2
    with Search(settings.database,settings.index) as search:
        result=candidates(search,Query('陆光后来呢'),RecallPolicy())
        assert [row['id'] for row in result]==['e'] and result[0]['score'] is None
        assert result[0]['method']=='entity' and 'arc' not in json.dumps(result)
    with Store(settings.database) as store:
        store.conn.execute('UPDATE evidence_bindings SET active=0 WHERE source_id=?',(source,))
    with Search(settings.database,settings.index) as search:
        assert candidates(search,Query('陆光'),RecallPolicy())==[]


def test_wrong_legacy_passage_input_is_not_reused(data):
    settings,client,_=data
    legacy={'profile':client.profile,'passage_config':{},'rows':[
        dict(owner_id='s',owner_kind='scene',start_offset=0,end_offset=3,content_hash='wrong',source_hash='wrong',model='test',dimension=2,text='wrong',embedding='[1,0]')]}
    report=prepare_passages(settings,legacy=legacy)
    assert report['legacy_rows_rejected']==1 and report['legacy_vectors_reused']==0


def test_trimmed_legacy_scene_preserves_input_and_maps_canonical_offsets(data):
    settings,client,_=data
    body='素材。'*180
    with Store(settings.database) as store:
        store.create('legacy','scene','原标题','\n'+body)
    config=dict(min_owner_chars=200,target_chars=160,max_chars=240,min_chars=40,overlap_sentences=1)
    signature=dict(schema=4,owner_kind='scene',owner_id='legacy',title='',content=body,
                   model='test',document_instruction='',passage_config=config)
    rows=[dict(owner_id='legacy',owner_kind='scene',start_offset=start,end_offset=end,
               content_hash=digest(body),source_hash=digest(json.dumps(signature,ensure_ascii=False,sort_keys=True)),
               model='test',dimension=2,text=body[start:end],embedding='[1,0]') for start,end in [(0,180),(180,len(body))]]
    report=prepare_passages(settings,legacy=dict(profile=client.profile,passage_config=config,rows=rows))
    assert report['legacy_vectors_reused']==2
    with sqlite3.connect(settings.index) as conn:
        actual=conn.execute("SELECT start_offset,end_offset,text,embedding FROM passages WHERE document_id='legacy' ORDER BY ordinal").fetchall()
        assert [(row[0],row[1]) for row in actual]==[(1,181),(181,541)]
        assert all(('\n'+body)[row[0]:row[1]]==row[2] and row[3] for row in actual)


@pytest.mark.parametrize('kind',['event','scene'])
@pytest.mark.parametrize('length,expected',[(500,False),(501,True),(504,True)])
def test_passage_length_boundary(kind,length,expected):
    doc={'kind':kind,'body_md':'\n'+'字'*length+'\n'}
    assert bool(slices(doc)) is expected


def test_changing_threshold_keeps_prepared_layout_until_body_changes(data,monkeypatch):
    from serein.deployment import save_settings
    settings,client,_=data
    fill_passages(settings,client=client)
    with sqlite3.connect(settings.index) as conn:
        rows=conn.execute("SELECT * FROM passages WHERE document_id='s'").fetchall()
    save_settings(settings.database,{'recall':{'passage_min_chars':10000}})
    monkeypatch.setattr('serein.recall.passages.slices',lambda *a,**kw:pytest.fail('Unchanged memory was split again'))
    report=prepare_passages(settings)
    assert report['owners_reused_local']>0
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute("SELECT * FROM passages WHERE document_id='s'").fetchall()==rows
