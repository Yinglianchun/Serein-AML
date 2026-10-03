import json
import sqlite3

import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index
from serein.recall.service import Recall
from serein.recall.scene import evidence_text


@pytest.fixture
def setup(tmp_path):
    database, index = tmp_path / "runtime.db", tmp_path / "index.db"
    with Store(database) as store:
        store.create("event_a", "event", "雨天归航", "世界之窗，看到第3集", metadata={"local_date": "2026-05-01"}, created_at="2099-01-01")
        store.create("event_b", "event", "后来那一天", "世界之窗，看到第8集", metadata={"local_date": "2026-06-01"}, created_at="2000-01-01")
        store.create("event_undated", "event", "不知日期", "世界之窗", created_at="2099-01-01")
        store.create("scene_a", "scene", "窗边那场雨", "世界之窗，普通正文", metadata={"date": "2026-05-02"})
        store.create("scene_tech", "scene", "技术说明", "世界之窗", metadata={"canonical_domain": "tech"})
        store.create("scene_cue", "scene", "另一个标题", "不相关正文", metadata={"scene_cues": ["红色围巾"]})
        store.create("scene_related", "scene", "相关场景", "边上那一天")
        store.create("scene_proposal", "scene", "未审核", "提案目标")
        store.conn.execute("INSERT INTO scene_relations VALUES ('edge','test','scene_a','scene_related','active',1,'{}')")
        store.conn.execute("INSERT INTO scene_proposals VALUES ('proposal','test','scene_a','scene_proposal','pending','{}')")
        store.create("narrative_a", "narrative", "世界之窗", "不可自动带出的整卷正文")
    build_index(database, index)
    return Settings(database, index, recall={"domains": {"tech": "excluded"}, "max_cards": 2})


def test_lexical_and_cues_are_candidates_not_automatic_hits(setup):
    engine = Recall(setup)
    result = engine.run("世界之窗")
    assert result["status"] == "no_match" and not result["selected_refs"]
    assert result["candidates"] and all("object" not in item for item in result["candidates"])
    assert result["suppressed"]["domain_excluded"] == 1
    cue = engine.run("红色围巾")
    assert cue["candidates"][0]["retrieval_method"] == "cue" and cue["status"] == "no_match"
    assert engine.run("红色围巾", mode="lookup")["selected_refs"] == ["scene:scene_cue"]


def test_named_title_and_actual_delivery_exclusions(setup):
    engine = Recall(setup)
    title = '你还记得《雨天归航》吗？'
    first = engine.run(title)
    assert first["selected_refs"] == ["event:event_a"]
    assert first["pools"]["event"]["items"][0]["admission"] == "explicit_full_title"
    assert engine.run(title)["selected_refs"] == first["selected_refs"]  # Reads do not create cooldown.
    delivered = engine.run(title, delivered_ids=["event:event_a"])
    assert not delivered["selected_refs"] and delivered["suppressed"]["already_delivered"] == 1
    assert engine.run(title, mode="lookup", delivered_ids=["event:event_a"])["selected_refs"]
    assert not engine.run(title, mode="lookup", exclude_ids=["event:event_a"])["selected_refs"]


def test_domain_exclusion_and_scope_cards_do_not_return_narrative_body(setup):
    engine = Recall(setup)
    assert not engine.run("技术说明", mode="lookup")["selected_refs"]
    assert engine.run("故事", intent="narrative")["status"] == "use_narrative_reader"
    assert engine.run("原话", intent="exact")["status"] == "use_evidence_reader"
    cards = engine.find_arc("世界之窗")
    assert cards["items"][0]["id"] == "narrative_a" and cards["scope_only"]
    assert "不可自动带出的整卷正文" not in json.dumps(cards, ensure_ascii=False)


