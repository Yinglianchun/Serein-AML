"""Factories are lazy; disabled optional features never instantiate clients."""
from . import Contributions


def handoff(services, options):
    from .handoff import factory
    return factory(services, options)


def event_pipeline(services, options):
    from .event_pipeline import factory
    return factory(services, options)


def narrative_authoring(services, options):
    from .narrative_authoring import factory
    return factory(services,options)


def background(services, options):
    import asyncio
    from dataclasses import replace
    from ..compat.jobs import BackgroundJobs
    selected = options['feature']
    settings = services._settings
    if not settings.writable or not settings.background:
        raise ValueError(f'{selected} requires writable storage and explicit background configuration')
    async def run():
        await BackgroundJobs(settings, features={selected}).run()
    return Contributions(jobs={selected:run})


def scheduled(feature):
    return lambda services, options: background(services, {**options,'feature':feature})


FACTORIES = {'handoff':handoff, 'event_pipeline':event_pipeline, 'narrative_authoring':narrative_authoring, 'relations':scheduled('relations'),
             'dreams':scheduled('dreams'), 'narrative_scout':scheduled('narrative_scout')}
