"""Oversize retry sends an actionable compression request, not a host truncation."""
import json

from aml import engine
from serein.core.store import Store
from serein.extensions import pipeline
from tests_aml.test_engine import memory, role_output, add


def test_writer_length_feedback_and_model_rewrite(memory, monkeypatch):
    monkeypatch.setenv('SEREIN_AML_SIMPLIFY_AUTHORING', '1')
    monkeypatch.setenv('SEREIN_AML_RELAX_CONTENT_REVIEW', '1')
    monkeypatch.setattr(pipeline, 'advance', pipeline.advance.original)
    drafts = []

    async def complete(model, payload, **kwargs):
        with Store(engine._paths('user-a').database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1'
            ).fetchone()[0])
        output = role_output(request['role'], request)
        if request['role'] == 'event_writer':
            rules, prompt = [row['content'] for row in payload['messages']]
            assert 'when unrelated to this Event' in rules
            assert 'Explicit preferences and constraints on how to use them are essential facts' in rules
            assert 'retain whose preference it is, exclusions, exceptions and changes' in rules
            assert 'record the constraint as a fact, not an instruction to the reader' in rules
            assert '1500' not in rules
            if not drafts:
                output['event_draft'] = 'x' * 1697
            else:
                assert 'Current body: 1697 characters.' in prompt
                assert 'usually within 500 characters' in prompt
                assert 'allowed maximum is 1500' not in prompt
                assert 'Keep only key technical points and necessary steps' in prompt
                assert 'never mechanically truncate' in prompt
                assert 'Explicit preferences and constraints on how to use them are essential facts' in prompt
                assert 'based on occupation' in prompt
            drafts.append(output['event_draft'])
        return {'choices': [{'message': {'content': json.dumps(output)}}]}

    monkeypatch.setattr('serein.model_runtime.complete', complete)
    add()
    assert len(drafts) == 2 and len(drafts[0]) == 1697 and len(drafts[1]) < 1500
    with Store(engine._paths('user-a').database, read_only=True) as store:
        saved = json.loads(store.conn.execute(
            "SELECT output_json FROM pipeline_jobs WHERE json_extract(request_json,'$.role')='event_writer'"
        ).fetchone()[0])
        assert saved['event_draft'] == drafts[1]
