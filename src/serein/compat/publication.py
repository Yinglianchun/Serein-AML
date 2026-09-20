"""Preserve Germany's versioned route/domain publication contracts."""

import asyncio
from pathlib import Path

from ..adapters.embedding import EmbeddingClient
from .background import germany_config
from .germany.semantic_router import SemanticRecallRouter
from .germany.domain_policy import DomainRecallPolicy


class RouteEmbedding:
    def __init__(self,settings):
        self.client=EmbeddingClient(settings.database,settings.index,**settings.embedding)
        self.model=self.client.profile['model']
        self.query_instruction=self.client.profile['query_instruction']
        self.max_chars=self.client.profile['max_chars']
        self.enabled=True

    async def embed_query(self,text):
        return (await asyncio.to_thread(self.client.query,text))['embedding']


def publication_config(settings):
    """Default publication storage belongs to this instance, never the package."""
    config=germany_config(settings)
    gateway=dict(config.get('gateway') or {})
    for key,directory in (('semantic_recall_router','semantic_recall_routes'),
                          ('domain_recall_policy','domain_recall_policies')):
        policy=dict(gateway.get(key) or {})
        path=Path(policy.get('publish_dir') or directory).expanduser()
        policy['publish_dir']=str((settings.database.parent/path).resolve())
        gateway[key]=policy
    return {**config,'gateway':gateway}


def publication_configured(settings,kind):
    config=publication_config(settings)['gateway'][kind]
    raw=germany_config(settings).get('gateway',{}).get(kind,{}) or {}
    return bool(raw.get('publish_dir') or raw.get('routes_path') or raw.get('index_path')
                or (Path(config['publish_dir'])/'active.json').exists())


def published_policy(settings):
    config=publication_config(settings)
    return SemanticRecallRouter(config,RouteEmbedding(settings)),DomainRecallPolicy(config)


def recall_policy_data(settings):
    """Unpublished public installs keep prepared routes and saved domain choices.

    An existing manifest or explicit seed is authoritative: invalid publications
    must fail validation, rather than silently bypass their recall boundaries.
    This only reads files and the embedding profile; it never embeds or publishes.
    """
    config=publication_config(settings)
    route_config=config['gateway']['semantic_recall_router']
    published=None
    if ((Path(route_config['publish_dir'])/'active.json').exists()
            or route_config.get('routes_path') or route_config.get('index_path')):
        published=route_data(SemanticRecallRouter(config,RouteEmbedding(settings)))
    domains=DomainRecallPolicy(config)
    policies=({item['key']:item['policy'] for item in domains.dataset_payload()['policies']}
              if domains.active else None)
    return published,policies


def route_data(router):
    index,error=router._load_index()
    if error:raise ValueError(error)
    payload=router.dataset_payload()
    return {'format':'serein-query-routes-v1','profile':router.embedding_engine.client.profile,
        'dimension':index['embedding']['dimension'],'generation':str(payload['dataset_version']),
        'policy':{key:getattr(router,key) for key in ('min_score','min_margin','aggregation_top_k',
            'boundary_veto_enabled','boundary_veto_min_score','boundary_veto_max_deficit')},
        'routes':[{'name':row['name'],'action':row['action'],'threshold':row['threshold'],
            'vectors':[item['embedding'] for item in row['utterances']]} for row in index['routes']],
        'boundaries':[{'action':row['action'],'vector':row['embedding']} for row in index['boundaries']]}
