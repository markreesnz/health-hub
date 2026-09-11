"""Revision-checked current state, independent of immutable recovery backups."""
import copy
import datetime
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1
RETIRED_KEYS = {'akahuAppToken', 'akahuUserToken', 'openLog', 'lastAutoBackup',
                'aiInsights', 'aiDismissed', 'aiFeedback', 'spendingInsights',
                'spendingDismissed', 'spendingFeedback', 'aiSpendInsights', 'aiSpendDismissed',
                'aiSpendFeedback', '_bannedTxIds', '_patches'}


def clean_state(state):
    state = copy.deepcopy(state)
    for key in list(state):
        if key in RETIRED_KEYS or key.startswith('_server'):
            state.pop(key, None)
    return state


def validate_state(state):
    if not isinstance(state, dict):
        raise ValueError('State must be an object')
    for key in ('transactions', 'snapshots'):
        if not isinstance(state.get(key), list):
            raise ValueError(f'{key} must be a list')
    if not isinstance(state.get('payeeOverrides'), dict):
        raise ValueError('payeeOverrides must be an object')
    for name in ('categoryAnnualForecast', 'categoryAccount', 'accountFortnightly',
                 'acctActualBalance', 'amortizationRules', 'migrations'):
        if name in state and not isinstance(state[name], dict):
            raise ValueError(f'{name} must be an object')
    for tx in state['transactions']:
        if not isinstance(tx, dict) or not isinstance(tx.get('id'), str) or not tx['id']:
            raise ValueError('Every transaction needs an id')
        if not isinstance(tx.get('amount'), (int, float)) or isinstance(tx['amount'], bool) or not math.isfinite(tx['amount']):
            raise ValueError('Every transaction needs a finite amount')
        datetime.date.fromisoformat(tx.get('date', ''))
    for snap in state['snapshots']:
        if not isinstance(snap, dict):
            raise ValueError('Invalid snapshot')
        datetime.date.fromisoformat(snap.get('date', ''))
    json.dumps(state, allow_nan=False)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Conflict(Exception):
    def __init__(self, current):
        self.current = current


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'current-state.json'
        self.backups = self.directory / 'backups'
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {'revision': 0, 'schemaVersion': SCHEMA_VERSION, 'state': None}
        # A corrupt current file is an error, never permission to initialise from defaults.
        with self.path.open() as f:
            current = json.load(f)
        validate_state(current['state'])
        if type(current.get('revision')) is not int or current['revision'] < 1:
            raise ValueError('Invalid current-state revision')
        return current

    def read(self):
        with self.lock:
            return self._read()

    def migrate(self, legacy):
        with self.lock:
            if self.path.exists():
                return self._read()
            if not legacy:
                return self._read()
            validate_state(legacy)
            archive = self.backups / 'pre-simplification.json'
            if not archive.exists():
                atomic_json(archive, legacy)
            state = clean_state(legacy)
            current = {'revision': 1, 'schemaVersion': SCHEMA_VERSION, 'state': state}
            atomic_json(self.path, current)
            return current

    def save(self, revision, state, mutation_id):
        validate_state(state)
        if type(revision) is not int or revision < 0:
            raise ValueError('A valid revision is required')
        if not isinstance(mutation_id, str) or not 1 <= len(mutation_id) <= 100:
            raise ValueError('A mutation id is required')
        with self.lock:
            current = self._read()
            if current.get('mutationId') == mutation_id:
                return current  # response lost after a successful write
            if revision != current['revision']:
                raise Conflict(current)
            self._daily_backup(current)
            state = clean_state(state)
            state['savedAt'] = int(time.time() * 1000)
            result = {'revision': revision + 1, 'schemaVersion': SCHEMA_VERSION,
                      'mutationId': mutation_id, 'state': state}
            atomic_json(self.path, result)
            return result

    def _daily_backup(self, current):
        if current['state'] is None:
            return
        date = datetime.date.today().isoformat()
        path = self.backups / f'financial-plan-{date}.json'
        if not path.exists():
            atomic_json(path, current['state'])

    def backup(self):
        with self.lock:
            self._daily_backup(self._read())
