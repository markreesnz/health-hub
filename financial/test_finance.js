const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {merge, Client} = require('./sync.js');
const calc = require('./calculations.js');
const clone = value => JSON.parse(JSON.stringify(value));
const initial = () => ({transactions:[{id:'a',date:'2026-09-01',amount:-10,category:'Other'}],snapshots:[],payeeOverrides:{a:'Other'},b1_td6:100});

test('merge preserves disjoint edits, added transactions, and null deletions', () => {
  const base = initial(), local = clone(base), remote = clone(base), conflicts = [];
  local.b1_td6 = 0;
  remote.transactions.push({id:'b',date:'2026-09-02',amount:-5});
  remote.payeeOverrides.a = null;
  const result = merge(base,local,remote,'',conflicts);
  assert.equal(result.b1_td6,0); assert.equal(result.transactions.length,2);
  assert.equal(result.payeeOverrides.a,null); assert.deepEqual(conflicts,[]);
});
test('conflicting transaction category edits require explicit resolution', () => {
  const base = initial(), local = clone(base), remote = clone(base), conflicts = [];
  local.transactions[0].category = 'Groceries'; remote.transactions[0].category = 'Travel';
  merge(base,local,remote,'',conflicts);
  assert.deepEqual(conflicts,['transactions.rows.a.category']);
  assert.equal(merge(base,local,remote,'',[],'remote').transactions[0].category,'Travel');
});
test('transaction deletion is not resurrected by unrelated edits', () => {
  const base = initial(), local = clone(base), remote = clone(base);
  local.transactions = []; remote.b1_td6 = 200;
  const result = merge(base,local,remote);
  assert.deepEqual(result.transactions,[]); assert.equal(result.b1_td6,200);
});

function harness() {
  const data = new Map(); const statuses = [];
  const storage = {getItem:k=>data.get(k) || null,setItem:(k,v)=>data.set(k,v)};
  let current = {revision:1,state:initial()}, posts = 0, failPull = false, loseReply = false;
  const client = new Client({api:'',key:'test',initial:initial(),storage,
    onState:()=>{},onEmpty:()=>{},onConflict:()=>{},onStatus:s=>statuses.push(s),
    fetch:async (url,opts) => {
      if (opts.method === 'GET') {
        if (failPull) throw new Error('offline');
        return {ok:true,status:200,json:async()=>clone(current)};
      }
      posts++;
      const req=JSON.parse(opts.body);
      if (current.mutationId === req.mutationId) return {ok:true,status:200,json:async()=>clone(current)};
      if (req.revision !== current.revision) return {ok:false,status:409,json:async()=>({current:clone(current)})};
      current={revision:current.revision+1,state:clone(req.state),mutationId:req.mutationId};
      if (loseReply) {loseReply=false;throw new Error('response lost');}
      return {ok:true,status:200,json:async()=>clone(current)};
    }});
  return {client,statuses,storage,get current(){return current;},set current(v){current=v;},get posts(){return posts;},set failPull(v){failPull=v;},set loseReply(v){loseReply=v;}};
}
function stop(h) { clearTimeout(h.client.timer); }
test('failed initial pull never pushes, even with queued changes', async () => {
  const h=harness(); h.failPull=true;
  h.client.change({...initial(),b1_td6:0});
  await h.client.sync(); stop(h);
  assert.equal(h.posts,0); assert.equal(h.client.ready,false);
});
test('opening with an unversioned stale cache adopts server without writing', async () => {
  const h=harness(); h.client.local.transactions=[];
  await h.client.sync(); stop(h);
  assert.equal(h.posts,0); assert.equal(h.client.local.transactions.length,1);
  assert.ok(h.storage.getItem('test-legacy-recovery'));
});
test('two devices editing different fields rebase safely', async () => {
  const h=harness(); await h.client.sync();
  const local=clone(h.client.local); local.b1_td6=0; h.client.change(local);
  const remote=clone(h.current); remote.revision++; remote.state.payeeOverrides.a=null; h.current=remote;
  await h.client.sync(); stop(h);
  assert.equal(h.current.state.b1_td6,0); assert.equal(h.current.state.payeeOverrides.a,null);
});
test('conflicting devices do not post until the user resolves', async () => {
  const h=harness(); await h.client.sync();
  h.client.change({...h.client.local,b1_td6:200});
  h.current={revision:2,state:{...initial(),b1_td6:300}};
  await h.client.sync(); assert.equal(h.posts,0); assert.ok(h.client.conflict);
  await h.client.resolve('remote'); stop(h);
  assert.equal(h.client.local.b1_td6,300); assert.equal(h.posts,0);
});
test('lost POST response is retried without creating a second revision', async () => {
  const h=harness(); await h.client.sync(); h.loseReply=true;
  h.client.change({...h.client.local,b1_td6:0}); await h.client.sync();
  assert.equal(h.current.revision,2);
  await h.client.sync(); stop(h);
  assert.equal(h.current.revision,2); assert.equal(h.client.local.b1_td6,0);
});
test('offline edits are persisted with their original base', async () => {
  const h=harness(); await h.client.sync(); h.failPull=true;
  h.client.change({...h.client.local,b1_td6:0}); await h.client.sync(); stop(h);
  const cache=JSON.parse(h.storage.getItem('test'));
  assert.equal(cache.base.b1_td6,100); assert.equal(cache.local.b1_td6,0);
});

