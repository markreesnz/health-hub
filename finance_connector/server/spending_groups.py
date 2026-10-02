"""Reporting groups for ordinary spending; never alter bank categories or balances."""
from decimal import Decimal
import json

ESSENTIAL = {'Groceries', 'Family payments', 'Power', 'Internet', 'Internet & phone',
             'Fuel', 'Transport', 'Health', 'Insurance', 'Body corporate', 'Banking fees',
             'Rates — Nottingham', 'Rates — Kensington', 'Water — Nottingham', 'Water — Kensington'}
DISCRETIONARY = {'Eating out', 'Wine', 'Travel', 'Books and records', 'Subscriptions'}
# Policy schema versions this reader understands. Version 2 added documentary fields
# (subscriptions_confirmed_monthly, richer subscriptions_status) that do not change any
# calculation here, so it reads identically to version 1.
SUPPORTED_POLICY_VERSIONS = (1, 2)
# Purposes confirmed 13 September; reporting defaults, not merchant-wide rules.
CONFIRMED = {
    'akahu_trans_cmtuu5qt51jie02kwcd6id0kb': ('Subscriptions', 'discretionary'),
    'akahu_trans_cmtw1sck713jx02juhtaj39ng': ('Subscriptions', 'discretionary'),
    'akahu_trans_cmtz1fg7g22mn02ih2cawa3vz': ('Personal', 'essential_and_committed'),
    'akahu_trans_cmtuu5qt41ji102kwh3tuapbw': ('Personal', 'discretionary'),
}


def classify(row):
    category = row.get('category')
    group = ('essential_and_committed' if category in ESSENTIAL else
             'discretionary' if category in DISCRETIONARY else 'needs_classification')
    if category == 'Groceries' and 'book' in str(row.get('payee', '')).casefold():
        group = 'needs_classification'
    confirmed = CONFIRMED.get(row.get('id'))
    if confirmed and category == confirmed[0]:
        group = confirmed[1]
    return group


def spending_groups(rows):
    groups = {name: {'debits': Decimal(0), 'credits': Decimal(0), 'count': 0}
              for name in ('essential_and_committed', 'discretionary', 'needs_classification')}
    unclear = []
    for row in rows:
        group = classify(row)
        amount = Decimal(str(row['amount']))
        groups[group]['debits'] += max(-amount, Decimal(0))
        groups[group]['credits'] += max(amount, Decimal(0))
        groups[group]['count'] += 1
        if group == 'needs_classification':
            unclear.append({k: row[k] for k in ('id', 'date', 'payee', 'category', 'amount') if k in row})
    return {'groups': [{'group': name, 'debit_spending': float(g['debits']),
                        'credits': float(g['credits']), 'transaction_count': g['count']}
                       for name, g in groups.items()],
            'needs_classification': unclear,
            'method': 'All core transactions partitioned once using saved categories. Mixed or conflicting purposes stay unclassified. Family support is committed. Credits are separate; no group spending limits or available-cash amounts are inferred.'}


