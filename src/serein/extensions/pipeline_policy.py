"""Task-local opt-out of editorial reviews; storage and ownership stay strict."""
from contextlib import contextmanager
from contextvars import ContextVar


_editorial_review = ContextVar('pipeline_editorial_review', default=True)


def editorial_review_enabled():
    return _editorial_review.get()


@contextmanager
def editorial_review_scope(enabled=True):
    token = _editorial_review.set(enabled)
    try:
        yield
    finally:
        _editorial_review.reset(token)