const categories=[{name:'Groceries'},{name:'Insurance'},{name:'Transfer',excluded:true},{name:'Renovation',oneOff:true}];
test('forecast excludes transfers, income, one-offs and excluded transactions', () => {
  const transactions=[['Groceries',-100],['Groceries',50],['Transfer',-5000],['Renovation',-5000]].map(([category,amount],i)=>({id:String(i),date:'2026-01-01',category,amount}));
  transactions.push({date:'2026-01-01',category:'Groceries',amount:-10000,excluded:true});
  const result=calc.forecast({transactions},categories,1000,'2026-01-01','2026-01-10');
  assert.equal(result.totalYtd,100); assert.equal(result.totalForecast,3650);
});
test('lumpy forecast uses annual override while cash dates remain unchanged', () => {
  const state={transactions:[{date:'2026-01-05',category:'Insurance',amount:-1200}],categoryAnnualForecast:{Insurance:{amount:1200,period:'year'}}};
  const before=clone(state), result=calc.forecast(state,categories,2000,'2026-01-01','2026-01-10');
  assert.equal(result.totalForecast,1200); assert.deepEqual(state,before);
});
test('forecast has no stale result after override edits or removal', () => {
  const state={transactions:[],categoryAnnualForecast:{Insurance:1200}};
  assert.equal(calc.forecast(state,categories,2000,'2026-01-01','2026-01-10').totalForecast,1200);
  state.categoryAnnualForecast.Insurance=0;
  assert.equal(calc.forecast(state,categories,2000,'2026-01-01','2026-01-10').totalForecast,0);
});
test('fortnight boundaries survive NZ DST and dates before the anchor', () => {
  assert.equal(calc.alignedStart('2026-09-23','2026-10-07'),'2026-10-07');
  assert.equal(calc.alignedStart('2026-09-23','2026-09-22'),'2026-09-09');
  assert.equal(calc.dayIndex('2026-09-23','2026-10-06'),14);
});
test('versioned migration preserves explicit zero and is idempotent', () => {
  const context={};vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/migrations.js','utf8'),context);
  const state=initial();state.b1_td6=0;state.b1_td12=0;
  const result=context.applyMigrations(state);
  assert.equal(result.b1_td6,0); assert.equal(result.b1_td12,0);
  const once=JSON.stringify(result);context.applyMigrations(result);assert.equal(JSON.stringify(result),once);
});
