import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import editing
from connection import FinanceError
from dataset import fortnight

class Fake:
    def __init__(self, directory):
        self.config = {'jobs_dir': str(Path(directory)/'jobs')}
        self.data = {'schemaVersion':1,'revision':1,'state': fixture()}
        self.saves = 0
    def check_contract(self): pass
    def dataset(self): return copy.deepcopy(self.data)
    def save_state(self, revision, state, mutation_id):
        if revision != self.data['revision']: raise FinanceError('Conflict')
        self.saves += 1
        self.data = {'schemaVersion':1,'revision':revision+1,'state':copy.deepcopy(state),'mutationId':mutation_id}
        return self.dataset()

def fixture():
    return {'transactions':[
        {'id':'dinner','date':'2026-09-10','amount':-824.18,'category':'Eating out','source':'Living Well','payee':'Dinner'},
        {'id':'credit','date':'2026-09-11','amount':650,'category':'Other','source':'Living Well'},
        {'id':'salary','date':'2026-09-09','amount':11222.36,'category':'Income','payee':'BNZ Salaries'},
        {'id':'reno','date':'2026-09-10','amount':-2000,'category':'Renovation'},
        {'id':'saving','date':'2026-09-10','amount':-5700,'category':'Transfer'}],
        'snapshots':[], 'payeeOverrides':{}, 'payAnchorDate':'2026-05-06', 'fortnightStart':'2026-09-09',
        'accountFortnightly':{'Living Well':5038.68,'Accruals':464},
        'categoryAnnualForecast':{'Personal':{'amount':400,'period':'month'}}, 'b1_float':50000, 'b1_td6':125000, 'b1_td12':125000, 'b2_cash':0,
        'acctActualBalance':{'Living Well':2589}}

def split(): return {'action':'split_reimbursement','transaction_id':'dinner','reimbursable_amount':600.0}
def total(state): return sum((Decimal(str(t['amount'])) for t in state['transactions']), Decimal(0))

