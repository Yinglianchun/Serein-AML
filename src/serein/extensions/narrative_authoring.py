"""Optional authoring tools; frontend material and save APIs remain available."""
from . import Contributions
from ..compat.narratives import narrative_transaction, RevisionInbox


def factory(services, options):
    database=services._settings.database
    def narrative_revision_inbox(status:str='pending',narrative_id:str='',limit:int=50):
        """Read proposed narrative changes; this does not publish a revision."""
        with narrative_transaction(database) as rolls:
            return RevisionInbox(rolls.store).list(status=status,narrative_id=narrative_id,limit=limit)
    def review_narrative_revision(proposal_id:str,action:str,draft_delta:str='',note:str=''):
        """Save a draft, dismiss or reopen a proposal. Publication is a separate action."""
        with narrative_transaction(database,write=True) as rolls:
            return RevisionInbox(rolls.store).review(proposal_id,action=action,draft_delta=draft_delta,note=note)
    def publish_narrative(request:dict):
        """Explicitly publish an authored narrative with expected_revision and original source identities."""
        with narrative_transaction(database,write=True) as rolls:
            return rolls.publish(**request)
    tools={'narrative_revision_inbox':narrative_revision_inbox}
    if services._settings.writable:
        tools.update(review_narrative_revision=review_narrative_revision,publish_narrative=publish_narrative)
    return Contributions(tools=tools)
