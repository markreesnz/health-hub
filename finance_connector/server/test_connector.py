import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from connection import Connection, FinanceError, NoRedirect, atomic_json
from dataset import accounts, bounds, spending, totals, transactions
import refresh

_CONFIG_DIR = tempfile.TemporaryDirectory()


def setUpModule():
    path = Path(_CONFIG_DIR.name) / 'connection.json'
    path.write_text(json.dumps({'finance_url': 'http://finance.internal:8765', 'jobs_dir': str(Path(_CONFIG_DIR.name) / 'jobs')}))
    import os
    os.environ['FINANCE_CONFIG'] = str(path)


def fixture():
    rows = [
        {'id': 'a', 'date': '2026-09-01', 'amount': -0.1, 'payee': 'Cafe', 'category': 'Eating out', 'source': 'Living Well'},
        {'id': 'b', 'date': '2026-09-02', 'amount': -0.2, 'payee': 'Cafe', 'category': 'Eating out', 'source': 'Living Well'},
        {'id': 'c', 'date': '2026-09-03', 'amount': 0.05, 'payee': 'Cafe refund', 'category': 'Eating out'},
        {'id': 'd', 'date': '2026-09-04', 'amount': -1000, 'category': 'Transfer'},
        {'id': 'e', 'date': '2026-09-05', 'amount': -100, 'category': 'Vehicle'},
        {'id': 'f', 'date': '2026-09-06', 'amount': -10, 'category': 'Groceries', 'excluded': True},
        {'id': 'g', 'date': '2026-08-31', 'amount': -20, 'category': 'Groceries'},
    ]
    return {'schemaVersion': 1, 'revision': 3, 'state': {'transactions': rows, 'snapshots': [], 'payeeOverrides': {}}}


class DataTests(unittest.TestCase):
    def test_spend_exclusions_and_refunds(self):
        data = fixture()
        original = copy.deepcopy(data)
        result = spending(data, '2026-09-01', '2026-09-06')
        self.assertEqual(result['totals']['debit_spending'], 0.3)
        self.assertEqual(result['totals']['credits'], 0.05)
        self.assertEqual(result['totals']['net_outflow'], 0.25)
        self.assertEqual(data, original)
        self.assertEqual(spending(data, '2026-09-01', '2026-09-06', scope='all')['totals']['debit_spending'], 1110.3)

    def test_filter_date_direction_and_query(self):
        self.assertEqual([t['id'] for t in transactions(fixture(), '2026-09-02', '2026-09-03', query='CAFE', direction='debit')], ['b'])
        self.assertEqual([t['id'] for t in transactions(fixture(), scope='one_off')], ['e'])
        self.assertEqual([t['id'] for t in transactions(fixture(), scope='excluded')], ['f', 'd'])

    def test_calendar_dates(self):
        for a, b in [('2026-02-30', None), ('2026-09-02', '2026-09-01'), ('20260901', None)]:
            with self.assertRaises(FinanceError):
                bounds(a, b)

    def test_switch_has_no_double_count(self):
        state = {'b1_float': 10, 'conservative_balance': 1000, 'b2_balance': 0, 'switch_pending': {'from': 'conservative_balance', 'to': 'b2_balance', 'amount': 600, 'fromBaseline': 1000, 'toBaseline': 0}}
        for source, dest in [(1000, 0), (400, 0), (400, 300), (400, 600)]:
            state.update(conservative_balance=source, b2_balance=dest)
            value = totals(state)
            self.assertEqual(value['bridge_bucket'], 0)
            self.assertEqual(value['long_term_bucket'], 1000)
            self.assertEqual(value['long_term_conservative_cash_pending'], 400)
            self.assertEqual(value['long_term_balanced_pool'], 600)
            self.assertEqual(value['plan_total_excluding_primary_home'], 1010)

    def test_pending_and_expected_holdings(self):
        state = {'conservative_balance': 200, 'b2_pending': {'amount': 50, 'baseline': 180}, 'property_nottingham': 100, 'property_kensington': 1000, 'dvrp_net': 20, 'ks_balance': 10}
        self.assertEqual(totals(state)['other_pending_funds'], 30)
        self.assertEqual(totals(state)['plan_total_excluding_primary_home'], 360)

    def test_bucket2_removed_matches_app_2_0_13(self):
        # Mirrors the app's own 2.0.13 test: B2 is zero and everything outside KiwiSaver is B3.
        value = totals({'conservative_balance': 0, 'b2_balance': 4000000, 'ks_balance': 500000})
        self.assertEqual((value['bridge_bucket'], value['long_term_bucket'], value['kiwisaver']), (0, 4000000, 500000))
        value = totals({'conservative_balance': 300000, 'b2_balance': 3700000, 'b2_cash': 10000})
        self.assertEqual((value['bridge_bucket'], value['long_term_bucket'], value['long_term_conservative_cash_pending']), (0, 4010000, 310000))

    def test_account_identifiers_omitted(self):
        data = accounts({'cachedAkahuAccounts': [{'_id': 'private', 'account_number': 'private', 'name': 'Test', 'balance': {'current': 1, 'currency': 'NZD'}, 'credentials': 'secret'}]})
        self.assertNotIn('private', json.dumps(data))
        self.assertNotIn('secret', json.dumps(data))