def budget_view(state, rows, month_rows, as_of):
    """Separate flexible allowance, calendar-month bills and reserved future spending."""
    context = state.get('financeConnector', {}).get('preferences', {}).get('planning_context', '')
    if 'SPENDING_POLICY=' not in context:
        return {'status': 'not_configured'}
    try:
        policy = json.JSONDecoder().raw_decode(context.split('SPENDING_POLICY=', 1)[1])[0]
        version = policy.get('version')
        if version not in SUPPORTED_POLICY_VERSIONS:
            return {'status': 'invalid_policy',
                    'message': 'Saved spending policy version %s is not supported by this connector (supported: %s). '
                               'Update the connector or the saved policy version.'
                               % (version, ', '.join(str(v) for v in SUPPORTED_POLICY_VERSIONS))}
        personal_essential = Decimal(str(policy['personal_essential_monthly'])) * 12
    except (ValueError, KeyError, TypeError):
        return {'status': 'invalid_policy', 'message': 'Review saved spending policy before using group limits.'}
    annual = {}
    for category, shape in state.get('categoryAnnualForecast', {}).items():
        if isinstance(shape, (int, float)):
            annual[category] = Decimal(str(shape))
        else:
            annual[category] = Decimal(str(shape['amount'])) * {'year': 1, 'quarter': 4, 'month': 12, 'fortnight': 26}[shape.get('period', 'year')]
    personal = annual.get('Personal', Decimal(0))
    if not 0 <= personal_essential <= personal:
        return {'status': 'invalid_policy', 'message': 'Essential personal provision exceeds the Personal category allowance.'}
    def money(n):
        return float(n.quantize(Decimal('.01')))
    def sums(items):
        return (sum((max(-Decimal(str(t['amount'])), Decimal(0)) for t in items), Decimal(0)),
                sum((max(Decimal(str(t['amount'])), Decimal(0)) for t in items), Decimal(0)))
    daily_rows = [r for r in rows if classify(r) == 'discretionary' and r.get('category') not in ('Subscriptions', 'Travel')]
    spent, credits = sums(daily_rows)
    allowance = (sum((annual.get(c, Decimal(0)) for c in ('Eating out', 'Wine', 'Books and records')), Decimal(0)) + personal - personal_essential) / 26
    unknown, _ = sums([r for r in rows if classify(r) == 'needs_classification'])
    subscriptions, subscription_credits = sums([r for r in month_rows if r.get('category') == 'Subscriptions'])
    sub_allowance = annual.get('Subscriptions', Decimal(0)) / 12
    full_spend, full_credits = sums([r for r in rows if classify(r) == 'discretionary'])
    essentials_annual = sum((v for c, v in annual.items() if c in ESSENTIAL), Decimal(0)) + personal_essential
    assigned = sum(annual.values(), Decimal(0))
    funding = sum((Decimal(str(state.get('accountFortnightly', {}).get(k, 0))) for k in ('Living Well', 'Accruals')), Decimal(0))
    provisions = []
    for category in ('Travel', 'Home & garden'):
        paid, refunded = sums([r for r in rows if r.get('category') == category])
        provisions.append({'category': category, 'annual_allowance': money(annual.get(category, Decimal(0))),
                           'fortnightly_provision': money(annual.get(category, Decimal(0)) / 26),
                           'fortnight_spending': money(paid), 'credits': money(refunded),
                           'available_pot_balance': None})
    classified_budget = essentials_annual + allowance * 26 + annual.get('Subscriptions', Decimal(0)) + sum((annual.get(c, Decimal(0)) for c in ('Travel', 'Home & garden')), Decimal(0))
    return {'status': 'configured', 'effective_date': policy['effective_date'],
            'everyday_discretionary': {'allowance': money(allowance), 'spent': money(spent), 'credits': money(credits),
                'budget_remaining': money(allowance - spent), 'needs_classification_spending': money(unknown),
                'remaining_after_unknown_spending': money(allowance - spent - unknown),
                'included': ['Eating out', 'Wine', 'Books and records', 'Discretionary part of Personal'],
                'personal_essential_monthly': money(personal_essential / 12), 'personal_discretionary_monthly': money((personal - personal_essential) / 12)},
            'all_discretionary_this_fortnight': {'spent': money(full_spend), 'credits': money(full_credits), 'includes_subscriptions_and_travel': True},
            'subscriptions': {'period_start': as_of[:7] + '-01', 'as_of': as_of, 'monthly_allowance': money(sub_allowance),
                'month_to_date_spent': money(subscriptions), 'credits': money(subscription_credits),
                'budget_remaining': money(sub_allowance - subscriptions), 'status': policy['subscriptions_status']},
            'reserved_provisions': provisions,
            'funding_reconciliation': {'combined_fortnightly_funding': money(funding), 'annual_funding': money(funding * 26),
                'annual_category_allowances': money(assigned), 'unassigned_contingency_annual': money(funding * 26 - assigned),
                'unassigned_contingency_fortnightly': money(funding - assigned / 26), 'essential_and_committed_annual_allowance': money(essentials_annual),
                'unclassified_category_allowances_annual': money(assigned - classified_budget)},
            'note': 'Provisions and unassigned contingency are not spare discretionary cash. Pot balances are unknown, not zero. Credits are reported separately. Unknown spending reduces conservative everyday headroom. Policy starts mid-fortnight; do not judge earlier purchases against a newly introduced cap.'}
