"""Read views of the canonical Finance dataset. No persistence or mutations."""
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from connection import CONTRACT, FinanceError

TZ = ZoneInfo('Pacific/Auckland')
EXCLUDED = {c['name'] for c in CONTRACT['categories'] if c['excluded']}
ONE_OFF = {c['name'] for c in CONTRACT['categories'] if c['oneOff']}
TX_FIELDS = ('id', 'date', 'amount', 'payee', 'description', 'category', 'source', 'excluded', 'importedAt')
PLAN_FIELDS = ('b1_float', 'b1_td6', 'b1_td12', 'b2_balance', 'b2_cash', 'b2_pending', 'b2_peak', 'b3_balance', 'b3_peak', 'ks_balance', 'conservative_balance', 'switch_pending', 'gentrack_shares', 'gentrack_price', 'property_kensington', 'property_nottingham', 'settlement_date', 'westpac_td_jun18', 'westpac_td_jun20', 'dvrp_net', 'stress_b2', 'stress_b3', 'birth_year', 'projection', 'nwIncludePrimary', 'nwIncludeFuture', 'manualUpdatedAt')
BUDGET_FIELDS = ('baselineDate', 'categoryAnnualForecast', 'categorySpread', 'categoryAccount', 'accountFortnightly', 'payAnchorDate', 'fortnightStart', 'fnFocusCats', 'allocationIgnore')


def today():
    return datetime.now(TZ).date().isoformat()


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def pick(obj, fields):
    return {key: obj[key] for key in fields if key in obj}


def metadata(dataset):
    state = dataset['state']
    saved = state.get('savedAt')
    dates = [t['date'] for t in state['transactions']]
    return {'source': 'Home Assistant Finance saved dataset', 'revision': dataset['revision'], 'read_at': datetime.now(timezone.utc).isoformat(), 'saved_at': datetime.fromtimestamp(saved / 1000, timezone.utc).isoformat() if isinstance(saved, (int, float)) else None, 'akahu_last_fetched_at': state.get('akahuLastFetch'), 'bank_refresh_last_requested_at': state.get('lastBackgroundRefresh'), 'manual_holdings_updated_at': state.get('manualUpdatedAt'), 'transaction_dates': {'first': min(dates) if dates else None, 'last': max(dates) if dates else None}, 'currency': 'NZD', 'timezone': 'Pacific/Auckland', 'freshness_note': 'Read now from saved records. Fetch/request times do not prove every bank has supplied fresh data. Manual holdings have separate update dates.'}


def bounds(start=None, end=None):
    for item in (start, end):
        if item is not None:
            try:
                if date.fromisoformat(item).isoformat() != item:
                    raise ValueError()
            except (ValueError, TypeError):
                raise FinanceError('Dates must use YYYY-MM-DD.') from None
    if start and end and start > end:
        raise FinanceError('The start date must not be after the end date.')


def scope_of(tx):
    if tx.get('excluded') or tx.get('category') in EXCLUDED:
        return 'excluded'
    return 'one_off' if tx.get('category') in ONE_OFF else 'core'


def transactions(dataset, start=None, end=None, query=None, category=None, source=None, scope='all', direction='all'):
    bounds(start, end)
    if scope not in ('all', 'core', 'one_off', 'excluded') or direction not in ('all', 'debit', 'credit'):
        raise FinanceError('Invalid transaction scope or direction.')
    rows = []
    for tx in dataset['state']['transactions']:
        if start and tx['date'] < start or end and tx['date'] > end:
            continue
        if category is not None and tx.get('category', '').casefold() != category.casefold():
            continue
        if source is not None and tx.get('source', '').casefold() != source.casefold():
            continue
        if query and query.casefold() not in ' '.join(str(tx.get(k, '')) for k in ('payee', 'description', 'category', 'source')).casefold():
            continue
        if scope != 'all' and scope_of(tx) != scope:
            continue
        if direction == 'debit' and tx['amount'] >= 0 or direction == 'credit' and tx['amount'] <= 0:
            continue
        rows.append(pick(tx, TX_FIELDS))
    return sorted(rows, key=lambda x: (x['date'], x['id']), reverse=True)


