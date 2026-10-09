"""Short card correction preserves routing and the core runtime contract."""
import json

import pytest

from aml import engine, runtime
from serein.core.store import Store
from serein.extensions import pipeline
from serein.extensions.pipeline_policy import editorial_review_scope
from test_engine import memory, role_output, add


@pytest.mark.parametrize('length', [600, 601, 692, 751])
@pytest.mark.parametrize('review', [False, True])
def test_core_card_limit_remains_unchanged(length, review):
    task = {'messages': [{'id': 1}]}
    output = role_output('track_router', task)
    output['track_updates'][0]['throughline'] = 'x' * length
    with editorial_review_scope(review):
        if length > 600:
            with pytest.raises(ValueError, match='bounded subject'):
                pipeline.normalize_event_track_message_output(output, task['messages'], [],
                    session_id='length', next_track_ordinal=1)
        else:
            pipeline.normalize_event_track_message_output(output, task['messages'], [],
                session_id='length', next_track_ordinal=1)


def test_length_correction_finishes_real_synthetic_add(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    calls = []

    async def complete(model, payload, **kwargs):
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        calls.append(request['role'])
        output = role_output(request['role'], request)
        if request['role'] == 'track_router':
            rules, prompt = [row['content'] for row in payload['messages']]
            assert 'Aim for 300 characters' in rules
            if calls.count('track_router') == 1:
                output['track_updates'][0]['throughline'] = 'x' * 751
            else:
                assert '"throughline_chars":751' in prompt.replace(' ', '')
                assert 'Keep message assignments, Track references, status and event_policy unchanged' in prompt
                assert 'Do not remove or reassign source messages' in prompt
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    receipt = add()
    assert calls == ['track_router', 'track_router', 'event_curator', 'event_writer']
    assert add() == receipt
    with Store(engine._paths('user-a').database, read_only=True) as store:
        rows = store.conn.execute("SELECT a.output_text,a.error FROM pipeline_attempts a "
            "JOIN pipeline_jobs j ON j.id=a.job_id WHERE j.role LIKE 'track_router%' AND a.output_text!='' "
            "ORDER BY a.rowid").fetchall()
        first, second = [json.loads(row[0]) for row in rows]
        assert first['message_assignments'] == second['message_assignments']
        assert len(first['track_updates'][0]['throughline']) == 751
        assert 'bounded subject' in rows[0][1] and rows[1][1] == ''
        assert not store.conn.execute("SELECT 1 FROM pipeline_batches WHERE status='paused_failure'").fetchone()
