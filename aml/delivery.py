"""Opt-in delivery guards and content-free, request-local diagnostics."""
import contextlib
import contextvars
import json
import logging
import os
import uuid

_REASONS = {
    'MODEL_FORMAT_ERROR', 'MODEL_CALL_ERROR', 'INVALID_UNIT_ID_TYPE', 'INVALID_UNIT_ID',
    'INVALID_SELECTION', 'INPUT_BUDGET', 'OUTPUT_BUDGET', 'DUPLICATE_UNIT',
    'INCOMPLETE_GROUP', 'SOURCE_CHANGED', 'UNREADABLE_OR_CHANGED', 'UNREADABLE',
    'DOMAIN_FILTER', 'UNIT_BYTES', 'UNIT_WINDOW', 'TOP_K_GROUP', 'GROUP_CARRIERS_PRESENT',
    'DELIVERY_ERROR', 'GROUP_INVALIDATED',
}


def enabled():
    return os.getenv('SEREIN_AML_DELIVERY_FIXES', '0') == '1'


class DeliveryError(RuntimeError):
    def __init__(self, reason):
        if reason not in _REASONS:
            reason = 'DELIVERY_ERROR'
        self.reason = reason
        super().__init__(reason)


class Result(list):
    """Physical carriers only; this does not certify semantic sufficiency."""
    def __init__(self, rows, carriers):
        super().__init__(rows)
        self.carriers = carriers


class Carriers(dict):
    """Group-scoped physical equivalence, never a global semantic identity merge."""
    def __init__(self, rows=()):
        super().__init__(rows)
        self.proofs = []
        self.validate = None

    def refresh(self, extra=None):
        self.proofs = [(group, targets) for group, targets in self.proofs
                       if all(self.validate(ref) and (extra is None or extra(ref)) for ref in targets)]


def group_targets(group, carriers):
    mapping = carriers
    if isinstance(carriers, Carriers):
        mapping = next((targets for reviewed, targets in carriers.proofs if reviewed is group), carriers)
    targets = [mapping.get(key, set()) for key in group['ids']]
    return set().union(*targets) if targets and all(targets) else set()


_audit = contextvars.ContextVar('aml_delivery_audit', default=None)


def _emit(event):
    # Opt-in diagnostics must survive the service's default WARNING threshold.
    # Do not change the global logger level or enable unrelated content logs.
    logging.getLogger(__name__).warning('aml_delivery %s', json.dumps(event))


@contextlib.contextmanager
def request():
    token = _audit.set({'request': uuid.uuid4().hex, 'aliases': {}} if
                       os.getenv('SEREIN_AML_DELIVERY_AUDIT', '0') == '1' else None)
    try:
        yield
    finally:
        _audit.reset(token)


def observe(stage, ref=None, *, count=1, length=0, reason=None):
    audit = _audit.get()
    if audit is None:
        return
    event = {'request': audit['request'], 'stage': stage, 'count': count, 'length': length}
    if ref is not None:
        event['material'] = audit['aliases'].setdefault(ref, uuid.uuid4().hex)
    if reason:
        event['reason'] = reason if reason in _REASONS else 'DELIVERY_ERROR'
    _emit(event)


def finish(rows, groups, direct, carriers=None):
    """Remove orphan expansions to a fixed point; direct hits remain independent."""
    rows = list(rows)
    if carriers is None:
        carriers = {row['id']: {row['id']} for row in rows}
    while True:
        live = {row['id'] for row in rows}
        accepted = set()
        for group in groups:
            targets = group_targets(group, carriers)
            if targets and targets <= live:
                accepted.update(group['ids'])
        kept = [row for row in rows if row['id'] in direct or row['id'] in accepted]
        if len(kept) == len(rows):
            observe('group', count=len(accepted & live), reason='GROUP_CARRIERS_PRESENT')
            return kept
        for row in rows:
            if row not in kept:
                observe('group', row['id'], reason='INCOMPLETE_GROUP')
        rows = kept


def allocate(rows, groups, direct, cap, carriers=None):
    """Keep whole selected excerpts for a group, never silently split its budget."""
    by_id = {row['id']: row for row in rows}
    chosen = set()
    grouped = {key for group in groups for key in group['ids']}
    if carriers is None:
        carriers = {key: {key} for key in by_id}
    for group in sorted(groups, key=lambda item: item.get('route') != 'arc_menu'):
        ids = group_targets(group, carriers)
        if not ids or not ids <= by_id.keys():
            for key in group['ids']:
                observe('group', key, reason='INCOMPLETE_GROUP')
            continue
        required = sum(len(by_id[key]['content']) for key in ids - chosen)
        if required <= cap:
            chosen.update(ids)
            cap -= required
        else:
            for key in ids - chosen:
                observe('budget', key, reason='OUTPUT_BUDGET')
    for row in rows:
        key = row['id']
        if key in chosen:
            continue
        if key in grouped and key not in direct:
            observe('rejected', key, reason='INCOMPLETE_GROUP')
            continue
        if len(row['content']) <= cap:
            chosen.add(key)
            cap -= len(row['content'])
        else:
            observe('budget', key, reason='OUTPUT_BUDGET')
    return [row for row in rows if row['id'] in chosen]
