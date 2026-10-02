"""Bounded Finance edits with previews, shared receipts and conflict-safe undo."""
import copy
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from connection import CONTRACT, FinanceError, atomic_json
from dataset import bounds, metadata, today

Id = Annotated[str, Field(min_length=1, max_length=300)]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Categorise(Operation):
    action: Literal['categorise_transaction']
    transaction_id: Id
    category: str
    excluded: bool | None = None
    learn_payee_rule: bool = False


class Reimburse(Operation):
    action: Literal['split_reimbursement']
    transaction_id: Id
    reimbursable_amount: Amount
    note: Annotated[str, Field(max_length=500)] = ''


class MatchReimbursement(Operation):
    action: Literal['match_reimbursement']
    expense_transaction_id: Id
    credit_transaction_id: Id
    amount: Amount


class CategoryBudget(Operation):
    action: Literal['set_category_budget']
    category: str
    amount: Amount
    period: Literal['fortnight', 'month', 'quarter', 'year'] = 'fortnight'
    account: str | None = None


class AccountBudget(Operation):
    action: Literal['set_account_budget']
    account: str
    amount: Amount


class PayCycle(Operation):
    action: Literal['set_pay_cycle']
    payday: str


class PlanningContext(Operation):
    action: Literal['set_planning_context']
    context: Annotated[str, Field(min_length=1, max_length=4000)]


class ReserveTransfer(Operation):
    action: Literal['record_reserve_transfer']
    amount: Amount
    source: Literal['BNZ Bucket 1']
    destination: Literal['Simplicity Cash Fund']
    purpose: Literal['Bucket 1 retirement reserve']
    reported_date: str


class ReserveTransferIntent(Operation):
    action: Literal['record_reserve_transfer_intent']
    amount: Amount
    source: Literal['BNZ Bucket 1']
    destination: Literal['Simplicity Cash Fund']
    purpose: Literal['Bucket 1 retirement reserve']
    reported_date: str


class ReserveTransferInTransit(Operation):
    action: Literal['mark_reserve_transfer_in_transit']
    amount: Amount
    source: Literal['BNZ Bucket 1']
    destination: Literal['Simplicity Cash Fund']
    purpose: Literal['Bucket 1 retirement reserve']
    reported_date: str


class AssignReserveTransitToB1(Operation):
    action: Literal['assign_reserve_transit_to_b1']
    reported_date: str


class Undo(Operation):
    action: Literal['undo_edit']
    request_id: str


EditOperation = Annotated[Union[Categorise, Reimburse, MatchReimbursement, CategoryBudget, AccountBudget, PayCycle, PlanningContext, ReserveTransfer, ReserveTransferIntent, ReserveTransferInTransit, AssignReserveTransitToB1, Undo], Field(discriminator='action')]
Operations = Annotated[list[EditOperation], Field(min_length=1, max_length=20)]
VALIDATOR = TypeAdapter(Operations)
CATEGORIES = {c['name'] for c in CONTRACT['categories']}
ACCOUNTS = {'Living Well', 'Accruals', 'Savings'}
NAMESPACE = 'financeConnector'