def spending(dataset, start, end, group_by='category', scope='core'):
    if group_by not in ('category', 'payee', 'source', 'month'):
        raise FinanceError('Group spending by category, payee, source, or month.')
    rows = transactions(dataset, start, end, scope=scope)
    groups = defaultdict(lambda: {'debits': Decimal(0), 'credits': Decimal(0), 'count': 0})
    for row in rows:
        key = row['date'][:7] if group_by == 'month' else row.get(group_by) or '(unspecified)'
        group = groups[key]
        value = Decimal(str(row['amount']))
        group['debits' if value < 0 else 'credits'] += abs(value)
        group['count'] += 1
    def render(key, group):
        return {'group': key, 'debit_spending': money(group['debits']), 'credits': money(group['credits']), 'net_outflow': money(group['debits'] - group['credits']), 'transaction_count': group['count']}
    totals = {'debits': sum((g['debits'] for g in groups.values()), Decimal(0)), 'credits': sum((g['credits'] for g in groups.values()), Decimal(0)), 'count': len(rows)}
    return {'period': {'start': start, 'end': end, 'inclusive': True}, 'scope': scope, 'totals': render('all selected transactions', totals), 'groups': sorted([render(k, v) for k, v in groups.items()], key=lambda x: (-x['debit_spending'], x['group'])), 'method': 'Raw cash amounts on transaction dates, without smoothing. Debit spending matches the app’s gross-spend convention; credits are reported separately and are not all necessarily refunds. Core excludes flagged transactions, excluded categories, and one-off categories.', 'excluded_categories': sorted(EXCLUDED), 'one_off_categories': sorted(ONE_OFF)}


