import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from state_store import StateStore, Conflict, clean_state


def fixture():
    return {'transactions': [{'id': 'a', 'date': '2026-09-01', 'amount': -10,
                              'payee': 'Example', 'category': 'Other'}],
            'snapshots': [], 'payeeOverrides': {'example': None}, 'b1_td6': 0}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(self.tmp.name)

    def test_migration_preserves_records_zero_and_recovery_copy(self):
        old = fixture(); old['akahuUserToken'] = 'test-only'
        migrated = self.store.migrate(old)
        self.assertEqual(migrated['state'], clean_state(old))
        self.assertEqual(migrated['revision'], 1)
        self.assertEqual(json.loads((self.store.backups / 'pre-simplification.json').read_text()), old)
        self.assertEqual(self.store.migrate({'invalid': True}), migrated)

    def test_stale_device_cannot_erase_transactions_or_resurrect_rule(self):
        current = self.store.migrate(fixture())
        stale = fixture(); stale['transactions'] = []; stale['payeeOverrides']['example'] = 'Groceries'
        with self.assertRaises(Conflict):
            self.store.save(0, stale, 'stale-device')
        self.assertEqual(self.store.read(), current)

    def test_two_simultaneous_devices_only_one_wins(self):
        self.store.migrate(fixture()); result = []
        def save(i):
            try:
                state = fixture(); state['b1_td6'] = i
                self.store.save(1, state, str(i)); result.append('saved')
            except Conflict:
                result.append('conflict')
        threads = [threading.Thread(target=save, args=(i,)) for i in (2, 3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(result, ['saved', 'conflict'])
        self.assertEqual(self.store.read()['revision'], 2)

    def test_response_loss_retry_is_idempotent(self):
        self.store.migrate(fixture())
        saved = self.store.save(1, fixture(), 'same-request')
        self.assertEqual(self.store.save(1, fixture(), 'same-request'), saved)

    def test_backup_not_overwritten_by_subsequent_saves(self):
        self.store.migrate(fixture())
        self.store.save(1, fixture(), 'one')
        backup = next(self.store.backups.glob('financial-plan-*.json'))
        original = backup.read_bytes()
        state = fixture(); state['transactions'] = []
        self.store.save(2, state, 'two')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.store.read()['state']['transactions'], [])

    def test_atomic_failure_preserves_previous_state(self):
        original = self.store.migrate(fixture())
        self.store.backup()
        with patch('state_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.save(1, fixture(), 'failed')
        self.assertEqual(self.store.read(), original)

    def test_corrupt_current_is_not_treated_as_empty(self):
        self.store.path.write_text('{bad')
        with self.assertRaises(ValueError): self.store.read()
        with self.assertRaises(ValueError): self.store.migrate(fixture())

    def test_malformed_state_rejected(self):
        for field, value in [('transactions', None), ('snapshots', {}), ('payeeOverrides', [])]:
            state = fixture(); state[field] = value
            with self.assertRaises(ValueError): self.store.save(0, state, 'bad')
        state = fixture(); state['transactions'][0]['amount'] = float('nan')
        with self.assertRaises(ValueError): self.store.save(0, state, 'bad')


spec = importlib.util.spec_from_file_location('finance_server', Path(__file__).with_name('akahu-proxy.py'))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ServerTests(unittest.TestCase):
    def test_old_client_is_read_only(self):
        class Request:
            path = '/backup'
            def _json(self, code, payload): self.result = code, payload
        request = Request()
        server.Handler.do_POST(request)
        self.assertEqual(request.result[0], 409)

    def test_snapshot_uses_current_bucket_roles_and_zero_funds(self):
        state = fixture()
        state.update(conservative_balance=100, b2_balance=200, b3_balance=0, ks_balance=300,
                     switch_pending=None, b2_pending=None)
        accounts = [{'connection': {'name': 'Simplicity'}, 'name': 'Balanced Fund',
                     'balance': {'current': 0}, 'meta': {'portfolio': [{'shares': 200, 'price': 1}]}}]
        with patch.object(server, '_latest_backup_state', return_value=state), patch.object(server, 'akahu_accounts', return_value=accounts):
            snapshot = server.build_snapshot()
        self.assertEqual(snapshot['b2'], 100)
        self.assertEqual(snapshot['b3'], 0)
        self.assertEqual(snapshot['ks'], 300)

    def test_switch_in_transit_preserves_total(self):
        state = fixture(); state.update(conservative_balance=0, b2_balance=0,
            switch_pending={'from':'conservative_balance', 'to':'b2_balance',
                            'fromBaseline':100, 'toBaseline':0, 'amount':100})
        with patch.object(server, '_latest_backup_state', return_value=state), patch.object(server, 'akahu_accounts', return_value=[]):
            snapshot = server.build_snapshot()
        self.assertEqual(snapshot['b2'], 0)
        self.assertEqual(snapshot['b3'], 100)


if __name__ == '__main__':
    unittest.main()