def currency(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount.quantize(Decimal('.01')) != amount:
        raise FinanceError('Amounts must be non-negative dollars with at most two decimal places.')
    return float(amount)


def edits(state):
    return state.get(NAMESPACE, {}).get('edits', {})


def value_at(state, path):
    value = state
    for part in path:
        if isinstance(value, list):
            matches = [item for item in value if isinstance(item, dict) and item.get('id') == part]
            if len(matches) != 1:
                return {'present': False}
            value = matches[0]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return {'present': False}
    return {'present': True, 'value': copy.deepcopy(value)}


def assign(state, path, entry):
    parent = state
    for part in path[:-1]:
        if isinstance(parent, list):
            matches = [row for row in parent if row.get('id') == part]
            if len(matches) != 1:
                raise FinanceError('A transaction changed or is missing.')
            parent = matches[0]
        else:
            parent = parent.setdefault(part, {})
    key = path[-1]
    if isinstance(parent, list):
        indices = [index for index, row in enumerate(parent) if row.get('id') == key]
        if len(indices) > 1:
            raise FinanceError('Duplicate transaction IDs need review before editing.')
        if entry['present']:
            if indices:
                parent[indices[0]] = copy.deepcopy(entry['value'])
            else:
                parent.append(copy.deepcopy(entry['value']))
        elif indices:
            parent.pop(indices[0])
    elif entry['present']:
        parent[key] = copy.deepcopy(entry['value'])
    else:
        parent.pop(key, None)


def plan(state, operations):
    """Validate operations and derive exact field changes without mutating input."""
    operations = VALIDATOR.validate_python(operations)
    output = copy.deepcopy(state)
    patches = []

    def set_value(path, value=None, present=True):
        before = value_at(output, path)
        after = {'present': True, 'value': copy.deepcopy(value)} if present else {'present': False}
        if before != after:
            patches.append({'path': path, 'before': before, 'after': after})
            assign(output, path, after)

    def tx(ident):
        rows = [t for t in output['transactions'] if t['id'] == ident]
        if len(rows) != 1:
            raise FinanceError('Select exactly one existing transaction ID before editing.')
        return rows[0]

    for op in operations:
        if isinstance(op, Categorise):
            if op.category not in CATEGORIES:
                raise FinanceError('Use a category that exists in the Finance app.')
            row = tx(op.transaction_id)
            # Linked reimbursements need their matching tools so receivable totals stay meaningful.
            linked_ids = set()
            for group in output.get(NAMESPACE, {}).get('reimbursements', {}).values():
                linked_ids.add(group['reimbursable_id'])
                linked_ids.update(payment['transaction_id'] for payment in group.get('payments', []))
            if row['id'] in linked_ids and (op.category != 'Reimbursable' or op.excluded is True):
                raise FinanceError('Undo or revise the linked reimbursement before changing its classification.')
            set_value(['transactions', row['id'], 'category'], op.category)
            if op.excluded is not None:
                set_value(['transactions', row['id'], 'excluded'], op.excluded)
            if op.learn_payee_rule:
                if op.category == 'Reimbursable':
                    raise FinanceError('Reimbursements are per-transaction; a permanent payee rule is inappropriate.')
                key = re.sub('[^a-z0-9]', '', row.get('payee', '').lower())[:40]
                if not key:
                    raise FinanceError('The selected transaction has no usable payee name.')
                set_value(['payeeOverrides', key], op.category)
        elif isinstance(op, Reimburse):
            row = copy.deepcopy(tx(op.transaction_id))
            amount = currency(op.reimbursable_amount)
            if row['amount'] >= 0 or not 0 < amount <= -row['amount']:
                raise FinanceError('A reimbursement must be positive and no larger than the selected expense.')
            if row.get('category') in ('Reimbursable', 'Transfer', 'Investing', 'Income'):
                raise FinanceError('Select the original personal/work expense to split.')
            group_path = [NAMESPACE, 'reimbursements', row['id']]
            if value_at(output, group_path)['present'] or any(t['id'] == row['id'] + '_reimb' for t in output['transactions']):
                raise FinanceError('This expense already has a reimbursement split. Undo its earlier split before replacing it.')
            if row.get('excluded'):
                raise FinanceError('This expense is individually excluded. Review its classification before splitting it.')
            remainder = float(Decimal(str(row['amount'])) + Decimal(str(amount)))
            if remainder == 0:
                reimb_id = row['id']
                set_value(['transactions', row['id'], 'category'], 'Reimbursable')
            else:
                reimb_id = row['id'] + '_reimb'
                reimb = {**row, 'id': reimb_id, 'amount': -amount, 'category': 'Reimbursable'}
                set_value(['transactions', row['id'], 'amount'], remainder)
                set_value(['transactions', reimb_id], reimb)
            set_value(group_path, {'expense_id': row['id'], 'reimbursable_id': reimb_id, 'original_amount': row['amount'], 'expected_amount': amount, 'note': op.note, 'payments': []})
        elif isinstance(op, MatchReimbursement):
            group_path = [NAMESPACE, 'reimbursements', op.expense_transaction_id]
            group_entry = value_at(output, group_path)
            if not group_entry['present']:
                raise FinanceError('That expense has no tracked reimbursement.')
            group = group_entry['value']
            row = copy.deepcopy(tx(op.credit_transaction_id))
            amount = currency(op.amount)
            outstanding = Decimal(str(group['expected_amount'])) - sum((Decimal(str(p['amount'])) for p in group['payments']), Decimal(0))
            if not 0 < amount <= row['amount'] or Decimal(str(amount)) > outstanding:
                raise FinanceError('The matched amount must fit the actual credit and the outstanding reimbursement.')
            for other in output.get(NAMESPACE, {}).get('reimbursements', {}).values():
                if any(p['transaction_id'] == row['id'] for p in other.get('payments', [])):
                    raise FinanceError('That credit is already matched to a reimbursement.')
            if row.get('excluded'):
                raise FinanceError('Review the excluded credit before matching it.')
            if row['amount'] == amount:
                credit_id = row['id']
                set_value(['transactions', credit_id, 'category'], 'Reimbursable')
            else:
                credit_id = row['id'] + '_reimb'
                if any(t['id'] == credit_id for t in output['transactions']):
                    raise FinanceError('That credit already has a reimbursement split.')
                set_value(['transactions', row['id'], 'amount'], float(Decimal(str(row['amount'])) - Decimal(str(amount))))
                set_value(['transactions', credit_id], {**row, 'id': credit_id, 'amount': amount, 'category': 'Reimbursable'})
            group['payments'].append({'transaction_id': credit_id, 'amount': amount, 'date': row['date']})
            set_value(group_path, group)
        elif isinstance(op, CategoryBudget):
            if op.category not in CATEGORIES or op.category in ('Transfer', 'Investing', 'Income', 'Reimbursable', 'Tax'):
                raise FinanceError('Choose an expense category for its budget.')
            if op.account is not None and op.account not in ACCOUNTS:
                raise FinanceError('Choose Living Well, Accruals or Savings as the category’s account.')
            set_value(['categoryAnnualForecast', op.category], {'amount': currency(op.amount), 'period': op.period})
            if op.account is not None:
                set_value(['categoryAccount', op.category], op.account)
        elif isinstance(op, AccountBudget):
            if op.account not in ACCOUNTS:
                raise FinanceError('Choose Living Well, Accruals or Savings for the fortnightly allocation.')
            set_value(['accountFortnightly', op.account], currency(op.amount))
        elif isinstance(op, PayCycle):
            bounds(op.payday, today())
            anchor = date.fromisoformat(op.payday)
            end = date.fromisoformat(today())
            start = anchor + timedelta(days=((end - anchor).days // 14) * 14)
            set_value(['payAnchorDate'], op.payday)
            set_value(['fortnightStart'], start.isoformat())
            set_value(['fnSeedVersion'], 3)
            salary_dates = [t['date'] for t in output['transactions'] if t['amount'] > 0 and not t.get('excluded') and t['date'] <= end.isoformat() and re.search('bnz salar', t.get('payee', '') + ' ' + t.get('description', ''), re.I)]
            if salary_dates:
                set_value(['lastRolloverSalaryDate'], max(salary_dates))
            set_value([NAMESPACE, 'preferences', 'budget_period'], 'fortnight')
        elif isinstance(op, PlanningContext):
            set_value([NAMESPACE, 'preferences', 'planning_context'], op.context)
        elif isinstance(op, ReserveTransfer):
            bounds(op.reported_date, op.reported_date)
            amount = currency(op.amount)
            if amount <= 0:
                raise FinanceError('A reserve transfer must be greater than zero.')
            source_before = currency(output.get('b1_float') or 0)
            destination_before = currency(output.get('b2_cash') or 0)
            if amount > source_before:
                raise FinanceError('The reserve transfer is larger than the saved Bucket 1 cash balance.')
            existing = value_at(output, [NAMESPACE, 'reserve_transfers', op.reported_date + '-bnz-to-simplicity-cash'])
            if existing['present']:
                raise FinanceError('That reported reserve transfer is already recorded. Undo or reconcile it before replacing it.')
            set_value(['b1_float'], currency(Decimal(str(source_before)) - Decimal(str(amount))))
            set_value(['b2_cash'], currency(Decimal(str(destination_before)) + Decimal(str(amount))))
            set_value(
                [NAMESPACE, 'reserve_transfers', op.reported_date + '-bnz-to-simplicity-cash'],
                {
                    'amount': amount,
                    'source': op.source,
                    'destination': op.destination,
                    'purpose': op.purpose,
                    'reported_date': op.reported_date,
                    'status': 'pending_feed',
                    'source_baseline': source_before,
                    'destination_baseline': destination_before,
                    'treatment': 'Internal reserve transfer; exclude from spending and retain in Bucket 1 reserve totals.',
                },
            )
        elif isinstance(op, ReserveTransferIntent):
            bounds(op.reported_date, op.reported_date)
            amount = currency(op.amount)
            if amount <= 0:
                raise FinanceError('A reserve transfer intent must be greater than zero.')
            key = op.reported_date + '-bnz-to-simplicity-cash'
            if value_at(output, [NAMESPACE, 'reserve_transfer_intents', key])['present']:
                raise FinanceError('That reserve transfer intent is already recorded.')
            set_value(
                [NAMESPACE, 'reserve_transfer_intents', key],
                {
                    'amount': amount,
                    'source': op.source,
                    'destination': op.destination,
                    'purpose': op.purpose,
                    'reported_date': op.reported_date,
                    'status': 'initiated_unconfirmed',
                    'treatment': 'Do not change holdings until connected feeds confirm the transfer. When confirmed, retain the amount in Bucket 1 reserve and exclude it from spending and investment performance.',
                },
            )
        elif isinstance(op, ReserveTransferInTransit):
            bounds(op.reported_date, op.reported_date)
            amount = currency(op.amount)
            if amount <= 0:
                raise FinanceError('An in-transit reserve transfer must be greater than zero.')
            key = op.reported_date + '-bnz-to-simplicity-cash'
            intent_path = [NAMESPACE, 'reserve_transfer_intents', key]
            intent_entry = value_at(output, intent_path)
            if not intent_entry['present']:
                raise FinanceError('Record the unconfirmed transfer intent before marking its departure.')
            if output.get('b2_pending'):
                raise FinanceError('Another Simplicity transfer is already pending; reconcile it before adding this one.')
            landed_baseline = float(
                Decimal(str(output.get('conservative_balance') or 0))
                + Decimal(str(output.get('b2_balance') or 0))
                + Decimal(str(output.get('b2_cash') or 0))
            )
            set_value(['b2_pending'], {'amount': amount, 'baseline': landed_baseline})
            transfer = {
                'amount': amount,
                'source': op.source,
                'destination': op.destination,
                'purpose': op.purpose,
                'reported_date': op.reported_date,
                'status': 'in_transit',
                'destination_baseline': float(output.get('b2_cash') or 0),
                'treatment': 'Departure confirmed by the BNZ balance; destination not yet reported. Include once in Bucket 1 reserve, exclude from spending and investment performance.',
            }
            set_value([NAMESPACE, 'reserve_transfers', key], transfer)
            intent = intent_entry['value']
            intent['status'] = 'departure_confirmed_in_transit'
            set_value(intent_path, intent)
        elif isinstance(op, AssignReserveTransitToB1):
            bounds(op.reported_date, op.reported_date)
            key = op.reported_date + '-bnz-to-simplicity-cash'
            transfer_path = [NAMESPACE, 'reserve_transfers', key]
            transfer_entry = value_at(output, transfer_path)
            if not transfer_entry['present'] or transfer_entry['value'].get('status') != 'in_transit':
                raise FinanceError('No matching in-transit reserve transfer was found.')
            transfer = transfer_entry['value']
            if transfer.get('included_in_b1_float'):
                raise FinanceError('That in-transit transfer is already assigned to Bucket 1.')
            pending = output.get('b2_pending') or {}
            if float(pending.get('amount') or 0) != float(transfer['amount']):
                raise FinanceError('The saved pending amount no longer matches the reserve transfer.')
            confirmed_bnz = currency(output.get('b1_float') or 0)
            aggregate = currency(Decimal(str(confirmed_bnz)) + Decimal(str(transfer['amount'])))
            set_value(['b1_float'], aggregate)
            set_value(['b2_pending'], None)
            transfer['included_in_b1_float'] = True
            transfer['confirmed_bnz_balance'] = confirmed_bnz
            transfer['treatment'] = 'Included once in the Bucket 1 total while in transit. Exclude from spending and investment performance; reconcile against the Simplicity Cash Fund feed on arrival.'
            set_value(transfer_path, transfer)
            set_value([NAMESPACE, 'reserve_vehicle_breakdown'], {
                'bnz_bucket_1_confirmed': confirmed_bnz,
                'simplicity_cash_fund_confirmed': float(output.get('b2_cash') or 0),
                'simplicity_cash_fund_in_transit': float(transfer['amount']),
                'bucket_1_liquid_total': aggregate,
            })
        elif isinstance(op, Undo):
            receipt = edits(output).get(op.request_id)
            if not receipt:
                raise FinanceError('That edit receipt was not found.')
            # Reverse in reverse order so a batch which touched one field twice is reversible.
            for patch in reversed(receipt['changes']):
                if value_at(output, patch['path']) != patch['after']:
                    raise FinanceError('A field changed since that edit. Undo cannot overwrite newer changes; create a targeted correction instead.')
                entry = patch['before']
                set_value(patch['path'], entry.get('value'), entry['present'])
    return output, patches, [op.model_dump() for op in operations]


def preview_dir(connection):
    path = Path(connection.config['jobs_dir']).parent / 'edit-previews'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def preview(connection, operations):
    connection.check_contract()
    data = connection.dataset()
    _, changes, normalised = plan(data['state'], operations)
    if not changes:
        return {'status': 'no_change', 'metadata': metadata(data), 'message': 'The requested settings already match the saved data.'}
    ident = uuid.uuid4().hex
    draft = {'preview_id': ident, 'base_revision': data['revision'], 'operations': normalised, 'changes': changes, 'created_at': datetime.now(timezone.utc).isoformat()}
    atomic_json(preview_dir(connection) / (ident + '.json'), draft)
    return {**draft, 'status': 'preview', 'message': 'No changes saved yet. Apply this preview only for changes the user requested. A concurrent save invalidates this preview.', 'budget_note': 'Category allowances and account allocations are separate settings. Changing a budget does not transfer money.'}


def apply(connection, preview_id, request_id):
    if not re.fullmatch('[a-f0-9]{32}', preview_id) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', request_id):
        raise FinanceError('Use a valid preview ID and an 8–80 character request ID. Reuse both after an uncertain response.')
    path = preview_dir(connection) / (preview_id + '.json')
    if not path.exists():
        raise FinanceError('That preview was not found. Read the data and create a fresh preview.')
    draft = json.loads(path.read_text())
    data = connection.dataset()
    existing = edits(data['state']).get(request_id)
    if existing:
        if existing['preview_id'] != preview_id:
            raise FinanceError('That request ID belongs to a different edit.')
        return {'status': 'already_applied', 'receipt': existing, 'metadata': metadata(data)}
    if data['revision'] != draft['base_revision']:
        raise FinanceError('The saved dataset changed after this preview. No edit applied. Read the current data and create a fresh preview.')
    connection.check_contract()
    state, changes, operations = plan(data['state'], draft['operations'])
    if changes != draft['changes']:
        raise FinanceError('The edit preview no longer matches. Create a new preview.')
    receipt = {'request_id': request_id, 'preview_id': preview_id, 'applied_at': datetime.now(timezone.utc).isoformat(), 'base_revision': data['revision'], 'saved_revision': data['revision'] + 1, 'operations': operations, 'changes': changes}
    state.setdefault(NAMESPACE, {}).setdefault('edits', {})[request_id] = receipt
    mutation_id = 'finance-edit-' + hashlib.sha256(request_id.encode()).hexdigest()
    result = connection.save_state(data['revision'], state, mutation_id)
    if edits(result.get('state', {})).get(request_id) != receipt:
        raise FinanceError('The save receipt was not returned. Retry the same preview and request ID to verify the outcome.')
    for patch in changes:
        # Check final values only when the same field occurs multiple times in a batch.
        if value_at(result['state'], patch['path']) != value_at(state, patch['path']):
            raise FinanceError('The server changed an edited value during validation. Read current records before any further edits.')
    return {'status': 'applied', 'receipt': receipt, 'metadata': metadata(result), 'message': 'Saved in the same dataset used by the Finance app. Reload the app to see the changes.'}
