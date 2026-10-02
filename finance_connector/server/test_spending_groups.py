import unittest
import json
from spending_groups import budget_view, spending_groups


class BudgetViewTests(unittest.TestCase):
    def state(self):
        return {'financeConnector': {'preferences': {'planning_context': 'SPENDING_POLICY=' + json.dumps({
            'version': 1, 'effective_date': '2026-09-13', 'personal_essential_monthly': 100,
            'subscriptions_status': 'provisional'})}},
            'accountFortnightly': {'Living Well': 1000, 'Accruals': 100},
            'categoryAnnualForecast': {
                'Personal': {'amount': 300, 'period': 'month'},
                'Eating out': {'amount': 300, 'period': 'fortnight'},
                'Wine': {'amount': 100, 'period': 'month'},
                'Books and records': {'amount': 200, 'period': 'month'},
                'Subscriptions': {'amount': 300, 'period': 'month'},
                'Travel': {'amount': 6500, 'period': 'year'},
                'Home & garden': {'amount': 5000, 'period': 'year'}}}

    def test_subscriptions_use_month_and_travel_never_pads_everyday(self):
        rows = [{'category': 'Eating out', 'amount': -100}, {'category': 'Subscriptions', 'amount': -190},
                {'category': 'Travel', 'amount': -1000}]
        month = rows + [{'category': 'Subscriptions', 'amount': -5.99}]
        r = budget_view(self.state(), rows, month, '2026-09-13')
        self.assertEqual(r['everyday_discretionary']['allowance'], 530.77)
        self.assertEqual(r['everyday_discretionary']['spent'], 100)
        self.assertEqual(r['subscriptions']['month_to_date_spent'], 195.99)
        self.assertEqual(r['all_discretionary_this_fortnight']['spent'], 1290)
        self.assertIsNone(r['reserved_provisions'][0]['available_pot_balance'])
        rec = r['funding_reconciliation']
        self.assertEqual(rec['annual_category_allowances'] + rec['unassigned_contingency_annual'], rec['annual_funding'])
        self.assertEqual(rec['unclassified_category_allowances_annual'], 0)

    def test_unknown_and_credits_not_silently_spare(self):
        rows = [{'category': 'Eating out', 'amount': -100}, {'category': 'Eating out', 'amount': 20},
                {'category': 'Personal', 'amount': -10}]
        r = budget_view(self.state(), rows, rows, '2026-09-13')['everyday_discretionary']
        self.assertEqual(r['credits'], 20)
        self.assertEqual(r['budget_remaining'], 430.77)
        self.assertEqual(r['remaining_after_unknown_spending'], 420.77)
        g = spending_groups(rows)['groups']
        self.assertEqual(sum(x['debit_spending'] for x in g), 110)

    def test_stale_policy_fails_closed(self):
        s = self.state()
        s['categoryAnnualForecast']['Personal']['amount'] = 50
        self.assertEqual(budget_view(s, [], [], '2026-09-13')['status'], 'invalid_policy')
        self.assertEqual(budget_view({}, [], [], '2026-09-13')['status'], 'not_configured')

    def policy_state(self, **overrides):
        s = self.state()
        policy = {'version': 1, 'effective_date': '2026-09-13',
                  'personal_essential_monthly': 100, 'subscriptions_status': 'provisional'}
        policy.update(overrides)
        s['financeConnector']['preferences']['planning_context'] = 'SPENDING_POLICY=' + json.dumps(policy)
        return s

    def test_version_2_policy_reads_identically_to_version_1(self):
        rows = [{'category': 'Eating out', 'amount': -100}]
        v1 = budget_view(self.policy_state(), rows, rows, '2026-09-13')
        v2 = budget_view(self.policy_state(version=2, effective_date='2026-09-24',
                                           subscriptions_confirmed_monthly=0), rows, rows, '2026-09-13')
        self.assertEqual(v2['status'], 'configured')
        self.assertEqual(v2['effective_date'], '2026-09-24')
        self.assertEqual(v2['everyday_discretionary'], v1['everyday_discretionary'])
        self.assertEqual(v2['funding_reconciliation'], v1['funding_reconciliation'])

    def test_unsupported_version_fails_closed_and_names_the_version(self):
        r = budget_view(self.policy_state(version=3), [], [], '2026-09-13')
        self.assertEqual(r['status'], 'invalid_policy')
        self.assertIn('3', r['message'])
        self.assertIn('1, 2', r['message'])


if __name__ == '__main__':
    unittest.main()
