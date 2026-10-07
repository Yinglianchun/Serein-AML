import json
from datetime import date

import pytest

from aml import engine, evidence
from serein.core.reader import Reader
from serein.core.store import Store
from test_engine import memory, add, search
from test_narrative_read_guards import published_volume


def pick_all(prompt):
    packet = json.loads(prompt.split('\nEVIDENCE: ', 1)[1])
    return {'selections': [{'ref': row['ref'], 'units': [u['id'] for u in row['units'][:12]],
                            'state': 'evidence'} for row in packet]}


def test_settled_sources_and_dates_reach_final_search_without_new_storage(memory, monkeypatch):
    add([{'role':'user','timestamp':1704067200000,
          'content':'Alice moved to Paris yesterday. Phone: 13812345678. Password: not-a-real-secret.'},
         {'role':'assistant','timestamp':1704067260000,
          'content':'The application was first rejected for a missing income statement.'}])
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE', '1')
    prompts=[]
    def model(prompt):
        prompts.append(prompt)
        return pick_all(prompt)
    monkeypatch.setattr(engine, '_model_json', model)
    result=search()
    assert len(prompts)==1 and len(result)==1
    text=result[0]['content']
    assert 'missing income statement' in text and '2023-12-31' in text
    assert '13812345678' not in text and 'not-a-real-secret' not in text
    assert '13812345678' not in prompts[0] and 'not-a-real-secret' not in prompts[0]
    with Store(engine._paths('user-a').database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
        assert '13812345678' in store.conn.execute('SELECT text FROM raw_events ORDER BY id').fetchone()[0]
    assert search(user_id='other')==[]


@pytest.mark.parametrize('change', ['discard','deactivate','edit'])
def test_bound_source_revocation_cannot_fall_back_to_summary(memory, monkeypatch, change):
    add()
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    database=engine._paths('user-a').database
    def model(prompt):
        with Store(database) as store:
            if change=='discard':
                store.conn.execute("UPDATE raw_events SET metadata_json=json_set(metadata_json,'$.discarded',1)")
            elif change=='deactivate':
                store.conn.execute('UPDATE evidence_bindings SET active=0')
            else:
                store.conn.execute("UPDATE raw_events SET text='changed'")
        return pick_all(prompt)
    monkeypatch.setattr(engine,'_model_json',model)
    assert search()==[]
    # A subsequent read must not leak the old summary either.
    monkeypatch.setattr(engine,'_model_json',pick_all)
    assert search()==[]


def test_unknown_time_is_not_ingestion_time(memory, monkeypatch):
    add([{'role':'user','content':'Alice moved to Paris yesterday.'},
         {'role':'assistant','content':'It was yesterday.'}])
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setattr(engine,'_model_json',pick_all)
    result=search()
    assert 'message_time=unknown' in result[0]['content']
    assert 'relative to message date' not in result[0]['content']


def test_explicit_time_window_finds_nonlexical_event(memory, monkeypatch):
    add([{'role':'user','timestamp':1704067200000,'content':'We planted a pear tree.'},
         {'role':'assistant','timestamp':1704067260000,'content':'The pear tree is planted.'}])
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setattr(engine,'_model_json',pick_all)
    monkeypatch.setattr(engine,'_recall_hits',lambda *a,**k: [])
    monkeypatch.setattr(engine.originals,'hits',lambda *a,**k: [])
    result=search(query='Summarize everything between 2024-01-01 and 2024-01-31.')
    assert result and 'pear tree' in result[0]['content']
    assert search(query='Summarize everything between 2023-01-01 and 2023-01-31.')==[]


def test_ranges_and_date_arithmetic_do_not_guess_today():
    assert evidence.date_range('最近3个月有什么变化') is None
    assert evidence.date_range('截至2024-03-31，最近1个月')==(date(2024,2,29),date(2024,3,31))
    assert evidence.date_range('between 2024-02-01 and 2024-02-29')==(date(2024,2,1),date(2024,2,29))
    assert evidence.date_range('from 2024-03-01 to 2024-02-01') is None
    assert '2023-12-31' in str(evidence.temporal_notes('昨天搬家','2024-01-01T00:00:00+08:00'))
    assert '2024-02-29' in str(evidence.temporal_notes('yesterday','2024-03-01T00:00:00+00:00'))
    assert evidence.temporal_notes('yesterday','')==[]


def test_model_input_cap_and_invalid_selection_fail_closed(memory, monkeypatch):
    add()
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES','4000')
    def invalid(prompt):
        assert len(prompt.encode())<=4000
        packet=json.loads(prompt.split('\nEVIDENCE: ',1)[1])
        return {'selections':[{'ref':packet[0]['ref'],'units':['invented']}]}
    monkeypatch.setattr(engine,'_model_json',invalid)
    assert search()==[]
    monkeypatch.setattr(engine,'_model_json',pick_all)
    assert sum(len(row['content']) for row in search())<=engine._CONTEXT_CHAR_CAP


def test_reranker_receives_redacted_bounded_text(monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SEARCH_INPUT_BYTES','4000')
    seen=[]
    def rank(query, docs):
        seen.append(docs)
        assert len(json.dumps([query,docs],ensure_ascii=False).encode())<=4000
        assert 'secret-value' not in str(docs) and '13812345678' not in str(docs)
        return {'a':.9}
    result=evidence.safe_reranker(rank)('city', [{'ref':'a','body':'Password: secret-value; Phone: 13812345678; '+'树'*10000}])
    assert seen and result=={'a':.9}


def test_ordinary_numbers_and_requested_phone_not_blanket_masked():
    text='Year 2024, quantity 13812345678. Phone: 13812345678; city: Suzhou.'
    assert 'quantity 13812345678' in evidence.minimize(text)
    assert 'Phone: 13812345678' in evidence.minimize(text,'What is my phone number?')
    assert 'redacted' in evidence.minimize('API key: fake-token','What is my API key?')
    masked=evidence.minimize('Phone: 13812345678. City: Suzhou. Password: fake-token.')
    assert evidence.minimize(masked)==masked
    assert 'City: Suzhou' in masked
    packet=json.dumps({'text':'Phone: 13812345678. Password: fake-token.','units':[1,2]})
    assert json.loads(evidence.minimize(packet))['units']==[1,2]
    private='Her phone number must not be disclosed. Phone: 13987654321.'
    assert '13987654321' not in evidence.minimize(private,'Give me her phone number')
    units=evidence.units_for({'id':'test','content':private},[], 'Give me her phone number')
    assert '13987654321' not in str(units)


def test_oversize_query_rejected_before_retrieval(memory,monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setattr(engine,'_rewrite_query',lambda *a: pytest.fail('must reject before sending'))
    with pytest.raises(ValueError,match='input budget'):
        search(query='雨'*10000)


def test_defaults_preserve_existing_search(memory, monkeypatch):
    add()
    monkeypatch.delenv('SEREIN_AML_SOURCE_EVIDENCE',raising=False)
    monkeypatch.setattr(evidence,'select',lambda *a: pytest.fail('opt-in only'))
    assert search()[0]['content']=='Alice moved to Paris.'


def test_scene_and_event_sharing_sources_are_not_duplicate_evidence(memory,monkeypatch):
    add()
    paths=engine._paths('user-a')
    with Store(paths.database) as store:
        event=store.conn.execute("SELECT id FROM documents WHERE kind='event'").fetchone()[0]
        store.create('scene_copy','scene','Alice moved','Alice moved to Paris.',manual_surface=True)
        for binding in store.conn.execute('SELECT source_id,metadata_json FROM evidence_bindings WHERE document_id=?',(event,)).fetchall():
            store.bind('scene_copy',binding['source_id'],metadata=json.loads(binding['metadata_json']))
    engine._sync_index(paths)
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setattr(engine,'_model_json',pick_all)
    found=search()
    assert len(found)==1
    assert found[0]['content'].count('Alice moved to Paris.')==1


@pytest.mark.parametrize('edit_during_selection',[False,True])
def test_volume_selection_keeps_source_guards(published_volume,monkeypatch,edit_during_selection):
    paths,_=published_volume
    original=engine._model_json
    def model(prompt):
        if '\nEVIDENCE: ' not in prompt:
            return original(prompt)
        result=pick_all(prompt)
        if edit_during_selection:
            with Store(paths.database) as store:
                doc=store.read('scene_factory')
                store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],
                             body_md='The factory is now CedarVale.',metadata=doc['metadata'])
        return result
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    monkeypatch.setattr(engine,'_model_json',model)
    found=engine.search_memory(query='What is the AsterBridge history?',options=None,user_id='synthetic-user',top_k=10)
    assert ('narrative_device' in {r['id'] for r in found}) is not edit_during_selection