class EditingTests(unittest.TestCase):
    def test_split_preserves_cash_and_input(self):
        original = fixture(); saved = copy.deepcopy(original)
        state, changes, _ = editing.plan(original,[split()])
        self.assertEqual(original,saved)
        self.assertEqual(total(state),total(original))
        self.assertEqual(state['transactions'][0]['amount'],-224.18)
        self.assertEqual(state['transactions'][-1]['amount'],-600)
        self.assertEqual(state['acctActualBalance'],original['acctActualBalance'])
        with self.assertRaises(FinanceError): editing.plan(state,[split()])
        with self.assertRaises(FinanceError): editing.plan(original,[{**split(),'reimbursable_amount':825.0}])
    def test_full_split_and_invalid_precision(self):
        state,_,_ = editing.plan(fixture(),[{**split(),'reimbursable_amount':824.18}])
        self.assertEqual(len(state['transactions']),5)
        self.assertEqual(state['transactions'][0]['category'],'Reimbursable')
        with self.assertRaises(FinanceError): editing.plan(fixture(),[{**split(),'reimbursable_amount':0.001}])
    def test_match_partial_credit_conserves_cash(self):
        state,_,_=editing.plan(fixture(),[split()])
        state,_,_=editing.plan(state,[{'action':'match_reimbursement','expense_transaction_id':'dinner','credit_transaction_id':'credit','amount':600.0}])
        self.assertEqual(total(state),total(fixture()))
        self.assertEqual(fortnight({'state':state},'2026-09-11')['tracked_reimbursements_outstanding'],[])
        with self.assertRaises(FinanceError): editing.plan(state,[{'action':'match_reimbursement','expense_transaction_id':'dinner','credit_transaction_id':'credit_reimb','amount':1.0}])
    def test_fortnight_exclusions_and_conversion(self):
        state,_,_=editing.plan(fixture(),[split()])
        result=fortnight({'state':state},'2026-09-11')
        self.assertEqual(result['period']['start'],'2026-09-09')
        self.assertEqual(result['period']['end'],'2026-09-22')
        self.assertEqual(result['core_spending_all_accounts']['debit_spending'],224.18)
        self.assertEqual(result['one_off_spending']['debit_spending'],2000)
        self.assertEqual(result['tracked_reimbursements_outstanding'][0]['outstanding'],600)
        self.assertEqual(next(r for r in result['categories'] if r['category']=='Personal')['fortnight_equivalent'],184.62)
        self.assertEqual(fortnight({'state':state},'2026-09-23')['period']['start'],'2026-09-23')
        self.assertEqual(fortnight({'state':state},'2026-09-08')['period']['start'],'2026-08-26')
    def test_pay_cycle_preserves_allocations(self):
        with patch('editing.today',return_value='2026-09-11'):
            state,_,_=editing.plan(fixture(),[{'action':'set_pay_cycle','payday':'2026-09-09'}])
        self.assertEqual(state['accountFortnightly'],fixture()['accountFortnightly'])
        self.assertEqual(state['lastRolloverSalaryDate'],'2026-09-09')
        self.assertEqual(state['payAnchorDate'],'2026-09-09')
    def test_reserve_transfer_preserves_total_and_purpose(self):
        original=fixture()
        op={'action':'record_reserve_transfer','amount':35000.0,'source':'BNZ Bucket 1','destination':'Simplicity Cash Fund','purpose':'Bucket 1 retirement reserve','reported_date':'2026-09-17'}
        state,_,_=editing.plan(original,[op])
        self.assertEqual(state['b1_float'],15000)
        self.assertEqual(state['b2_cash'],35000)
        self.assertEqual(state['financeConnector']['reserve_transfers']['2026-09-17-bnz-to-simplicity-cash']['status'],'pending_feed')
        from dataset import totals
        before=totals(original); after=totals(state)
        self.assertEqual(after['cash_bucket'],before['cash_bucket'])
        self.assertEqual(after['bridge_bucket'],before['bridge_bucket'])
        self.assertEqual(after['plan_total_excluding_primary_home'],before['plan_total_excluding_primary_home'])
    def test_reserve_transfer_intent_does_not_change_holdings(self):
        original=fixture()
        op={'action':'record_reserve_transfer_intent','amount':35000.0,'source':'BNZ Bucket 1','destination':'Simplicity Cash Fund','purpose':'Bucket 1 retirement reserve','reported_date':'2026-09-17'}
        state,_,_=editing.plan(original,[op])
        self.assertEqual(state['b1_float'],original['b1_float'])
        self.assertEqual(state['b2_cash'],original['b2_cash'])
        self.assertEqual(state['financeConnector']['reserve_transfer_intents']['2026-09-17-bnz-to-simplicity-cash']['status'],'initiated_unconfirmed')
    def test_departed_reserve_transfer_is_in_transit_without_double_counting(self):
        original=fixture(); original['b1_float']=15000
        intent={'action':'record_reserve_transfer_intent','amount':35000.0,'source':'BNZ Bucket 1','destination':'Simplicity Cash Fund','purpose':'Bucket 1 retirement reserve','reported_date':'2026-09-17'}
        state,_,_=editing.plan(original,[intent])
        op={'action':'mark_reserve_transfer_in_transit','amount':35000.0,'source':'BNZ Bucket 1','destination':'Simplicity Cash Fund','purpose':'Bucket 1 retirement reserve','reported_date':'2026-09-17'}
        state,_,_=editing.plan(state,[op])
        self.assertEqual(state['b1_float'],15000)
        self.assertEqual(state['b2_pending']['amount'],35000)
        self.assertEqual(state['financeConnector']['reserve_transfers']['2026-09-17-bnz-to-simplicity-cash']['status'],'in_transit')
        from dataset import totals
        result=totals(state)
        self.assertEqual(result['cash_bucket'],300000)
        self.assertEqual(result['cash_bucket_in_transit'],35000)
        self.assertEqual(result['other_pending_funds'],0)
        state,_,_=editing.plan(state,[{'action':'assign_reserve_transit_to_b1','reported_date':'2026-09-17'}])
        self.assertEqual(state['b1_float'],50000)
        self.assertIsNone(state['b2_pending'])
        result=totals(state)
        self.assertEqual(result['cash_bucket'],300000)
        self.assertEqual(result['bridge_bucket'],0)
    def test_preview_apply_idempotence_and_undo(self):
        with tempfile.TemporaryDirectory() as directory:
            c=Fake(directory); draft=editing.preview(c,[split()])
            self.assertEqual(c.saves,0)
            result=editing.apply(c,draft['preview_id'],'test-request-1')
            self.assertEqual(result['status'],'applied')
            c.data['revision']+=1
            self.assertEqual(editing.apply(c,draft['preview_id'],'test-request-1')['status'],'already_applied')
            self.assertEqual(c.saves,1)
            c.data['state']['unrelated']='preserve'
            undo=editing.preview(c,[{'action':'undo_edit','request_id':'test-request-1'}])
            editing.apply(c,undo['preview_id'],'test-undo-1')
            self.assertEqual(c.data['state']['transactions'],fixture()['transactions'])
            self.assertEqual(c.data['state']['unrelated'],'preserve')
    def test_stale_preview_and_guarded_undo(self):
        with tempfile.TemporaryDirectory() as directory:
            c=Fake(directory); draft=editing.preview(c,[split()]); c.data['revision']+=1
            with self.assertRaises(FinanceError): editing.apply(c,draft['preview_id'],'test-request-2')
            draft=editing.preview(c,[split()]); editing.apply(c,draft['preview_id'],'test-request-3')
            c.data['state']['transactions'][0]['amount']=-1
            with self.assertRaises(FinanceError): editing.preview(c,[{'action':'undo_edit','request_id':'test-request-3'}])

if __name__=='__main__': unittest.main()
