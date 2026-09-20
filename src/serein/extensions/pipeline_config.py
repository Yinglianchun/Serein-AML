"""Freeze server-only models for one execution; a later continuation reads updates."""
from contextvars import ContextVar
from contextlib import contextmanager
from ..deployment import read_settings, configured_models

ACTIVE = ContextVar('event_execution_config', default=None)


@contextmanager
def execution(database):
    token=ACTIVE.set(load(database))
    try:yield
    finally:ACTIVE.reset(token)


def load(database):
    settings=read_settings(database)
    catalog={item['id']:item for item in configured_models(settings)}
    return {'revision':settings['settings_version'],'identity':settings['identity'],
            'policy':settings['pipeline'],'models':{role:catalog.get(settings['assignments'].get(role))
                for role in ('track_router','image_transcription','event_curator','event_writer')}}


def snapshot(database, batch_id):
    return ACTIVE.get() or load(database)