def totals(state):
    """Finance 2.0.3 totalsFromState, including both legs of fund switches."""
    def n(key):
        return float(state.get(key) or 0)
    not_left = in_flight = 0
    switch = state.get('switch_pending')
    if switch:
        amount = float(switch.get('amount') or 0)
        if state.get(switch['from']) is None:
            not_left = amount
        else:
            left = min(amount, max(0, float(switch.get('fromBaseline') or 0) - n(switch['from'])))
            arrived = min(amount, max(0, n(switch['to']) - float(switch.get('toBaseline') or 0)))
            not_left, in_flight = max(0, amount - left), max(0, left - arrived)
    pending = state.get('b2_pending')
    pending_value = 0
    if pending:
        arrived = max(0, n('conservative_balance') + n('b2_balance') + n('b2_cash') - float(pending.get('baseline') or 0))
        pending_value = max(0, float(pending.get('amount') or 0) - arrived)
    reserve_cash_fund = sum(
        float(row.get('amount') or 0)
        for row in state.get('financeConnector', {}).get('reserve_transfers', {}).values()
        if row.get('destination') == 'Simplicity Cash Fund'
        and row.get('purpose') == 'Bucket 1 retirement reserve'
        and row.get('status') in ('pending_feed', 'confirmed')
    )
    reserve_cash_fund = min(reserve_cash_fund, n('b2_cash'))
    reserve_in_transit = sum(
        float(row.get('amount') or 0)
        for row in state.get('financeConnector', {}).get('reserve_transfers', {}).values()
        if row.get('destination') == 'Simplicity Cash Fund'
        and row.get('purpose') == 'Bucket 1 retirement reserve'
        and row.get('status') == 'in_transit'
    )
    reserve_in_transit_addition = sum(
        float(row.get('amount') or 0)
        for row in state.get('financeConnector', {}).get('reserve_transfers', {}).values()
        if row.get('destination') == 'Simplicity Cash Fund'
        and row.get('purpose') == 'Bucket 1 retirement reserve'
        and row.get('status') == 'in_transit'
        and not row.get('included_in_b1_float')
    )
    reserve_in_transit_addition = min(reserve_in_transit_addition, pending_value)
    b1 = n('b1_float') + n('b1_td6') + n('b1_td12') + reserve_cash_fund + reserve_in_transit_addition
    # Finance 2.0.13 removed Bucket 2: Conservative, Cash Fund and pending money now count in B3
    # alongside Balanced. The connector's Bucket 1 reserve adjustments still apply first.
    other_vehicles = max(0, n('conservative_balance') - not_left) + max(0, n('b2_cash') - reserve_cash_fund) + max(0, pending_value - reserve_in_transit_addition)
    balanced_pool = n('b3_balance') + n('b2_balance') + not_left + in_flight
    b2 = 0
    b3 = other_vehicles + balanced_pool
    ks = n('ks_balance')
    westpac = n('westpac_td_jun18') + n('westpac_td_jun20')
    gt = n('gentrack_shares') * n('gentrack_price')
    total = b1 + b2 + b3 + ks + westpac + n('dvrp_net') + gt + n('property_nottingham')
    return {k: money(v) for k, v in {'cash_bucket': b1, 'cash_bucket_simplicity_cash_fund': reserve_cash_fund, 'cash_bucket_in_transit': reserve_in_transit, 'bridge_bucket': b2, 'long_term_bucket': b3, 'long_term_balanced_pool': balanced_pool, 'long_term_conservative_cash_pending': other_vehicles, 'kiwisaver': ks, 'fund_switch_pending': not_left + in_flight, 'other_pending_funds': max(0, pending_value - reserve_in_transit_addition), 'westpac_term_deposits': westpac, 'expected_dvrp': n('dvrp_net'), 'gentrack_value': gt, 'nottingham_estimate': n('property_nottingham'), 'plan_total_excluding_primary_home': total, 'plan_total_excluding_kiwisaver_and_primary_home': total - ks}.items()}


def accounts(state):
    result = []
    for account in state.get('cachedAkahuAccounts', []):
        result.append({'name': account.get('name'), 'provider': (account.get('connection') or {}).get('name'), 'type': account.get('type'), 'currency': account.get('currency'), 'balance': pick(account.get('balance') or {}, ('current', 'available', 'overdrawn', 'currency')), 'refreshed': account.get('refreshed')})
    return {'saved_bank_accounts': result, 'spending_account_balances': state.get('acctActualBalance', {}), 'note': 'These are cached bank-reported balances. Plan holdings can differ because of manual values, unit pricing, or transfers in progress. Do not sum both representations.'}


from spending_groups import spending_groups, budget_view

