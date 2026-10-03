"""Frontend policy editing keeps the existing version and confirmation checks."""

from fastapi import APIRouter,HTTPException
import asyncio
from ..compat.publication import publication_config, publication_configured, published_policy
from ..compat.germany.domain_policy import DomainRecallPolicy
from ..configured_models import effective_settings


def routes(settings,auth):
    router=APIRouter(dependencies=auth)
    domains=DomainRecallPolicy(publication_config(settings))
    publish_lock=asyncio.Lock()

    def routes_store():
        # Settings may select/prepare a model after startup. Do not open a stale
        # or unprepared embedding index while registering HTTP endpoints.
        return published_policy(effective_settings(settings))[0]

    @router.get('/api/semantic-recall/routes')
    def read_routes():return routes_store().dataset_payload()

    if publication_configured(settings,'domain_recall_policy'):
        @router.get('/api/semantic-recall/domain-policies')
        def read_domains():return domains.dataset_payload()

    @router.post('/api/semantic-recall/routes/publish')
    async def publish_routes(body:dict):
        try:
            async with publish_lock:
                return await routes_store().publish_dataset(routes=body.get('routes'),
                    expected_dataset_version=int(body['expected_dataset_version']),confirmation=str(body.get('confirm') or ''))
        except (KeyError,TypeError,ValueError) as exc:
            raise HTTPException(409 if str(exc).startswith('route_publish_version_conflict:') else 400,str(exc)) from exc

    if publication_configured(settings,'domain_recall_policy'):
        @router.post('/api/semantic-recall/domain-policies/publish')
        async def publish_domains(body:dict):
            try:
                return await domains.publish_dataset(policies=body.get('policies'),
                    expected_dataset_version=int(body['expected_dataset_version']),confirmation=str(body.get('confirm') or ''))
            except (KeyError,TypeError,ValueError) as exc:
                raise HTTPException(409 if str(exc).startswith('domain_policy_publish_version_conflict:') else 400,str(exc)) from exc

    return router