class ConnectionTests(unittest.TestCase):
    def test_paths_and_redirects(self):
        c = Connection()
        with self.assertRaises(FinanceError):
            c.read('/refresh')
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_missing_state_not_defaulted(self):
        c = Connection()
        with patch.object(c, 'read', return_value={'schemaVersion': 1, 'revision': 0, 'state': None}):
            with self.assertRaises(FinanceError):
                c.dataset()

    def test_changed_app_blocks_derived_tools(self):
        c = Connection()
        with patch.object(c, 'read', return_value='changed app'):
            with self.assertRaises(FinanceError):
                c.check_contract()


class DirectModeTests(unittest.TestCase):
    def test_direct_mode_has_no_cookie_or_credential(self):
        c = Connection()
        self.assertTrue(c.direct)
        session = c.session()
        self.assertEqual(session['url'], 'http://finance.internal:8765')
        self.assertNotIn('Cookie', c._headers(session, {'Accept': 'application/json'}))

    def test_direct_mode_rejects_external_or_path_urls(self):
        import os
        for bad in ('https://finance.example', 'http://finance.internal:8765/x', 'http://user:pw@finance.internal'):
            path = Path(_CONFIG_DIR.name) / 'bad.json'
            path.write_text(json.dumps({'finance_url': bad, 'jobs_dir': '/tmp/x'}))
            with self.assertRaises(FinanceError):
                Connection(str(path))

    def test_planning_names(self):
        import server
        for bad in ('../x.json', 'x.txt', 'X.json', '.hidden.json', 'a/b.json', '', 'x.json.previous'):
            with self.assertRaises(FinanceError):
                server.planning_path(bad)
        self.assertEqual(server.planning_path('planned-payments.json').name, 'planned-payments.json')


class RefreshTests(unittest.TestCase):
    def test_reconcile_bucket1_reserve_keeps_transit_in_b1(self):
        state={'b1_float':15000,'cachedAkahuAccounts':[
            {'connection':{'name':'BNZ'},'name':'Bucket 1','balance':{'current':15000}},
            {'connection':{'name':'Simplicity'},'name':'Cash Fund','balance':{'current':0}}],
            'financeConnector':{'reserve_transfers':{'x':{'amount':35000,'destination':'Simplicity Cash Fund','purpose':'Bucket 1 retirement reserve','included_in_b1_float':True,'status':'in_transit','destination_baseline':0}}}}
        self.assertTrue(refresh.reconcile_bucket1_reserve(state))
        self.assertEqual(state['b1_float'],50000)
        self.assertEqual(state['financeConnector']['reserve_vehicle_breakdown']['simplicity_cash_fund_in_transit'],35000)
        state['cachedAkahuAccounts'][1]['balance']['current']=35000
        self.assertTrue(refresh.reconcile_bucket1_reserve(state))
        self.assertEqual(state['b1_float'],50000)
        self.assertEqual(state['financeConnector']['reserve_transfers']['x']['status'],'confirmed')

    def test_idempotency_and_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            c = Connection()
            c.config['jobs_dir'] = directory
            with patch.object(c, 'check_contract'), patch('refresh.subprocess.Popen') as popen:
                first = refresh.start(c, 'request-one')
                retry = refresh.start(c, 'request-one')
                overlapping = refresh.start(c, 'request-two')
                self.assertEqual(first['job_id'], retry['job_id'])
                self.assertEqual(first['job_id'], overlapping['job_id'])
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.call_args.kwargs['stdout'], refresh.subprocess.DEVNULL)

    def test_stale_job_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            c = Connection()
            c.config['jobs_dir'] = directory
            ident = 'a' * 32
            atomic_json(Path(directory) / (ident + '.json'), {'job_id': ident, 'status': 'running', 'started_epoch': time.time() - 400})
            self.assertEqual(refresh.status(c, ident)['status'], 'interrupted')
            with self.assertRaises(FinanceError):
                refresh.status(c, '../../token')


if __name__ == '__main__':
    unittest.main()