def fortnight(dataset, as_of=None):
    state = dataset['state']
    as_of = as_of or today()
    anchor = state.get('payAnchorDate')
    if not anchor:
        raise FinanceError('Set a confirmed payday before reporting a fortnightly budget.')
    bounds(anchor, None)
    bounds(as_of, None)
    anchor_day, end_day = date.fromisoformat(anchor), date.fromisoformat(as_of)
    start_day = anchor_day + timedelta(days=((end_day - anchor_day).days // 14) * 14)
    start, end, next_payday = start_day.isoformat(), (start_day + timedelta(days=13)).isoformat(), (start_day + timedelta(days=14)).isoformat()
    rows = transactions(dataset, start, as_of, scope='core')
    ppy = {'fortnight': 26, 'month': 12, 'quarter': 4, 'year': 1}
    funding = state.get('accountFortnightly', {})
    account_rows = []
    for account in ('Living Well', 'Accruals'):
        outflow = sum((-Decimal(str(t['amount'])) for t in rows if t.get('source') == account and t['amount'] < 0), Decimal(0))
        credits = sum((Decimal(str(t['amount'])) for t in rows if t.get('source') == account and t['amount'] > 0), Decimal(0))
        budget = Decimal(str(funding.get(account) or 0))
        account_rows.append({'account': account, 'fortnightly_allocation': money(budget), 'core_debit_spending': money(outflow), 'core_category_credits': money(credits), 'allocation_less_core_debits': money(budget-outflow), 'recorded_bank_balance': state.get('acctActualBalance', {}).get(account), 'purpose': 'everyday spending allowance' if account == 'Living Well' else 'contribution to a pot for larger bills, not a cap on bills paid this fortnight'})
    category_rows = []
    for cat in CONTRACT['categories']:
        name = cat['name']
        if cat['excluded'] or cat['oneOff']:
            continue
        subset = [r for r in rows if r.get('category') == name]
        debits = sum((-Decimal(str(t['amount'])) for t in subset if t['amount'] < 0), Decimal(0))
        credits = sum((Decimal(str(t['amount'])) for t in subset if t['amount'] > 0), Decimal(0))
        configured = state.get('categoryAnnualForecast', {}).get(name)
        shape = {'amount': configured, 'period': 'year'} if isinstance(configured, (float, int)) else configured
        budget = None if not shape else Decimal(str(shape['amount'])) * ppy[shape.get('period', 'year')] / 26
        mapped = state.get('categoryAccount', {}).get(name)
        pot = mapped == 'Accruals' or bool(shape and shape.get('period', 'year') in ('year','quarter') and mapped in (None,'','Essentials'))
        if subset or shape is not None:
            category_rows.append({'category': name, 'core_debit_spending': money(debits), 'credits': money(credits), 'configured_budget': configured, 'fortnight_equivalent': money(budget) if budget is not None else None, 'budget_less_debits': money(budget-debits) if budget is not None and not pot else None, 'account': mapped, 'pot_funded': pot})
    reimbursements = []
    for group in state.get('financeConnector', {}).get('reimbursements', {}).values():
        owed = Decimal(str(group['expected_amount'])) - sum((Decimal(str(p['amount'])) for p in group['payments']), Decimal(0))
        if owed > 0:
            reimbursements.append({'expense_id': group['expense_id'], 'outstanding': money(owed), 'note': group.get('note', '')})
    active = state.get('fortnightStart')
    return {'period': {'start': start, 'end': end, 'next_payday': next_payday, 'as_of': as_of, 'day': (end_day-start_day).days+1, 'days_remaining_after_today': (start_day+timedelta(days=13)-end_day).days}, 'app_active_fortnight_start': active, 'app_rollover_needed': active != start, 'retirement_target_per_fortnight': money(CONTRACT['targets']['targetSpend']/26), 'configured_spending_plus_accruals': money(sum(Decimal(str(funding.get(k) or 0)) for k in ('Living Well','Accruals'))), 'accounts': account_rows, 'core_spending_all_accounts': spending(dataset,start,as_of)['totals'], 'spending_groups': spending_groups(rows), 'spending_budget_view': budget_view(state, rows, transactions(dataset, as_of[:7] + '-01', as_of, scope='core'), as_of), 'categories': sorted(category_rows,key=lambda r:-r['core_debit_spending']), 'one_off_spending': spending(dataset,start,as_of,scope='one_off')['totals'], 'tracked_reimbursements_outstanding': reimbursements, 'planning_context': state.get('financeConnector',{}).get('preferences',{}).get('planning_context'), 'notes': ['The budget comparison uses your saved spending allocation, not your full salary. Savings transfers and reimbursements are outside core lifestyle spending.', 'Allocation less core debits is a budget calculation, not cash available after upcoming bills. Outstanding reimbursements are not added to the bank balance.', 'Monthly budgets convert at 12/26 and annual budgets at 1/26. Accruals are a funding contribution; bills may be paid in a different fortnight.', 'The connector reports the pay-aligned fortnight for the selected date. The UI’s manual rollover is shown separately and is never advanced by a read.']}
