import re
import pytest
from test_live_clients import live
from serein.core.store import Store


def create(client,title,content):
    result=client.post('/v1/tools/call',json={'name':'write_scene','arguments':{'title':title,'content':content,'cues':['窗边雨声']}})
    assert result.status_code==200,result.text
    return re.search(r'\[scene_id:([^]]+)\]',result.json()['result'])[1]


def seed(client):
    source=create(client,'窗边','那天我们坐在窗边，听了一整晚的雨。')
    target=create(client,'再一次的雨','后来又下雨了，我想起那天窗边的雨声。')
    response=client.post('/api/scene-edge-proposals/manual',json={'source_scene_id':source,'target_scene_id':target,
        'relation_type':'echoes','source_evidence':'那天我们坐在窗边，听了一整晚的雨。',
        'target_evidence':'后来又下雨了，我想起那天窗边的雨声。','reason':'同一段窗边雨声的再次回望。',
        'confirm':'CREATE_SCENE_EDGE_PROPOSAL'})
    assert response.status_code==200,response.text
    rows=client.get('/api/scene-edge-proposals?include_context=true').json()
    assert len(rows['proposals'])==1,rows
    return source,target,rows['proposals'][0]['proposal_id']


def test_reviewed_relation_projects_atomically_and_can_be_removed(live):
    settings,client=live
    source,target,proposal=seed(client)
    response=client.post('/api/scene-edge-proposals/review',json={'proposal_id':proposal,'decision':'accept','confirm':'ACCEPT_SCENE_EDGE'})
    assert response.status_code==200,response.text
    edge=client.get('/api/scene-edges').json()['edges'][0]
    projection=client.post('/api/serein/memory-projection',json={}).json()
    assert len(projection['edges'])==1
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT active FROM scene_relations').fetchone()[0]==1
    response=client.request('DELETE','/api/scene-edges/'+edge['edge_id'],json={'scene_id':source,'confirm':'DELETE_SCENE_EDGE'})
    assert response.status_code==200,response.text
    assert client.post('/api/serein/memory-projection',json={}).json()['edges']==[]
    response=client.post('/api/scene-edges/'+edge['edge_id']+'/restore',json={'confirm':'RESTORE_SCENE_EDGE'})
    assert response.status_code==200,response.text
    assert len(client.post('/api/serein/memory-projection',json={}).json()['edges'])==1


def test_changed_scene_cannot_promote_old_proposal(live):
    settings,client=live
    source,target,proposal=seed(client)
    current=client.post('/v1/tools/call',json={'name':'read_memory','arguments':{'memory_type':'scene','memory_id':source}}).json()['result']
    client.post('/v1/tools/call',json={'name':'edit_scene','arguments':{'scene_id':source,
        'expected_updated_at':current['metadata']['updated_at'],'content':'这张卡的正文已经重新写过。'}})
    response=client.post('/api/scene-edge-proposals/review',json={'proposal_id':proposal,'decision':'accept','confirm':'ACCEPT_SCENE_EDGE'})
    assert response.json()['status']!='accepted',response.text
    assert client.get('/api/scene-edges').json()['edges']==[]


@pytest.mark.parametrize('reason,ok',[('雨声回响',True),(' ',False)])
def test_manual_relation_requires_reason_without_minimum_length(live,reason,ok):
    _,client=live
    source=create(client,'Synthetic A','A synthetic shared moment beside the window.')
    target=create(client,'Synthetic B','Another synthetic memory beside the window.')
    result=client.post('/api/scene-edge-proposals/manual',json={
        'source_scene_id':source,'target_scene_id':target,'relation_type':'echoes',
        'source_evidence':'A synthetic shared moment beside the window.',
        'target_evidence':'Another synthetic memory beside the window.',
        'reason':reason,'confirm':'CREATE_SCENE_EDGE_PROPOSAL'})
    assert (result.status_code==200)==ok,result.text


def test_imported_boundary_whitespace_keeps_reviewed_relation_visible(live):
    settings,client=live
    source,target,proposal=seed(client)
    accepted=client.post('/api/scene-edge-proposals/review',json={
        'proposal_id':proposal,'decision':'accept','confirm':'ACCEPT_SCENE_EDGE'})
    assert accepted.status_code==200,accepted.text
    with Store(settings.database) as store:
        for key in (source,target):
            doc=store.read(key)
            store.revise(key,expected_revision=doc['revision'],title=doc['title'],
                         body_md='\n\n'+doc['body_md']+'\n')
    projection=client.post('/api/serein/memory-projection',json={}).json()
    assert len(projection['edges'])==1
    with Store(settings.database,read_only=True) as store:
        assert store.read(source)['body_md'].startswith('\n\n')
        assert store.conn.execute('SELECT active FROM scene_relations').fetchone()[0]==1


@pytest.mark.parametrize('change', ['body', 'deleted', 'archived', 'title'])
def test_pending_list_retires_obsolete_proposals_before_counting(live, change):
    settings,client=live
    source,target,proposal=seed(client)
    with Store(settings.database) as store:
        if change in ('deleted', 'archived'):
            store.set_lifecycle(target, change)
        else:
            doc=store.read(source)
            store.revise(source,expected_revision=doc['revision'],
                         title='新标题' if change=='title' else doc['title'],
                         body_md='已经改写，原来的经历不成立。' if change=='body' else doc['body_md'])
    result=client.get('/api/scene-edge-proposals?limit=1').json()
    expected='pending' if change=='title' else 'superseded'
    assert result['count']==(1 if change=='title' else 0),result
    assert result['stats']['pending']==result['count']
    with Store(settings.database,read_only=True) as store:
        for table,field in [('scene_edge_proposals','proposal_id'),('scene_proposals','id')]:
            assert store.conn.execute(f'SELECT status FROM {table} WHERE {field}=?',(proposal,)).fetchone()[0]==expected
    history=client.get('/api/scene-edge-proposals?status=all').json()
    assert history['proposals'][0]['status']==expected


@pytest.mark.parametrize('evidence,ok',[('A',True),('A s',True),(' ',False),('missing',False)])
def test_manual_relation_evidence_only_requires_nonempty_source_match(live,evidence,ok):
    _,client=live
    source=create(client,'Synthetic A','A synthetic shared moment.')
    target=create(client,'Synthetic B','A synthetic second moment.')
    result=client.post('/api/scene-edge-proposals/manual',json={
        'source_scene_id':source,'target_scene_id':target,'relation_type':'echoes',
        'source_evidence':evidence,'target_evidence':evidence,
        'reason':'Echo','confirm':'CREATE_SCENE_EDGE_PROPOSAL'})
    assert (result.status_code==200)==ok,result.text
    proposals=client.get('/api/scene-edge-proposals').json()['proposals']
    assert len(proposals)==(1 if ok else 0),proposals