@pytest.mark.parametrize('metadata', [
    {'legacy_registry': {'arc_key': 'work:synthetic'}},
    {'arc_key': 'work:synthetic'},
])
def test_find_arc_space_keywords_fall_back_and_rank_coverage(tmp_path, metadata):
    database, index = tmp_path / 'memory.db', tmp_path / 'index.db'
    with Store(database) as store:
        store.create('both', 'narrative', '时光代理人', '英国庄园的故事', metadata=metadata)
        store.create('title_only', 'narrative', '时光代理人', '早期故事')
        store.create('other', 'narrative', '别的卷', '完全不相关的故事')
        store.create('deleted', 'narrative', '时光代理人', '英国庄园 未出现的词', lifecycle='deleted')
        store.create('scene', 'scene', '时光代理人', '英国庄园 未出现的词')
        store.create('stale', 'narrative', '时光代理人', '英国庄园')
    build_index(database, index)
    with Store(database) as store:
        store.revise('stale', expected_revision=1, title='已经换了主题', body_md='别的内容')
    engine = Recall(Settings(database, index))
    query = '时光代理人\t英国庄园　未出现的词\n时光代理人'
    result = engine.find_arc(query, limit=2)
    assert result['query'] == query and result['match_mode'] == 'keyword_fallback'
    assert [row['id'] for row in result['items']] == ['both', 'title_only']
    assert result['items'][0]['matched_keywords'] == ['时光代理人', '英国庄园']
    assert result['items'][0]['arc_key'] == 'work:synthetic'
    assert result['scope_only'] and not result['injected'] and not result['narrative_body_included']
    assert '英国庄园的故事' not in json.dumps(result, ensure_ascii=False)
    assert len(engine.find_arc(query, limit=1)['items']) == 1
    strict = engine.find_arc('时光代理人 英国庄园')
    assert strict['match_mode'] == 'all_terms' and [row['id'] for row in strict['items']] == ['both']
    assert 'matched_keywords' not in strict['items'][0]
    assert engine.find_arc('不存在的标题')['status'] == 'no_match'
    assert engine.find_arc('不存在的标题 无匹配词')['status'] == 'no_match'
    assert engine.find_arc('   ')['status'] == 'no_match'
    with pytest.raises(ValueError, match='12'):
        engine.find_arc(' '.join('unmatched'+str(i) for i in range(13)))


def test_latest_and_timeline_use_event_date_not_record_creation(setup):
    engine = Recall(setup)
    latest = engine.run("世界之窗", mode="lookup", intent="latest")
    assert latest["selected_refs"] == ["event:event_b"]
    progress = engine.run("世界之窗", mode="lookup", intent="progress")
    assert progress["selected_refs"] == ["event:event_b"]
    timeline = engine.run("世界之窗", mode="lookup", intent="timeline", limit=10)
    assert timeline["selected_refs"] == ["event:event_a", "scene:scene_a", "event:event_b"]


def test_only_confirmed_relationships_expand_from_selected_scene(setup):
    from serein.deployment import save_settings
    save_settings(setup.database,{'features':{'association':True}})
    result = Recall(setup).run("窗边那场雨")
    assert result["selected_refs"] == ["scene:scene_a"]
    assert [item["id"] for item in result["related_candidates"]] == ["scene_related"]
    assert all(item["disposition"] == "candidate" and "object" not in item for item in result["related_candidates"])


def test_disabled_association_keeps_direct_lookup_and_does_not_expand_edges(setup):
    from serein.core.reader import Reader
    from serein.recall.scene import related_candidates
    from serein.recall.query import Query
    engine=Recall(setup)
    for mode in ('surface','lookup'):
        result=engine.run('窗边那场雨',mode=mode)
        assert result['selected_refs']==['scene:scene_a']
        assert result['related_candidates']==[]
    with Reader(setup.database) as reader:
        statements=[];reader.store.conn.set_trace_callback(statements.append)
        assert related_candidates(reader,['scene_a'],Query('窗边那场雨'),engine.policy)==[]
        assert not any('scene_relations' in sql for sql in statements)


def test_context_only_sections_are_not_reranker_body_evidence():
    doc = {"body_md": "正文\n## 评论\n秘密评论\n### 子标题\n秘密子段\n## 真正经历\n原始经历\n## 待办\n未来任务"}
    text = evidence_text(doc)
    assert "正文" in text and "原始经历" in text
    assert "秘密" not in text and "未来任务" not in text


