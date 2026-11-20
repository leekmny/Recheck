"""The durable updates used to reconstruct a porting result."""
from __future__ import annotations

from .porting_manifest import SIDES


def initial_rows(manifest):
    return [dict(id=case['id'], kind=case.get('kind', 'console'),
                 required=case.get('required', True), outcome='unexecuted',
                 acceptance='inconclusive' if case.get('required', True) else 'not_required',
                 attempts=[]) for case in manifest['scenarios']]


def _finalize(row, planned):
    attempts = row['attempts']
    matches = [attempt['comparison']['match'] for attempt in attempts]
    accepted = [attempt['comparison']['acceptance'] for attempt in attempts]
    if len(attempts) != planned or None in matches:
        row['outcome'] = 'unmeasured' if attempts else 'unexecuted'
    elif all(matches):
        row['outcome'] = 'matched'
    else:
        row['outcome'] = 'mismatched'
    row['acceptance'] = ('failed' if 'failed' in accepted else
                         'not_required' if not row['required'] else
                         'inconclusive' if len(attempts) != planned or 'inconclusive' in accepted else
                         'passed')


def apply_update(row, update):
    row.update(update.get('fields', {}))
    if 'index' not in update:
        return
    pending = row.setdefault('pending_attempt', dict(index=update['index'], observations={}))
    pending['observations'].update(update.get('observations', {}))
    if 'comparison' in update:
        row.pop('pending_attempt')
        # Preserve manifest side order even when the second worker returns first.
        observation = {side: pending['observations'][side] for side in SIDES}
        row['attempts'].append(dict(index=pending['index'], observations=observation,
                                    comparison=update['comparison']))
        _finalize(row, update['planned'])