def test_semantic_admission_requires_reranking_full_original_question(setup, monkeypatch):
    profile = {"model": "test", "provider_host": "test.invalid", "query_instruction": "", "document_instruction": "", "max_chars": 6000}
    with sqlite3.connect(setup.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.execute("INSERT INTO vectors VALUES ('event_a','[1,0]',2)")
    class FakeEmbedding:
        def __init__(self, *args, **kwargs):
            pass
        def query(self, text):
            return {"query": text, "profile": profile, "embedding": [1, 0]}
    monkeypatch.setattr("serein.adapters.embedding.EmbeddingClient", FakeEmbedding)
    from dataclasses import replace
    configured = replace(setup, embedding={"endpoint": "unused", "api_key_env": "unused"})
    question = "世界之窗那天发生了什么？"
    assert not Recall(configured).run(question, method="semantic", min_cosine=0.5)["selected_refs"]
    def rerank(text, documents):
        assert text == question and documents[0]["body"] == "世界之窗，看到第3集"
        return {documents[0]["ref"]: 0.64}
    assert not Recall(configured, reranker=rerank).run(question, method="semantic", min_cosine=0.5)["selected_refs"]
    accepted = Recall(configured, reranker=lambda *_: {"event:event_a": 0.9}).run(question, method="semantic", min_cosine=0.5)
    assert accepted["selected_refs"] == ["event:event_a"] and not accepted["injected"]
    def should_not_rerank(*_):
        pytest.fail("Explicit lookup must not request automatic admission scores")
    assert Recall(configured, reranker=should_not_rerank).run(question, mode="lookup", method="semantic", min_cosine=.5)["selected_refs"]


def test_saved_threshold_and_simulation_override_change_only_final_admission(setup, monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.application import Application
    from serein.deployment import read_settings, save_settings
    profile={'model':'threshold-fixture','provider_host':'fixture.invalid','query_instruction':'','document_instruction':'','max_chars':6000}
    with sqlite3.connect(setup.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.executemany("INSERT INTO vectors VALUES (?, '[1,0]', 2)", [('scene_a',),('scene_tech',)])
    class Embedding:
        def __init__(self,*args,**kwargs):pass
        def query(self,text):return {'query':text,'profile':profile,'embedding':[1,0]}
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient',Embedding)
    monkeypatch.setattr('serein.adapters.reranker.RerankerClient',lambda **kwargs:lambda text,docs:{doc['ref']:.62 for doc in docs})
    settings=replace(setup,writable=True,embedding={'endpoint':'unused'},reranker={'endpoint':'unused'})
    state=read_settings(settings.database)
    domains=state['tagging']['domains']
    for domain in domains:
        if domain['key']=='tech':domain['policy']='excluded'
    save_settings(settings.database,{'tagging':{'domains':domains}})
    services=Application(settings).services
    query='世界之窗那天发生了什么？'
    assert not services.recall(query,method='semantic',min_cosine=.5)['selected_refs']
    saved=read_settings(settings.database)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    request={'query':query,'simulation':True,'direct_threshold':.6}
    trial=client.post('/api/hook/recall',json=request)
    assert trial.status_code==200,trial.text
    assert trial.json()['recalled_ids']==['scene:scene_a']
    assert trial.json()['debug']['typed_event_scene_live']['direct_threshold']==.6
    assert trial.json()['injected'] is False
    assert read_settings(settings.database)==saved
    assert not services.recall(query,method='semantic',min_cosine=.5)['selected_refs']
    for value in (-.1,1.1,True,'0.6',None):
        assert client.post('/api/hook/recall',json={**request,'direct_threshold':value}).status_code==400
    assert client.post('/api/hook/recall',json={**request,'simulation':False}).status_code==400
    save_settings(settings.database,{'recall':{'direct_threshold':.6}})
    admitted=services.recall(query,method='semantic',min_cosine=.5)
    assert admitted['selected_refs']==['scene:scene_a'] and admitted['direct_threshold']==.6
    assert not services.recall(query,method='semantic',min_cosine=.5,delivered_ids=['scene:scene_a'])['selected_refs']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM injection_debug').fetchone()[0]==0
    save_settings(settings.database,{'recall':{'direct_threshold':.65}})
    assert not services.recall(query,method='semantic',min_cosine=.5)['selected_refs']
