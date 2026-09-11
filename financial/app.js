  // ========== Constants ==========
  // targetSpend lowered 148,300 → 142,000 on 2026-08-04: donations stopped ($10.8K/yr came
  // out), and the remaining core run-rate (baseline 1 May) annualises to ~$141.5K. Rounded up
  // for a small buffer. Was $137.5K base + $10.8K giving = $142K; now spend-based.
  // b2 = the bridge from retirement (~52) to KiwiSaver unlock at 65: ~13 years of spending at
  // targetSpend, less what B1's float + TD ladder already covers. Was $1.0M when B2 was a
  // medium-term Balanced holding; re-derived Sep 2026 when Conservative took over the bridge role.
  const TARGETS = { b1: 300000, b2: 1550000, b3: 4087332, targetSpend: 142000, drawRate: 0.035 };
  const TRIGGERS = { b1_refill_threshold: 150000, b2_refill_threshold: 600000, b2_peak_drawdown: 0.15, b3_peak_drawdown: 0.20 };
  const DAILY_TARGET = TARGETS.targetSpend / 365.25;
  const STORAGE_KEY = 'mark-financial-plan-v4';
  // base path so proxy calls work under HA ingress AND on the LAN
  const API = location.pathname.endsWith('/') ? location.pathname.slice(0, -1) : location.pathname.replace(/\/[^/]*$/, '');

  const CATEGORIES = [
    { name: 'Groceries' }, { name: 'Eating out' }, { name: 'Wine' }, { name: 'Fuel' },
    { name: 'Transport' }, { name: 'Power' }, { name: 'Internet' },
    { name: 'Insurance' }, { name: 'Rates — Nottingham' }, { name: 'Rates — Kensington' }, { name: 'Body corporate' },
    { name: 'Water — Nottingham' }, { name: 'Water — Kensington' },
    { name: 'Health' }, { name: 'School fees' }, { name: 'Subscriptions' }, { name: 'Books and records' },
    { name: 'Personal' }, { name: 'Home & garden' }, { name: 'Travel' },
    { name: 'Family payments' },
    // Giving categories removed Aug 2026 (donations stopped 2026-08-04); historical giving
    // transactions are retagged to 'Other' (excluded) by the _removeGivingWineAug2026 migration.
    { name: 'Cash/ATM' }, { name: 'Legal fees', oneOff: true }, { name: 'Banking fees' },
    { name: 'Other' },
    { name: 'Renovation', oneOff: true },     // tracked but excluded from core $142K target
    { name: 'Vehicle', oneOff: true },         // car purchases — one-off capital, excluded from run-rate
    { name: 'Tax', excluded: true },           // PAYE/provisional/terminal — excluded from $142K
    { name: 'Transfer', excluded: true },
    { name: 'Investing', excluded: true },
    { name: 'Income', excluded: true },
    { name: 'Reimbursable', excluded: true }  // shared costs to be paid back (e.g. Sarah's half) — net of expense & repayment = what you're owed
  ];
  const EXCLUDED_CATS = new Set(CATEGORIES.filter(c => c.excluded).map(c => c.name));
  const ONEOFF_CATS = new Set(CATEGORIES.filter(c => c.oneOff).map(c => c.name));

  const CAT_RULES = [
    { re: /countdown|pak.?n.?save|new world|four square|fresh choice|woolworths|moore wilsons|farro/i, cat: 'Groceries' },
    { re: /uber\s*eats|menulog|doordash|delivereasy|hellofresh|my food bag/i, cat: 'Eating out' },
    { re: /cafe|restaurant|bistro|kitchen\s|coffee|bar\s|brewery|tavern|pub\s|sushi/i, cat: 'Eating out' },
    { re: /^z\s|z energy|bp\s|caltex|mobil|gull\s|waitomo|npd\s|gas station|fuel/i, cat: 'Fuel' },
    { re: /uber\s|metlink|snapper|at hop|wellington trans|auckland trans|park.?n.?ride|taxi|wilson park/i, cat: 'Transport' },
    { re: /genesis|mercury|contact energy|electric kiwi|powershop|trustpower|frank energy|nova energy|meridian|ecotricity|flick/i, cat: 'Power' },
    { re: /vodafone|spark billing|simply broadband|2degrees|2 degrees|one\.?nz|skinny|slingshot|orcon|2talk/i, cat: 'Internet' },
    { re: /aa insurance|ami\s|state insurance|tower insurance|vero|nzi\s|southern cross|cove insurance|trade me insurance/i, cat: 'Insurance' },
    { re: /ww901093098/i, cat: 'Water — Nottingham' },   // Nottingham Tiaki Wai account ref
    { re: /tiaki\s*wai|wellington water|water\s*rates|water\s*bill|watercare|capacity\s*infrastructure/i, cat: 'Water — Kensington' },
    { re: /wellington city council/i, cat: 'Rates — Nottingham' },
    { re: /city council|district council|rates payment/i, cat: 'Rates — Kensington' },
    { re: /body corp|body corporate|strata fee|owners corp|crockers|bayleys property/i, cat: 'Body corporate' },
    { re: /\blawyers?\b|\bsolicitor|legal services|\bbarrister|law firm|notary public|\battorney|bell gully|russell mcveagh|chapman tripp|buddle findlay|simpson grierson|minterellison|anthony harper|duncan cotterill|dla piper|kensington swan/i, cat: 'Legal fees' },
    { re: /unichem|life pharmacy|chemist warehouse|chemist|pharmacy|dental|dentist|doctor|gp\s|medical centre|optometrist/i, cat: 'Health' },
    { re: /whitcoulls|unity books|paper plus|time out books|vic books|book depository|bookshop|booksellers|university book|scorpio books|kindle|flying nun|real groovy|slowboat|slow boat|volume records|marbecks|vinyl/i, cat: 'Books and records' },
    { re: /netflix|spotify|disney\+|disney plus|apple\.com.bill|apple\s*services|prime video|amazon prime|sky tv|neon\s|adobe|microsoft\s|google\s*one|youtube|patreon|nyt\s|the times|nzme/i, cat: 'Subscriptions' },
    { re: /\bschool fees|\bcollege fees|tuition fee|kindergart|kindy fees|after.?school care|before.?school care/i, cat: 'School fees' },
    { re: /whitcoulls|paper plus|unity books|time out bookstore|dymocks|book depository|booktopia|fishpond|kindle|audible|kobo|wheelers books/i, cat: 'Books and records' },
    { re: /air new zealand|airnz|jetstar|qantas|booking\.com|airbnb|hotel|expedia|trivago|interisland|bluebridge/i, cat: 'Travel' },
    { re: /atm withdraw|cash withdraw|cash out/i, cat: 'Cash/ATM' },
    { re: /transfer|tfr\s|to acct|from acct|own account/i, cat: 'Transfer' },
    { re: /simplicity|kiwisaver|westpac td|term deposit|investment fund/i, cat: 'Investing' },
    { re: /salary|payroll|wages|bnz inward credit|ird refund|interest paid/i, cat: 'Income' },
    { re: /inland revenue|\bird\b|\bird payment|provisional tax|terminal tax|paye payment|tax payment/i, cat: 'Tax' },
    { re: /fee|bnz fee|account fee|overseas service margin|fx fee/i, cat: 'Banking fees' },
    { re: /mitre 10|bunnings|placemakers|kings plant barn|garden|hardware/i, cat: 'Home & garden' },
    { re: /\brenovat|\brefurb|kitchen design|kitchen studio|cabinetmaker|\bjoiner\b|\btiler\b|stonemason|fletcher building|plasterboard|gib stop|builder ltd|building services|interior designer/i, cat: 'Renovation' },
    { re: /gazley|gazely|\bmotors\b|car dealer|automobiles|\bholden\b|\btoyota\b|\bmazda\b|\bsubaru\b|\bvolkswagen\b|\baudi\b|\bbmw\b/i, cat: 'Vehicle' },
    { re: /kmart|warehouse|farmers|hallenstein|glassons|smith|cotton on|target/i, cat: 'Personal' },
    { re: /\bwine\b|winery|vineyard|cellar door|glengarry|regional wines|black market|vintners|wineseeker|wine society/i, cat: 'Wine' }
  ];

  function autoCategorise(payee, desc) {
    const haystack = (payee + ' ' + (desc||'')).toLowerCase();
    for (const r of CAT_RULES) { if (r.re.test(haystack)) return r.cat; }
    return 'Other';
  }

  const DEFAULTS = {
    b1_float: 26676, b1_td6: 126153.53, b1_td12: 126296.23,
    b2_balance: 0, b2_peak: 0,
    b3_balance: 2358736, b3_peak: 2358736,
    ks_balance: 846281,
    // Sep 2026: all non-KiwiSaver investments consolidated into Simplicity Conservative.
    // B2 and B3 are now notional slices of this single pool (see totalsFromState).
    conservative_balance: 0,
    // Share holdings — used by the Pending capital events table
    gentrack_shares: 34700, gentrack_price: 3.92,             // tax-free, NZD
    // Property values (current estimates; not in investable buckets but counted in net worth)
    property_kensington: 750000,    // primary residence
    // Sep 2026: re-based from the Apr 2025 purchase price ($1,620,000) to a PESSIMISTIC
    // sale estimate. Wellington City was down 6.24% YoY and 4.59% in the quarter to Jul 2026
    // and still falling; the upper quartile has been the weakest segment. Fire 3 Oct 2025,
    // rebuild by Johns Lyng ($479,924.79, complete ~Nov 2026) restores pre-loss condition but
    // adds little beyond it, and disclosed fire history carries a real buyer discount.
    // Scenarios: pessimistic $1.31M / central ~$1.45M / optimistic $1.54M. Carrying the
    // pessimistic figure deliberately so the plan is not resting on the best case.
    property_nottingham: 1310000,   // selling Dec 2026 — pessimistic estimate, not purchase price
    // Expected settlement for Nottingham. The original plan assumed Dec 2026; rebuild completes
    // ~Nov 2026 and selling straight off the CCC is the worst window for both fire stigma and
    // the thin Dec/Jan market, so Mar 2027 is now the working assumption. Drives the settlement
    // panel: carrying cost of the slip, and whether liquid cover reaches settlement.
    settlement_date: '2027-03-31',
    // Pre-bucket holdings — owned now, will move into buckets at known dates
    westpac_tds: 0,                 // matured Jun 2026 — proceeds moved to Simplicity
    // $240,000 gross, pro-rated to the 10/12 of the year actually worked, less the 25%
    // forfeited on leaving, then less 39% tax: 240,000 x 10/12 x 0.75 x 0.61 = $91,500.
    // Previously carried at $146,400, which applied the tax haircut only.
    dvrp_net: 91500,                // paid Dec 2026 → B2/B3
    // Westpac TDs — both matured Jun 2026, proceeds in Simplicity Cash + Balanced
    westpac_td_jun18: 0,
    westpac_td_jun20: 0,
    // Stress test inputs: % drop from current for each bucket
    stress_b2: 0,
    stress_b3: 0,
    // Per-category amortization (spread months). Defaults below; user overrides via UI.
    categorySpread: {},
    // Per-category forecast override: { 'Body corporate': 4000, 'Insurance': 1200 }
    // If set, forecast for that category = this annual amount (regardless of YTD).
    // If unset, forecast is auto-inferred from YTD pace.
    // Baked baseline (post-squash, 2026-06-24): the canonical per-category budgets.
    categoryAnnualForecast: {
      'Body corporate': 10292,
      'Family payments': { amount: 1267.5, period: 'fortnight' },
      'Rates — Nottingham': { amount: 364.29, period: 'fortnight' },
      'Rates — Kensington': { amount: 214, period: 'fortnight' },
      'Eating out': { amount: 400, period: 'fortnight' },
      'Groceries': { amount: 700, period: 'fortnight' },
      'Transport': { amount: 25, period: 'fortnight' },
      'Fuel': { amount: 50, period: 'fortnight' },
      'Health': { amount: 1375, period: 'month' },
      'Personal': { amount: 400, period: 'month' },
      'Banking fees': { amount: 21, period: 'month' },
      'Power': { amount: 350, period: 'month' },
      'Internet': { amount: 85, period: 'month' },
      'Books and records': { amount: 200, period: 'month' },
      'Home & garden': { amount: 5000, period: 'year' },
      'Travel': { amount: 6500, period: 'year' },
      'Insurance': { amount: 1780, period: 'year' },
      'Subscriptions': { amount: 1000, period: 'year' },
    },
    // Map each category to a BNZ account so we can roll up category forecasts to
    // the fortnightly account allocation (Living Well / Essentials / Accruals / Savings).
    categoryAccount: {
      'Groceries': 'Living Well', 'Eating out': 'Living Well', 'Personal': 'Living Well',
      'Transport': 'Living Well', 'Fuel': 'Living Well', 'Cash/ATM': 'Living Well',
      'Travel': 'Living Well', 'Home & garden': 'Living Well', 'Books and records': 'Living Well',
      'Subscriptions': 'Living Well', 'Wine': 'Living Well', 'Other': 'Living Well',
      'Family payments': 'Essentials', 'Health': 'Essentials', 'Power': 'Essentials',
      'Internet': 'Essentials', 'Banking fees': 'Essentials',
      'Rates — Nottingham': 'Essentials', 'Rates — Kensington': 'Essentials',
      'Body corporate': 'Accruals', 'Insurance': 'Accruals',
    },
    // Fortnightly budget per BNZ account ($/fortnight).
    accountFortnightly: { 'Living Well': 2555, 'Essentials': 2691, 'Accruals': 564, 'Savings': 0 },
    // Any past pay date — used to align the "current fortnight" window to your actual pay cycle.
    payAnchorDate: '2026-05-06',
    // Categories the user has marked as "not a mismatch" — hidden from the allocation check
    allocationIgnore: [],
    // Net worth display toggles
    nwIncludePrimary: true,
    // Personal — for net-worth projection (Mark born 27 Sep 1974)
    birth_year: 1974,
    checks: {},
    hideCompletedMilestones: false,
    snapshots: [{ date: '2026-05-01', b1_float: 26676, b1_td6: 125000, b1_td12: 125000, b2: 0, b3: 2358736, ks: 846281 }],
    transactions: [],
    payeeOverrides: {},
    lastImport: null,
    sources: ['Living Well', 'Essentials', 'Savings', 'Accruals'],
    weekly: { date: null, items: {} },
    akahuLastFetch: null,
    cachedAkahuAccounts: null,
    contributions: [],
    savingsOpen: 0,           // open savings balance (no target) — for holidays/renos
    lastSweepFortnight: null  // fnStart ISO of the last fortnight whose underspend was swept
  };
  // Single source of truth for the 5 BNZ spending accounts.
  // akahu = name as it appears in Akahu (BNZ has a typo: "Accurals")
  // plan  = canonical name used in state.acctActualBalance, STANDARD_ACCOUNTS, source labels
  // Essentials was closed Aug 2026 — its budget + categories folded into Living Well
  // (see the _essWaterAug2026 migration). No longer a live BNZ account.
  const BNZ_ACCOUNTS = [
    { akahu: 'Living Well',   plan: 'Living Well' },
    { akahu: 'Accurals',      plan: 'Accruals' },
    { akahu: 'Savings',       plan: 'Savings' },
  ];
  const STANDARD_ACCOUNTS = BNZ_ACCOUNTS.map(a => a.plan);

  const AKAHU_SYNC_MAP = [
    // Sep 2026: everything outside KiwiSaver was consolidated into the Conservative Fund, then
    // $1.8M switched into Balanced. The Growth and Cash funds were emptied and are no longer
    // mapped — they still appear in Akahu carrying stale share counts against a $0 balance, and
    // akahuValue() would ignore that anyway, but there is nothing left for them to sync.
    { connection: 'Simplicity', name: 'Conservative Fund', stateKey: 'conservative_balance', label: '→ Conservative (bridge / B2)' },
    { connection: 'Simplicity', name: 'Balanced Fund',    stateKey: 'b2_balance',   label: '→ Balanced (long money / B3)' },
    { connection: 'Simplicity', name: "Mark's Kiwisaver", stateKey: 'ks_balance',   label: '→ KiwiSaver', useCurrent: true },
    { connection: 'BNZ',        name: 'Bucket 1',         stateKey: 'b1_float',     label: '→ B1 Float' },
  ];

  // Older storage keys we used during early iterations — checked as fallback.
  const LEGACY_KEYS = ['mark-financial-plan-v3', 'mark-financial-plan-v2', 'mark-financial-plan-v1'];
  let _migratedFromKey = null;
  let _hadLocalData = false;   // cross-device sync

  function mergeWithDefaults(obj) {
    const merged = Object.assign({}, JSON.parse(JSON.stringify(DEFAULTS)), obj || {});
    merged.sources = merged.sources || [];
    STANDARD_ACCOUNTS.forEach(a => { if (!merged.sources.includes(a)) merged.sources.push(a); });
    return merged;
  }

  function loadState() {
    // 1) Try the current key
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) { _hadLocalData = true; return mergeWithDefaults(JSON.parse(raw)); }
    } catch(e) {}
    // 2) Fall back to older keys (newest first). If we find data, migrate it.
    for (const key of LEGACY_KEYS) {
      try {
        const raw = localStorage.getItem(key);
        if (!raw) continue;
        const parsed = JSON.parse(raw);
        const merged = mergeWithDefaults(parsed);
        _migratedFromKey = key;
        // Persist under the current key so we don't keep looking
        localStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
        return merged;
      } catch(e) {}
    }
    return JSON.parse(JSON.stringify(DEFAULTS));
  }
  let syncClient = null;
  function cleanState(s) {
    for (const k of ['akahuAppToken', 'akahuUserToken', 'openLog', 'lastAutoBackup', 'aiInsights', 'aiDismissed', 'aiFeedback', 'aiSpendInsights', 'aiSpendDismissed', 'aiSpendFeedback', '_bannedTxIds', '_patches']) delete s[k];
    return s;
  }
  function saveState() {
    cleanState(state);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (_) { setSyncStatus('Storage full — export a backup before closing this page', true); }
    if (syncClient) syncClient.change(state);
  }
  function setSyncStatus(message, warning) {
    const el = document.getElementById('lastSaved');
    const failed = warning && message !== 'Changes waiting to save';
    el.textContent = message; el.className = 'save-warning'; el.hidden = !failed; el.style.display = failed ? '' : 'none';
    document.getElementById('retrySave').hidden = !failed;
  }
  async function syncFromServer() { return syncClient ? syncClient.sync() : false; }

  // Any 'Other' transaction whose payee has a learned rule follows the rule. 'Other' only
  // ever means "never categorised" (categorising a tx always writes the payee rule too), so
  // this is safe to run on every load — it heals transactions that were imported while a
  // device was holding a copy whose rules had been clobbered.
  function applyRulesToUncategorised(s) {
    let changed = 0;
    const rules = s.payeeOverrides || {};
    (s.transactions || []).forEach(t => {
      if (t.category !== 'Other') return;
      const cat = rules[payeeKey(t.payee) || (t.payee || '(none)').toLowerCase()];
      if (cat && cat !== 'Other') { t.category = cat; changed++; }
    });
    return changed;
  }
  // ===== Theme (dark/light) =====
  (function () {
    const saved = localStorage.getItem('fp-theme');
    if (saved) document.documentElement.dataset.theme = saved;
    const track = document.getElementById('themeSwitchTrack');
    const label = document.getElementById('themeSwitchLabel');
    function update() {
      const dark = document.documentElement.dataset.theme === 'dark';
      if (track) track.classList.toggle('on', dark);
      if (label) label.textContent = dark ? 'Dark mode' : 'Light mode';
    }
    update();
    if (track) track.addEventListener('click', () => {
      const dark = document.documentElement.dataset.theme !== 'dark';
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      localStorage.setItem('fp-theme', dark ? 'dark' : 'light');
      update();
    });
  })();

  let state = loadState();

  // ===== One-shot state migrations =====
  // The jun2026 data migrations (44 of them) were squashed on 2026-06-24: each had already run on
  // the live data, and their end-state config is now baked into the default state object above.
  // Add any NEW one-time migration below, guarded by a flag:
  //   if (!state.migrations.X) { /* … */ state.migrations.X = true; try { saveState(); } catch(e) {} }
  state.migrations = state.migrations || {};
  let currentForecast = null;
  let chartCum, chartCat, chartHistory, chartMonthly, chartDow, chartNetWorth, chartKiwiSaver;
  let akahuAccounts = null;

  // Clear any stale digest-dismissed flag on every load (Weekly review is always visible now)


  function fmt(n) { if (n === null || n === undefined || isNaN(n)) return '$0'; return '$' + Math.round(n).toLocaleString('en-NZ'); }
  function fmtCompact(n) { const a = Math.abs(n); if (a >= 1e6) return '$' + (n/1e6).toFixed(2) + 'M'; if (a >= 1e3) return '$' + Math.round(n/1e3) + 'K'; return '$' + Math.round(n); }
  function pct(n) { return Math.round(n*100) + '%'; }
  function pad2(n) { return n<10 ? '0'+n : ''+n; }
  function todayISO() { const d = new Date(); return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate()); }
  function parseISO(s) { return new Date(s + 'T00:00:00'); }
  function daysAgoISO(n) { const d = new Date(); d.setDate(d.getDate()-n); return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate()); }
  // The pay-aligned fortnight boundary on or before `dateISO` — the every-second-Wednesday cadence
  // anchored to payAnchorDate. Used to decide when a manual rollover becomes due.
  function payAlignedStart(dateISO) {
    return FinanceCalculations.alignedStart(state.payAnchorDate || daysAgoISO(14), dateISO);
  }
  // The most recent salary credit ("BNZ Salaries"), or null — the same signal the payday
  // distribution panel keys off.
  function latestSalary() {
    let best = null;
    (state.transactions || []).forEach(t => {
      if (t.amount <= 0 || t.excluded) return;
      if (!/bnz salar/i.test((t.description || '') + ' ' + (t.payee || ''))) return;
      if (!best || t.date > best.date) best = { date: t.date, amount: t.amount };
    });
    return best;
  }
  // The active "This fortnight" window. MANUAL mode: the start advances ONLY when you press
  // "Start new fortnight" (rolloverFortnight) — never automatically — so the spend bars don't
  // silently zero on payday.
  const FN_SEED_VERSION = 3;
  function currentFortnight() {
    const todayStr = todayISO();
    // (Re)seed when unset or when a prior build seeded with older logic (also after importing an old
    // backup, which has no fortnightStart). Anchor to the fortnight funded by the most recent salary
    // and treat that salary as already acknowledged, so the "Start new fortnight" button stays hidden
    // until a genuinely NEW salary credit lands.
    if (!state.fortnightStart || state.fnSeedVersion !== FN_SEED_VERSION) {
      const s0 = latestSalary();
      state.fortnightStart = payAlignedStart(s0 ? s0.date : todayStr);
      state.lastRolloverSalaryDate = s0 ? s0.date : '';
      state.fnSeedVersion = FN_SEED_VERSION;
      saveState();
    }
    const fnStartISO = state.fortnightStart;
    const fnDayIndex = FinanceCalculations.dayIndex(fnStartISO, todayStr);
    return { fnStartISO, fnDayIndex };
  }
  // True once a NEW salary has landed since the last manual rollover — i.e. the "Start new
  // fortnight" button is due. Mirrors the payday-distribution panel's salary-arrival trigger.
  function fortnightRolloverDue() {
    const s = latestSalary();
    return !!(s && s.date > (state.lastRolloverSalaryDate || ''));
  }
  // Start the fortnight we're now in: advance to the current pay-aligned boundary, zero the view,
  // and acknowledge the latest salary so the button hides until the next pay lands.
  function rolloverFortnight() {
    state.fortnightStart = payAlignedStart(todayISO());
    const s = latestSalary();
    if (s) state.lastRolloverSalaryDate = s.date;
    saveState();
    render();
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  // Safely set textContent — no-op if the element is missing (handles UI elements removed/renamed over time).
  function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
  function payeeKey(payee) { return (payee||'').toLowerCase().replace(/[^a-z0-9]/g,'').slice(0,40); }
  function startOfYearISO() { return new Date().getFullYear() + '-01-01'; }

  // Tracking baseline: the date from which all "since-baseline" spend calculations start.
  // Defaults to start of year, but the user can set it forward (e.g. May 2026) to draw a
  // line under pre-baseline spending that doesn't reflect future lifestyle.
  function baselineISO() {
    const b = state.baselineDate;
    if (b && /^\d{4}-\d{2}-\d{2}$/.test(b)) return b;
    return startOfYearISO();
  }
  function baselineIsSet() {
    return !!(state.baselineDate && state.baselineDate !== startOfYearISO());
  }
  // How many days from baseline to year-end (denominator for pro-rating the $142K target).
  function baselineDaysToYearEnd() {
    const now = new Date();
    const yearEnd = new Date(now.getFullYear() + 1, 0, 1);
    const baseline = parseISO(baselineISO());
    return Math.max(1, Math.floor((yearEnd - baseline) / 86400000));
  }
  // Days elapsed from baseline to today.
  function baselineDaysElapsed() {
    const now = new Date();
    const baseline = parseISO(baselineISO());
    return Math.max(1, Math.floor((now - baseline) / 86400000) + 1);
  }
  // Pro-rated target for the baseline → year-end window (e.g. $142K × 244/365 if baseline is May 1).
  function baselineTargetForWindow() {
    return TARGETS.targetSpend * baselineDaysToYearEnd() / 365.25;
  }
  // Friendly label for the baseline period, e.g. "since May 1" (suppresses year if current).
  function baselineLabel() {
    const iso = baselineISO();
    if (iso === startOfYearISO()) return 'YTD';
    const d = parseISO(iso);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return 'since ' + months[d.getMonth()] + ' ' + d.getDate() + (sameYear ? '' : ' ' + d.getFullYear());
  }

  // Pre-bucket holdings + property + shares — the amount the net-worth line adds on top of the
  // bucket totals. Stored per snapshot (as nwExtra) so historical net worth stays continuous when
  // money moves between pre-bucket holdings and the buckets.
  function snapshotNwExtra() {
    return (+state.property_nottingham||0)
      + ((+state.gentrack_shares||0) * (+state.gentrack_price||0))
      + (+state.westpac_td_jun18||0) + (+state.westpac_td_jun20||0)
      + (+state.dvrp_net||0);
  }

  // Money in transit to Simplicity is tracked as b2_pending {amount, baseline}. Any rise in the
  // deployed funds above the baseline is transit money that has LANDED — only the remainder is
  // still in flight. Counting the full amount until the clear threshold double-counts the landed
  // portion (market drift makes this approximate, but bounds the error to the drift).
  function b2PendingRemaining() {
    if (!state.b2_pending) return 0;
    const landed = (+state.conservative_balance||0) + (+state.b2_balance||0) + (+state.b2_cash||0);
    const arrived = Math.max(0, landed - (+state.b2_pending.baseline||0));
    return Math.max(0, (+state.b2_pending.amount||0) - arrived);
  }

  // Sep 2026: $1.8M switched Conservative -> Balanced. A Simplicity switch settles the sell and
  // the buy on different days and Akahu lags both, so for a few days the money either still shows
  // in Conservative or is briefly in neither fund. Track both legs explicitly so the bucket split
  // AND the total stay correct throughout, rather than the pool appearing to shrink mid-transfer.
  //   notYetLeft — still sitting in the source per Akahu, but already committed: shift it across.
  //   inFlight   — gone from the source, not yet in the destination: add it back to the total.
  function switchInFlight() {
    const sp = state.switch_pending;
    if (!sp) return { notYetLeft: 0, inFlight: 0 };
    // An unpopulated source (no sync yet) must not be read as the money having left it —
    // that empties the source bucket and drops the untransferred remainder from the total.
    if (state[sp.from] == null) return { notYetLeft: +sp.amount || 0, inFlight: 0 };
    const amt     = +sp.amount || 0;
    const left    = Math.min(amt, Math.max(0, (+sp.fromBaseline||0) - (+state[sp.from]||0)));
    const arrived = Math.min(amt, Math.max(0, (+state[sp.to]||0) - (+sp.toBaseline||0)));
    return { notYetLeft: Math.max(0, amt - left), inFlight: Math.max(0, left - arrived) };
  }

  function totalsFromState() {
    const b1 = (+state.b1_float||0) + (+state.b1_td6||0) + (+state.b1_td12||0);
    // Sep 2026: everything outside KiwiSaver sits in one Simplicity Conservative Fund, so B2 and
    // B3 are no longer separate vehicles — they are notional slices of a single pool. Deployed
    // per-fund balances are counted first and the Conservative pool fills B2 to target, remainder
    // to B3. Summing the per-fund keys first means this degrades cleanly back to the real-vehicle
    // model if money is ever split back out into Balanced/Growth.
    // Sep 2026: the funds swapped roles. Conservative now holds the bridge from retirement to
    // KiwiSaver unlock at 65 (B2), and Balanced holds the long money (B3) — the reverse of the
    // original Balanced-is-B2 / Growth-is-B3 layout. Buckets track the JOB the money is doing,
    // not the fund it happens to sit in, so the mapping follows the role. The legacy per-fund
    // keys are all still summed, so this stays correct if money is split across vehicles again.
    const sw = switchInFlight();
    const conservative = Math.max(0, (+state.conservative_balance||0) - sw.notYetLeft);
    // b2_cash (Cash Fund) and b3_balance (Growth Fund) are wound down and no longer synced.
    // They stay in the sums as defensive zeros so a stray balance could never go uncounted.
    const b2_cash = +state.b2_cash||0;
    const b2_pending = b2PendingRemaining();
    // Simplicity Balanced — now long money, feeds B3. Includes anything still in transit.
    const b2_balanced = (+state.b2_balance||0) + sw.notYetLeft + sw.inFlight;
    const b2 = conservative + b2_cash + b2_pending;
    const b3 = (+state.b3_balance||0) + b2_balanced;
    const ks = +state.ks_balance||0;
    const buckets = b1 + b2 + b3 + ks;
    // Pre-bucket holdings — owned now, will be allocated to buckets later
    const westpac = (+state.westpac_td_jun18||0) + (+state.westpac_td_jun20||0);
    const dvrp = +state.dvrp_net||0;
    const preBucket = westpac + dvrp;
    // Other current investable assets (excl. primary residence)
    const gtVal = (+state.gentrack_shares||0) * (+state.gentrack_price||0);
    const shares = gtVal;
    const nottingham = +state.property_nottingham||0;
    // Total investable (excl. primary residence) — matches original plan's $6.23M figure
    const investable = buckets + preBucket + shares + nottingham;
    // Pre-65 pool (everything except KiwiSaver, which unlocks at 65)
    const preKs = investable - ks;
    return { b1, b2, b2_balanced, b2_cash, b2_pending, conservative, b3, ks, switchInTransit: sw.notYetLeft + sw.inFlight, westpac, dvrp, gtVal, shares, preBucket, nottingham, total: investable, preKs };
  }

  // ========== Amortization ==========
  // Categories with lumpy annual/quarterly payments get spread across N months
  // for chart and KPI purposes (transactions still show raw amounts on real dates).
  const DEFAULT_SPREAD = {
    'Body corporate': 12,
    'Insurance': 12,
    'Rates — Nottingham': 1,   // fortnightly auto-payment — no smoothing needed
    'Rates — Kensington': 3    // quarterly billing — smooth across 3 months
  };

  // NZ 2026 school term end dates (Marsden tracks the standard NZ school calendar).
  // Mark pays in the last week of each term — projection uses term_end - 2 days (Wed of last week).
  // For 2027+ the projection falls back to the simple "last-payment + period" math.
  const NZ_2026_TERM_ENDS = ['2026-04-17', '2026-07-03', '2026-09-26', '2026-12-17'];
  function nextSchoolFeePaymentDate(todayDate, lastPaymentDate) {
    const ts = todayDate.getFullYear() + '-' + pad2(todayDate.getMonth()+1) + '-' + pad2(todayDate.getDate());
    for (const end of NZ_2026_TERM_ENDS) {
      if (end < ts) continue;
      // Auto-rotate: skip a term end if a payment of-likely-this-term has hit within the
      // 35 days BEFORE it. Mark pays in the last week of term; if there's a payment within
      // the last few weeks before this term-end, treat that term as already paid.
      if (lastPaymentDate) {
        const endDate = parseISO(end);
        const paymentDate = parseISO(lastPaymentDate);
        const daysBetween = (endDate - paymentDate) / 86400000;
        if (daysBetween >= 0 && daysBetween <= 35) continue;  // already paid for this term
      }
      const d = parseISO(end);
      return { date: new Date(d.getTime() - 2 * 86400000), termEnd: end };
    }
    return null;
  }
  function spreadFor(category) {
    if (state.categorySpread && state.categorySpread[category] != null) return state.categorySpread[category];
    return DEFAULT_SPREAD[category] || 1;
  }
  // How much of transaction t falls within [startISO, endISO] when amortized?
  function amortizedAmountInRange(t, startISO, endISO) {
    const raw = -t.amount;
    const n = spreadFor(t.category);
    if (n <= 1) {
      if (t.date < startISO || t.date > endISO) return 0;
      return raw;
    }
    const txDate = parseISO(t.date);
    const startDate = parseISO(startISO);
    const endDate = parseISO(endISO);
    const endExclusive = new Date(endDate.getTime() + 86400000);
    const perMonth = raw / n;
    let total = 0;
    for (let i = 0; i < n; i++) {
      const ms = new Date(txDate.getFullYear(), txDate.getMonth() + i, 1);
      const me = new Date(txDate.getFullYear(), txDate.getMonth() + i + 1, 1);
      const oStart = ms > startDate ? ms : startDate;
      const oEnd = me < endExclusive ? me : endExclusive;
      if (oEnd <= oStart) continue;
      const monthDays = (me - ms) / 86400000;
      const overlapDays = (oEnd - oStart) / 86400000;
      total += perMonth * (overlapDays / monthDays);
    }
    return total;
  }

  // Spending in a date range — AMORTIZED by default. Annual / quarterly lumps
  // (Body corp, Insurance, Rates) are spread across N months so they don't
  // distort a single month, a 4-week window, or a 90-day pace. Per-account
  // totals and the transaction table use raw=true for actual cash views.
  function spendingInRange(startISO, endISO, opts) {
    const includeOneOff = opts && opts.includeOneOff;
    const raw = opts && opts.raw;
    let total = 0;
    (state.transactions || []).forEach(t => {
      if (EXCLUDED_CATS.has(t.category)) return;
      if (t.excluded) return;
      if (!includeOneOff && ONEOFF_CATS.has(t.category)) return;
      if (t.amount >= 0) return;
      if (raw) {
        if (t.date < startISO || t.date > endISO) return;
        total += -t.amount;
      } else {
        total += amortizedAmountInRange(t, startISO, endISO);
      }
    });
    return total;
  }
  // Sum of just one-off spending (e.g. Renovation) in a range
  function oneOffInRange(startISO, endISO) {
    let total = 0;
    (state.transactions || []).forEach(t => {
      if (t.date < startISO || t.date > endISO) return;
      if (!ONEOFF_CATS.has(t.category)) return;
      if (t.excluded) return;
      if (t.amount < 0) total += -t.amount;
    });
    return total;
  }

  // ========== Render ==========
  function renderWestpacAlerts() {
    const wrap = document.getElementById('westpacAlertsContainer');
    if (!wrap) return;
    // Purge persisted Simplicity change banners (feature retired 2026-07-09 — the weekly
    // digest email carries fund deltas now; only Westpac money-arrived alerts remain).
    if ((state.westpacAlerts || []).some(a => a.kind === 'change')) {
      state.westpacAlerts = state.westpacAlerts.filter(a => a.kind !== 'change');
      saveState();
    }
    const list = state.westpacAlerts || [];
    if (!list.length) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = list.map((a, i) => {
      const isChange = a.kind === 'change';
      const bg = isChange ? '#dbeafe' : '#fef9c3';
      const bd = isChange ? '#93c5fd' : '#fde047';
      const fg = isChange ? '#1e3a8a' : '#713f12';
      const sub = isChange ? '#1d4ed8' : '#a16207';
      const text = isChange
        ? '<strong>' + escapeHtml(a.name) + '</strong> ' + (a.amount >= 0 ? '+' : '−') + fmt(Math.abs(a.amount))
          + ' <span style="color:' + sub + '; font-size:11px;">→ ' + fmt(a.balance) + ' · ' + a.at.slice(0,10) + '</span>'
        : '<strong>' + escapeHtml(a.name) + '</strong> ' + fmt(a.amount) + ' arrived '
          + '<span style="color:' + sub + '; font-size:11px;">' + a.at.slice(0,10) + '</span>';
      return '<div class="alert-bar show" style="background:' + bg + '; border-color:' + bd + '; color:' + fg + '; display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">'
        + '<span>' + text + '</span>'
        + '<button data-idx="' + i + '" class="westpac-dismiss" style="background:none; border:none; color:' + fg + '; cursor:pointer; font-size:18px; padding:0 6px; line-height:1;">×</button>'
        + '</div>';
    }).join('');
    wrap.querySelectorAll('.westpac-dismiss').forEach(btn => {
      btn.addEventListener('click', () => {
        state.westpacAlerts.splice(+btn.dataset.idx, 1);
        saveState(); renderWestpacAlerts();
      });
    });
  }

  // Settlement model. The plan originally assumed a Dec 2026 sale; a slip costs carrying
  // (Nottingham rates + water keep running) and stretches how long B1 has to cover spending
  // on its own, because no proceeds arrive until settlement.
  const SETTLE_BASE     = '2026-12-15';   // original plan assumption
  const NOTT_RATES_FN   = 364.29;         // per fortnight
  const NOTT_WATER_QTR  = 811.94;         // per quarter
  const TD6_MATURES     = '2026-12-08';
  const TD12_MATURES    = '2027-06-08';

  function renderSettlement() {
    const el = document.getElementById('settleDate');
    if (!el) return;
    const iso = state.settlement_date || '2027-03-31';
    el.value = iso;
    const today = new Date(todayISO() + 'T00:00:00');
    const settle = new Date(iso + 'T00:00:00');
    const base   = new Date(SETTLE_BASE + 'T00:00:00');
    const MS_MONTH = 365.25 / 12 * 86400000;
    const monthsAway = Math.max(0, (settle - today) / MS_MONTH);
    const slipMonths = Math.max(0, (settle - base) / MS_MONTH);

    const carryPerMonth = NOTT_RATES_FN * 26 / 12 + NOTT_WATER_QTR / 3;
    const slipCost = slipMonths * carryPerMonth;

    // Liquidity that can actually be reached before settlement.
    const liquidNow = +state.b1_float || 0;
    let maturing = 0;
    if (new Date(TD6_MATURES  + 'T00:00:00') <= settle) maturing += +state.b1_td6  || 0;
    if (new Date(TD12_MATURES + 'T00:00:00') <= settle) maturing += +state.b1_td12 || 0;
    const spendToSettle = monthsAway * TARGETS.targetSpend / 12;
    const cover = liquidNow + maturing - spendToSettle;

    setText('settleAway', '— ' + monthsAway.toFixed(1) + ' months away' +
      (slipMonths > 0.2 ? ' · ' + slipMonths.toFixed(1) + ' months past the original Dec 2026 plan' : ''));

    document.getElementById('settleBody').innerHTML =
      '<tr><td>Carrying cost of the slip</td><td style="text-align:right;">' + fmt(slipCost) +
        '<div style="font-size:11px;color:#9ca3af;">rates + water at ' + fmt(carryPerMonth) + '/mo</div></td></tr>' +
      '<tr><td>Spending to settlement</td><td style="text-align:right;">' + fmt(spendToSettle) + '</td></tr>' +
      '<tr><td>Reachable before settlement</td><td style="text-align:right;">' + fmt(liquidNow + maturing) +
        '<div style="font-size:11px;color:#9ca3af;">float ' + fmt(liquidNow) + ' + TDs maturing ' + fmt(maturing) + '</div></td></tr>' +
      '<tr><td><strong>Cover at settlement</strong></td><td style="text-align:right;"><strong>' + fmt(cover) + '</strong></td></tr>';

    let html = '';
    if (cover < 0) html += '<div><span class="dot bad"></span>Liquid cover runs out before settlement — draw from B2 (Conservative, T+2)</div>';
    else if (cover < TARGETS.targetSpend / 2) html += '<div><span class="dot warn"></span>Under 6 months of spending left at settlement — thin margin if it slips again</div>';
    else html += '<div><span class="dot ok"></span>Liquid cover reaches settlement with ' + (cover / (TARGETS.targetSpend/12)).toFixed(1) + ' months to spare</div>';
    if (slipMonths > 0.2) html += '<div><span class="dot warn"></span>Bridge buffer and the B3 top-up both wait on settlement — nothing rebalances until the money lands</div>';
    document.getElementById('settleTriggers').innerHTML = html;
    const st = document.getElementById('settleStatus');
    st.className = 'status ' + (cover < 0 ? 'bad' : (cover < TARGETS.targetSpend / 2 ? 'warn' : 'ok'));
    st.textContent = cover < 0 ? 'SHORT' : (cover < TARGETS.targetSpend / 2 ? 'THIN' : 'OK');
  }

  document.addEventListener('change', e => {
    if (e.target && e.target.id === 'settleDate') {
      state.settlement_date = e.target.value; saveState(); render();
    }
  });

  function render() {
    currentForecast = FinanceCalculations.forecast(state, CATEGORIES, TARGETS.targetSpend, baselineISO(), todayISO());
    renderManualStatus();
    { const el = document.getElementById('titleDate');
      if (el) {
        const d = new Date();
        const DOW = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
        const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        el.textContent = '— ' + DOW[d.getDay()] + ' ' + d.getDate() + ' ' + MON[d.getMonth()];
      } }
    renderWestpacAlerts();
    renderSettlement();
    const t = totalsFromState();
    const preKsPool = t.preKs;
    const sustain = preKsPool * TARGETS.drawRate;
    const tgt = TARGETS.targetSpend;
    const buffer = sustain - tgt;
    const proj = currentForecast.totalForecast;  // current 12-month projected spend
    setText('kpiSustain', fmt(sustain) + '/yr');
    { const b = document.getElementById('kpiBuffer');
      if (b) {
        // Equivalent drawdown rate: what % of the pre-KiwiSaver pool this year's spend represents.
        const equivDraw = preKsPool > 0 ? (proj / preKsPool * 100) : 0;
        b.innerHTML = 'Spending <strong>' + fmt(proj) + '</strong> <span style="color:var(--text4); font-weight:normal;">(' + equivDraw.toFixed(1) + '% draw)</span>';
        b.style.color = proj <= sustain ? 'var(--text2)' : '#b91c1c';
      } }
    // Headroom bar: target spend (blue) within your sustainable draw, with markers for a few draw
    // rates — 3.0% (conservative), 3.5% (base), 4.0% (the classic "4% rule") — so you see how far
    // below even a cautious draw your target sits.
    { const bar = document.getElementById('headroomBar');
      const ticks = document.getElementById('headroomTicks');
      const legend = document.getElementById('headroomLegend');
      const preKs = TARGETS.drawRate > 0 ? sustain / TARGETS.drawRate : 0;  // pre-KiwiSaver pool
      const draws = [
        // 2.75% = what an all-Conservative pool plausibly sustains in real terms over 35 years.
        // While the whole pool sits in the Conservative fund this is the marker that binds,
        // not the 3.5% base rate, which assumed a growth-weighted portfolio.
        { r: 0.0275, lbl: '2.75%', amt: preKs * 0.0275, col: '#7c3aed' },
        { r: 0.03,  lbl: '3.0%', amt: preKs * 0.03,  col: '#0891b2' },
        { r: 0.035, lbl: '3.5%', amt: preKs * 0.035, col: '#047857' },
        { r: 0.04,  lbl: '4.0%', amt: preKs * 0.04,  col: '#b45309' },
      ];
      const scale = Math.max(proj, draws[draws.length - 1].amt) * 1.04 || 1;
      const projW = Math.min(100, proj / scale * 100);
      const susW = Math.min(100, sustain / scale * 100);
      if (bar) {
        // Blue fill = current 12-month projected spend; green band = spare to sustainable.
        let inner = (proj <= sustain)
          ? '<div style="position:absolute; left:0; top:0; bottom:0; width:' + projW + '%; background:#2563eb;"></div>' +
            '<div style="position:absolute; left:' + projW + '%; width:' + Math.max(0, susW - projW) + '%; top:0; bottom:0; background:rgba(16,185,129,0.35);"></div>'
          : '<div style="position:absolute; left:0; top:0; bottom:0; width:' + susW + '%; background:#2563eb;"></div>' +
            '<div style="position:absolute; left:' + susW + '%; width:' + (projW - susW) + '%; top:0; bottom:0; background:repeating-linear-gradient(45deg,#ef4444,#ef4444 5px,#fecaca 5px,#fecaca 10px);"></div>';
        // draw-rate markers
        draws.forEach(d => {
          const x = Math.min(100, d.amt / scale * 100);
          inner += '<div title="' + d.lbl + ' draw = ' + fmt(d.amt) + '/yr" style="position:absolute; top:0; bottom:0; left:' + x + '%; width:2px; background:' + d.col + ';"></div>';
        });
        bar.innerHTML = inner;
      }
      if (ticks) {
        ticks.innerHTML = draws.map(d => {
          const x = Math.min(98, d.amt / scale * 100);
          return '<span style="position:absolute; left:' + x + '%; transform:translateX(-50%); font-size:9px; color:' + d.col + '; white-space:nowrap;">' + d.lbl + '</span>';
        }).join('') +
        '<span style="position:absolute; left:' + Math.min(96, projW) + '%; transform:translateX(-50%); font-size:9px; color:#2563eb; white-space:nowrap;">spending</span>';
      }
      if (legend) {
        legend.innerHTML = 'Spending <strong>' + fmt(proj) + '</strong> · ~$' + Math.round(proj / 26).toLocaleString('en-NZ') + '/fn &nbsp; — &nbsp; sustainable draw: ' +
          draws.map(d => '<span style="color:' + d.col + ';">' + d.lbl + ' <strong>' + fmt(d.amt) + '</strong></span>').join(' · ');
      }
    }
    // (kpiTotal and kpiEmpDays setText calls removed — KPI cards were retired)

    // Tab pills (quick health badges)
    document.getElementById('tabPlanPill').textContent = fmtCompact(t.total);

    // Bucket 1
    document.getElementById('b1Amt').textContent = fmt(t.b1);
    const b1Pct = Math.min(100, (t.b1 / TARGETS.b1) * 100);
    const b1Bar = document.getElementById('b1Bar');
    b1Bar.style.width = b1Pct + '%';
    b1Bar.className = 'fill ' + (t.b1 < TRIGGERS.b1_refill_threshold ? 'bad' : (t.b1 < 200000 ? 'warn' : ''));
    const b1Tds = (+state.b1_td6||0) + (+state.b1_td12||0);
    let b1HTML = '';
    if (b1Tds < TRIGGERS.b1_refill_threshold) b1HTML += '<div><span class="dot bad"></span>TDs at $' + Math.round(b1Tds/1000) + 'K — below $150K threshold: trigger B2 → B1 top-up ($125K)</div>';
    else b1HTML += '<div><span class="dot ok"></span>TDs at $' + Math.round(b1Tds/1000) + 'K — above $150K threshold</div>';
    if ((+state.b1_float||0) < 30000) b1HTML += '<div><span class="dot warn"></span>Float below $30K — top up to ~$50K from maturing TD at next 6-month review</div>';
    document.getElementById('b1Triggers').innerHTML = b1HTML;
    document.getElementById('b1Status').className = 'status ' + (b1Tds < TRIGGERS.b1_refill_threshold ? 'bad' : 'ok');
    document.getElementById('b1Status').textContent = b1Tds < TRIGGERS.b1_refill_threshold ? 'REFILL' : 'OK';

    // Bucket 2 — headline is the bridge pool: Conservative + any residual Cash + in-transit
    setText('b2Amt', fmt(t.b2));
    const b2PendingRow = document.getElementById('b2PendingRow');
    if (b2PendingRow) {
      b2PendingRow.style.display = t.b2_pending > 0 ? '' : 'none';
      if (t.b2_pending > 0) document.getElementById('b2PendingAmt').textContent = fmt(t.b2_pending);
    }
    const b2Pct = Math.min(100, (t.b2 / TARGETS.b2) * 100);
    const b2Bar = document.getElementById('b2Bar');
    b2Bar.style.width = b2Pct + '%';
    b2Bar.className = 'fill ' + (t.b2 < TRIGGERS.b2_refill_threshold ? 'bad' : (t.b2 < TARGETS.b2 * 0.9 ? 'warn' : ''));
    // Peaks ratchet up automatically. They were manual fields, which is how b3_peak came to sit
    // 24% above the holding it was measuring. Only ever raised here — a fall is the signal the
    // drawdown triggers exist to catch, so it must never be silently absorbed into the peak.
    { let ratcheted = false;
      if (t.b2 > (+state.b2_peak||0)) { state.b2_peak = t.b2; ratcheted = true; }
      if (t.b3 > (+state.b3_peak||0)) { state.b3_peak = t.b3; ratcheted = true; }
      if (ratcheted) saveState(); }
    const b2Peak = +state.b2_peak||0;
    const b2Drawdown = b2Peak > 0 ? (b2Peak - t.b2) / b2Peak : 0;
    let b2HTML = '';
    if (t.b2 < TRIGGERS.b2_refill_threshold) {
      if (b2Drawdown > TRIGGERS.b2_peak_drawdown) b2HTML += '<div><span class="dot bad"></span>Below $600K AND down ' + pct(b2Drawdown) + ' from peak — DO NOT refill from B3; draw from B1 float</div>';
      else b2HTML += '<div><span class="dot bad"></span>Below $600K — refill from B3 (transfer enough to restore to $1M)</div>';
    } else b2HTML += '<div><span class="dot ok"></span>Above $600K refill threshold</div>';
    if (b2Drawdown > TRIGGERS.b2_peak_drawdown) b2HTML += '<div><span class="dot warn"></span>Down ' + pct(b2Drawdown) + ' from peak — pause B2 → B1 top-ups</div>';
    document.getElementById('b2Triggers').innerHTML = b2HTML;
    document.getElementById('b2Status').className = 'status ' + (t.b2 < TRIGGERS.b2_refill_threshold ? 'bad' : (b2Drawdown > TRIGGERS.b2_peak_drawdown ? 'warn' : 'ok'));
    document.getElementById('b2Status').textContent = t.b2 < TRIGGERS.b2_refill_threshold ? 'REFILL' : (b2Drawdown > TRIGGERS.b2_peak_drawdown ? 'HOLD' : 'OK');

    // Bucket 3
    setText('b3Amt', fmt(t.b3));
    const b3TransitRow = document.getElementById('b3TransitRow');
    if (b3TransitRow) {
      b3TransitRow.style.display = t.switchInTransit > 0 ? '' : 'none';
      if (t.switchInTransit > 0) setText('b3TransitAmt', fmt(t.switchInTransit));
    }
    const b3Pct = Math.min(100, (t.b3 / TARGETS.b3) * 100);
    const b3Bar = document.getElementById('b3Bar');
    b3Bar.style.width = b3Pct + '%';
    b3Bar.className = 'fill';
    const b3Peak = +state.b3_peak||0;
    const b3Drawdown = b3Peak > 0 ? (b3Peak - t.b3) / b3Peak : 0;
    let b3HTML = '';
    if (b3Drawdown > TRIGGERS.b3_peak_drawdown) { b3HTML += '<div><span class="dot bad"></span>Down ' + pct(b3Drawdown) + ' from peak — pause B3 → B2 refills</div>'; b3Bar.className='fill bad'; }
    else if (b3Drawdown > 0.10) { b3HTML += '<div><span class="dot warn"></span>Down ' + pct(b3Drawdown) + ' from peak — still within tolerance (trigger is 20%)</div>'; b3Bar.className='fill warn'; }
    else b3HTML += '<div><span class="dot ok"></span>Within 10% of peak — normal operation</div>';
    document.getElementById('b3Triggers').innerHTML = b3HTML;
    document.getElementById('b3Status').className = 'status ' + (b3Drawdown > TRIGGERS.b3_peak_drawdown ? 'bad' : (b3Drawdown > 0.10 ? 'warn' : 'ok'));
    document.getElementById('b3Status').textContent = b3Drawdown > TRIGGERS.b3_peak_drawdown ? 'HOLD' : 'OK';

    document.querySelectorAll('.ro').forEach(el => {
      const v = +state[el.dataset.key] || 0;
      const f = el.dataset.fmt;
      if (f === 'int') el.textContent = Math.round(v).toLocaleString('en-NZ');
      else if (f === 'dec2') el.textContent = v.toFixed(2);
      else if (f === 'year') el.textContent = String(Math.round(v));
      else el.textContent = fmt(v);
    });
    document.querySelectorAll('input.edit').forEach(el => { el.value = (+state[el.dataset.key] || 0); });

    // Share holdings — Gentrack recomputes value live
    const gtVal = (+state.gentrack_shares||0) * (+state.gentrack_price||0);
    setText('gentrackValueDisp', fmt(gtVal));
    // Holdings table total: pre-bucket + shares + Nottingham (excl. future inheritance)
    const holdingsTotal = (+state.westpac_td_jun18||0) + (+state.westpac_td_jun20||0) + (+state.dvrp_net||0)
                        + gtVal + (+state.property_nottingham||0);
    setText('holdingsTotal', fmt(holdingsTotal));

    // Alert bar
    const alertBar = document.getElementById('alertBar');
    let alertMsg = ''; let alertClass = '';
    if (b1Tds < TRIGGERS.b1_refill_threshold && t.b2 >= TRIGGERS.b2_refill_threshold) alertMsg = '<strong>Action:</strong> B1 TDs below $150K — transfer $125K from B2 to restore the ladder.';
    else if (t.b2 < TRIGGERS.b2_refill_threshold && b3Drawdown <= TRIGGERS.b3_peak_drawdown) alertMsg = '<strong>Action:</strong> B2 below $600K — refill from B3 to restore $1M.';
    else if (b3Drawdown > TRIGGERS.b3_peak_drawdown) { alertMsg = '<strong>Hold:</strong> B3 down ' + pct(b3Drawdown) + ' from peak — pause all refills; live on B1.'; alertClass = 'bad'; }
    else if (b2Drawdown > TRIGGERS.b2_peak_drawdown) alertMsg = '<strong>Caution:</strong> B2 down ' + pct(b2Drawdown) + ' from peak — pause B2 → B1 top-ups.';
    if (alertMsg) { alertBar.innerHTML = alertMsg; alertBar.className = 'alert-bar show ' + alertClass; } else alertBar.className = 'alert-bar';


    // Each render section is wrapped in safeCall so a single null DOM reference
    // (from removed/renamed elements) can't crash the entire render pass.
    _renderErrors = [];
    safeCall('renderBaselineInput', renderBaselineInput);
    safeCall('renderSundayDigest', renderSundayDigest);
    safeCall('renderWeeklyAdvice', renderWeeklyAdvice);
    safeCall('renderIncome', renderIncome);
    safeCall('renderForecast', renderForecast);
    safeCall('renderStatusStrip', renderStatusStrip);
    safeCall('renderRecentActivity', renderRecentActivity);
    safeCall('renderWhereItGoes', renderWhereItGoes);
    safeCall('renderAccountBudgets', renderAccountBudgets);
    safeCall('renderCurrentMonthByCategory', renderCurrentMonthByCategory);
    safeCall('renderFortnightControls', renderFortnightControls);
    safeCall('renderPaydayPlan', renderPaydayPlan);
    safeCall('renderSourcesUI', renderSourcesUI);
    safeCall('renderNetWorth', renderNetWorth);
    safeCall('renderAllocation', renderAllocation);
    safeCall('renderAmortRules', renderAmortRules);
    safeCall('renderBulkPayees', renderBulkPayees);
    safeCall('renderLearnedRules', renderLearnedRules);
    safeCall('renderTransactions', renderTransactions);
    safeCall('renderSnapshotTable', renderSnapshotTable);
    safeCall('renderTopPayees', renderTopPayees);
    safeCall('updateCharts', updateCharts);
    showRenderErrors();
  }
  // safeCall isolates each render section so one failure can't blank the whole page — but it must
  // not fail SILENTLY (that hid the blank Settings/forecast bugs). Errors are logged AND surfaced
  // in an on-screen badge so a broken section is immediately obvious.
  let _renderErrors = [];
  function safeCall(name, fn) {
    try { fn(); } catch (e) { _renderErrors.push(name); console.error('Render error in ' + name + ':', e); }
  }
  function showRenderErrors() {
    let bar = document.getElementById('renderErrorBar');
    if (!_renderErrors.length) { if (bar) bar.style.display = 'none'; return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'renderErrorBar';
      bar.title = 'Open the browser console (⌥⌘I) for the stack trace';
      bar.style.cssText = 'position:fixed; bottom:10px; right:10px; z-index:9999; background:#b91c1c;' +
        ' color:#fff; font-size:12px; padding:8px 12px; border-radius:8px; max-width:340px;' +
        ' box-shadow:0 2px 10px rgba(0,0,0,0.25); cursor:pointer; line-height:1.4;';
      bar.addEventListener('click', () => { bar.style.display = 'none'; });
      document.body.appendChild(bar);
    }
    bar.style.display = '';
    bar.textContent = '⚠ ' + [...new Set(_renderErrors)].length + ' section(s) failed to render: ' +
      [...new Set(_renderErrors)].join(', ');
  }

  // ========== Bulk categorise by payee ==========
  function renderBulkPayees() {
    const scope = document.getElementById('bulkScope').value;
    const period = document.getElementById('bulkPeriod').value;
    const srcFilt = document.getElementById('bulkSrcFilter').value;

    let startDate = '0000-01-01';
    if (period === 'ytd') startDate = startOfYearISO();
    else if (period === '90') startDate = daysAgoISO(90);
    else if (period === '28') startDate = daysAgoISO(28);

    const lastImportDate = state.lastImport ? state.lastImport.date : null;

    const groups = {};
    (state.transactions||[]).forEach(t => {
      if (t.date < startDate) return;
      if (t.amount >= 0) return;       // expenses only
      if (EXCLUDED_CATS.has(t.category)) return;
      if (srcFilt !== 'all' && (t.source||'') !== srcFilt) return;
      if (scope === 'newOnly' && t.importedAt !== lastImportDate) return;
      const k = payeeKey(t.payee) || (t.payee||'(none)').toLowerCase();
      if (!groups[k]) {
        groups[k] = { k, name: t.payee || '(no payee)', count: 0, total: 0, category: t.category,
                      hasOverride: !!state.payeeOverrides[k], recent: t.date, recentDesc: t.description||'',
                      sources: new Set() };
      }
      groups[k].count++;
      groups[k].total += -t.amount;
      if (t.source) groups[k].sources.add(t.source);
      if (t.date > groups[k].recent) {
        groups[k].recent = t.date; groups[k].recentDesc = t.description||'';
      }
    });

    let payees = Object.values(groups);
    if (scope === 'needsReview') payees = payees.filter(p => p.category === 'Other');
    payees.sort((a, b) => b.total - a.total);

    const reviewCount = Object.values(groups).filter(p => p.category === 'Other').length;
    document.getElementById('bulkCount').textContent = reviewCount + ' to review';
    document.getElementById('bulkCount').style.display = reviewCount === 0 ? 'none' : '';
    // Surface a top-of-tab banner so categorising is flagged even when detail/tools are hidden.
    { const banner = document.getElementById('needCatBanner');
      const cnt = document.getElementById('needCatCount');
      if (banner) {
        banner.style.display = reviewCount > 0 ? '' : 'none';
        if (cnt) cnt.textContent = reviewCount + ' payee' + (reviewCount === 1 ? '' : 's') + ' need categorising';
      }
    }

    // Auto-expand the section when there's something to review; auto-collapse when empty.
    // Only auto-toggle on initial state — once user manually opens/closes, respect their choice
    // for the rest of the session (tracked via a marker attribute).
    const details = document.getElementById('bulkDetails');
    if (details && !details.dataset.userToggled) {
      details.open = reviewCount > 0;
    }

    const body = document.getElementById('bulkBody');
    body.innerHTML = '';
    const empty = document.getElementById('bulkEmpty');
    const table = document.getElementById('bulkTable');
    if (payees.length === 0) {
      empty.style.display = '';
      empty.textContent = scope === 'needsReview' ? 'All payees are categorised. Switch to "All payees" if you want to review them.' :
                          scope === 'newOnly' ? 'No newly-imported transactions to review.' :
                          'No transactions in this period.';
      table.style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    table.style.display = '';

    payees.slice(0, 200).forEach(p => {
      const tr = document.createElement('tr');
      const needs = p.category === 'Other';
      if (needs) tr.style.background = '#fffbeb';
      const catOpts = CATEGORIES.map(c => '<option value="' + c.name + '"' + (c.name === p.category ? ' selected' : '') + '>' + c.name + ((c.excluded ? ' (excl)' : c.oneOff ? ' (one-off)' : '')) + '</option>').join('');
      const srcSpan = p.sources.size > 0 ? '<div class="src">' + Array.from(p.sources).join(' · ') + '</div>' : '';
      const learnedBadge = p.hasOverride ? ' <span class="pill green" style="font-size:10px;">learned</span>' : '';
      tr.innerHTML =
        '<td><div class="payee">' + escapeHtml(p.name) + learnedBadge + '</div>' + srcSpan + '</td>' +
        '<td><div class="desc">' + escapeHtml(p.recent) + (p.recentDesc ? ' · ' + escapeHtml(p.recentDesc).slice(0, 50) : '') + '</div></td>' +
        '<td class="amt">' + p.count + '</td>' +
        '<td class="amt">' + fmt(p.total) + '</td>' +
        '<td><select class="cat-select bulk-cat" data-pkey="' + escapeHtml(p.k) + '" style="max-width: 160px;">' + catOpts + '</select></td>';
      body.appendChild(tr);
    });

    body.querySelectorAll('.bulk-cat').forEach(sel => {
      sel.addEventListener('change', () => {
        const pkey = sel.dataset.pkey;
        const newCat = sel.value;
        let touched = 0;
        (state.transactions||[]).forEach(t => {
          const tk = payeeKey(t.payee) || (t.payee||'(none)').toLowerCase();
          if (tk === pkey) { t.category = newCat; touched++; }
        });
        state.payeeOverrides[pkey] = newCat;
        saveState();
        render();
      });
    });
  }

  // (Per-category budgets UI and renderBudgets/spendingByCategoryYtd were retired)

  // ========== Amortization rules editor ==========
  function renderAmortRules() {
    const body = document.getElementById('amortBody');
    if (!body) return;
    // Show all categories that aren't excluded/oneOff
    const cats = CATEGORIES.filter(c => !c.excluded && !c.oneOff && c.name !== 'Other');
    body.innerHTML = '';
    let activeCount = 0;
    cats.forEach(c => {
      const months = spreadFor(c.name);
      if (months > 1) activeCount++;
      const isDefault = (state.categorySpread || {})[c.name] == null;
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + escapeHtml(c.name) + '</td>' +
        '<td style="text-align:right;"><input type="number" class="num amort-input" data-cat="' + escapeHtml(c.name) + '" value="' + months + '" min="1" max="24" step="1" style="width: 70px;"></td>' +
        '<td style="font-size:11px; color:#9ca3af;">' + (isDefault ? (DEFAULT_SPREAD[c.name] ? 'default ' + DEFAULT_SPREAD[c.name] : 'no spread') : 'custom') + '</td>';
      body.appendChild(tr);
    });
    body.querySelectorAll('.amort-input').forEach(inp => {
      inp.addEventListener('change', () => {
        const v = parseInt(inp.value, 10);
        const cat = inp.dataset.cat;
        state.categorySpread = state.categorySpread || {};
        // Match default → remove override; otherwise save
        const def = DEFAULT_SPREAD[cat] || 1;
        if (isNaN(v) || v < 1 || v === def) delete state.categorySpread[cat];
        else state.categorySpread[cat] = v;
        saveState(); render();
      });
    });
    document.getElementById('amortRulesCount').textContent = activeCount + ' active';
  }

  function renderLearnedRules() {
    const entries = Object.entries(state.payeeOverrides || {}).filter(([, v]) => v != null);
    document.getElementById('rulesCount').textContent = entries.length;
    const body = document.getElementById('rulesBody');
    body.innerHTML = '';
    document.getElementById('rulesEmpty').style.display = entries.length === 0 ? '' : 'none';
    document.getElementById('rulesTable').style.display = entries.length === 0 ? 'none' : '';
    entries.sort((a, b) => a[0].localeCompare(b[0]));
    entries.forEach(([k, cat]) => {
      // Find a representative payee name from transactions
      const sample = (state.transactions||[]).find(t => (payeeKey(t.payee) || (t.payee||'(none)').toLowerCase()) === k);
      const displayName = sample ? sample.payee : k;
      const tr = document.createElement('tr');
      const catOpts = CATEGORIES.map(c => '<option value="' + c.name + '"' + (c.name === cat ? ' selected' : '') + '>' + c.name + ((c.excluded ? ' (excl)' : c.oneOff ? ' (one-off)' : '')) + '</option>').join('');
      tr.innerHTML =
        '<td><div class="payee">' + escapeHtml(displayName) + '</div><div class="src">' + escapeHtml(k) + '</div></td>' +
        '<td><select class="cat-select rule-cat" data-pkey="' + escapeHtml(k) + '">' + catOpts + '</select></td>' +
        '<td><button class="del rule-del" data-pkey="' + escapeHtml(k) + '" title="Remove rule">×</button></td>';
      body.appendChild(tr);
    });
    body.querySelectorAll('.rule-cat').forEach(sel => {
      sel.addEventListener('change', () => {
        state.payeeOverrides[sel.dataset.pkey] = sel.value;
        // Also update existing transactions
        (state.transactions||[]).forEach(t => {
          const tk = payeeKey(t.payee) || (t.payee||'(none)').toLowerCase();
          if (tk === sel.dataset.pkey) t.category = sel.value;
        });
        saveState(); render();
      });
    });
    body.querySelectorAll('.rule-del').forEach(btn => {
      btn.addEventListener('click', () => {
        // Tombstone, not delete — cross-device sync unions rule maps, and only an explicit
        // null stops the other device's copy re-adding the rule on its next sync.
        state.payeeOverrides[btn.dataset.pkey] = null;
        saveState(); render();
      });
    });
  }




  // ========== Sources / accounts ==========

  function renderSourcesUI() {
    // Populate source filter dropdowns
    const untaggedCount = (state.transactions||[]).filter(t => !t.source && t.amount < 0).length;
    ['txSrcFilter','bulkSrcFilter'].forEach(id => {
      const sel = document.getElementById(id); if (!sel) return;
      const prev = sel.value || 'all';
      const untaggedOpt = untaggedCount > 0 ? '<option value="__untagged__">— Untagged (' + untaggedCount + ') —</option>' : '';
      sel.innerHTML = '<option value="all">All</option>' + untaggedOpt + (state.sources||[]).map(s => '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>').join('');
      if (Array.from(sel.options).some(o => o.value === prev)) sel.value = prev;
    });

    // Untagged banner
    const banner = document.getElementById('untaggedBanner');
    if (banner) {
      banner.style.display = untaggedCount > 0 ? '' : 'none';
      const lbl = document.getElementById('untaggedBannerCount');
      if (lbl) lbl.textContent = untaggedCount + ' untagged transaction' + (untaggedCount !== 1 ? 's' : '');
    }
    const btn2 = document.getElementById('bulkTagBtn2');
    if (btn2 && !btn2.dataset.wired) {
      btn2.dataset.wired = '1';
      btn2.addEventListener('click', function() {
        const acct = document.getElementById('bulkTagSel2').value;
        (state.transactions||[]).forEach(t => { if (!t.source) t.source = acct; });
        saveState(); render();
      });
    }
    const filterBtn = document.getElementById('filterUntaggedBtn');
    if (filterBtn && !filterBtn.dataset.wired) {
      filterBtn.dataset.wired = '1';
      filterBtn.addEventListener('click', function() {
        const sel = document.getElementById('txSrcFilter');
        if (sel) { sel.value = '__untagged__'; sel.dispatchEvent(new Event('change')); }
        const txDetails = document.getElementById('txTable')?.closest('details');
        if (txDetails) txDetails.open = true;
        document.getElementById('txTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }

  }

  // ========== Transactions ==========
  function buildCatFilter() {
    const sel = document.getElementById('txCatFilter');
    const prev = sel.value || 'all';
    sel.innerHTML = '<option value="all">All</option>' + CATEGORIES.map(c => '<option value="' + c.name + '">' + c.name + ((c.excluded ? ' (excl)' : c.oneOff ? ' (one-off)' : '')) + '</option>').join('');
    sel.value = prev;
  }

  function renderTransactions() {
    const filt = document.getElementById('txFilter').value;
    const catFilt = document.getElementById('txCatFilter').value;
    const typeFilt = document.getElementById('txTypeFilter').value;
    const srcFilt = document.getElementById('txSrcFilter').value;
    let startDate;
    const today = todayISO();
    if (filt === 'recent') startDate = daysAgoISO(28);
    else if (filt === 'month') { const d = new Date(); startDate = d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-01'; }
    else if (filt === 'ytd') startDate = startOfYearISO();
    else if (filt === 'fortnight') {
      startDate = currentFortnight().fnStartISO;
    }
    else startDate = '0000-01-01';

    let txs = (state.transactions||[]).filter(t => t.date >= startDate && t.date <= today);
    if (catFilt !== 'all') txs = txs.filter(t => t.category === catFilt);
    if (typeFilt === 'expense') txs = txs.filter(t => t.amount < 0 && !EXCLUDED_CATS.has(t.category) && !t.excluded);
    else if (typeFilt === 'excluded') txs = txs.filter(t => EXCLUDED_CATS.has(t.category) || t.excluded);
    else if (typeFilt === 'uncat') txs = txs.filter(t => t.category === 'Other' && t.amount < 0 && !t.excluded);
    if (srcFilt === '__untagged__') txs = txs.filter(t => !t.source);
    else if (srcFilt !== 'all') txs = txs.filter(t => (t.source||'') === srcFilt);
    const search = document.getElementById('txSearch').value.trim().toLowerCase();
    if (search) txs = txs.filter(t => ((t.payee || '') + ' ' + (t.description || '')).toLowerCase().includes(search));
    txs.sort((a,b) => b.date.localeCompare(a.date) || (b.id||'').localeCompare(a.id||''));

    document.getElementById('txCount').textContent = txs.length + ' shown';
    // Reimbursable running balance: net of all Reimbursable rows. Negative = still owed to you
    // (expense halves outweigh repayments); ~0 = settled. Computed across ALL transactions.
    const owedEl = document.getElementById('reimbOwed');
    if (owedEl) {
      const net = (state.transactions || []).reduce((s, t) => t.category === 'Reimbursable' ? s + (t.amount || 0) : s, 0);
      if (Math.abs(net) < 0.005) { owedEl.style.display = 'none'; }
      else { owedEl.style.display = ''; owedEl.textContent = 'owed to you ' + fmt(-net); }
    }
    const body = document.getElementById('txBody');
    body.innerHTML = '';
    const empty = document.getElementById('txEmpty');
    const table = document.getElementById('txTable');
    if (txs.length === 0) { empty.style.display = ''; table.style.display = 'none'; return; }
    empty.style.display = 'none'; table.style.display = '';
    // Cap only to protect the DOM against pathologically large lists; high enough to show the
    // full history in practice. The count label reflects the true filtered total.
    const CAP = 5000;
    if (txs.length > CAP) txs = txs.slice(0, CAP);
    txs.forEach(t => {
      const tr = document.createElement('tr');
      tr.className = (t.excluded || EXCLUDED_CATS.has(t.category)) ? 'excluded' : '';
      const inOut = t.amount > 0 ? 'in' : '';
      const catOpts = CATEGORIES.map(c => '<option value="' + c.name + '"' + (c.name === t.category ? ' selected' : '') + '>' + c.name + ((c.excluded ? ' (excl)' : c.oneOff ? ' (one-off)' : '')) + '</option>').join('');
      const srcOpts = [''].concat(STANDARD_ACCOUNTS).map(a => '<option value="' + escapeHtml(a) + '"' + (a === (t.source||'') ? ' selected' : '') + '>' + (a || '—') + '</option>').join('');
      tr.innerHTML =
        '<td colspan="5"><details class="tx-entry"><summary>' +
          '<span class="tx-date">' + escapeHtml(t.date.slice(5).split('-').reverse().join('/')) + '</span>' +
          '<span class="tx-name">' + escapeHtml(t.payee || '(no payee)') + '</span>' +
          '<span class="tx-value ' + inOut + '">' + fmt(t.amount) + '</span></summary>' +
          '<div class="tx-edit"><div class="desc">' + escapeHtml(t.date + ' · ' + (t.description || t.payee || '')) + '</div>' +
          '<label>Category<select class="cat-select" data-id="' + t.id + '">' + catOpts + '</select></label>' +
          '<label>Account<select class="src-select" data-id="' + t.id + '">' + srcOpts + '</select></label>' +
          '<div class="tx-row-actions">' +
          ((t.amount < 0 && t.category !== 'Reimbursable') ? '<button class="split-reimb" data-id="' + t.id + '" title="Split 50/50 — keep half in its category, move half to Reimbursable">Split 50/50</button>' : '') +
          '<button class="del" data-id="' + t.id + '" title="Delete">Delete</button></div></div></details></td>';
      body.appendChild(tr);
    });
    body.querySelectorAll('.cat-select').forEach(sel => {
      sel.addEventListener('change', () => {
        const t = state.transactions.find(x => x.id === sel.dataset.id); if (!t) return;
        t.category = sel.value;
        // Don't learn a payee rule for Reimbursable — it's a per-transaction, manual tag (e.g. a
        // shared cost or a one-off repayment from Sarah), not a stable payee→category mapping.
        if (t.payee && sel.value !== 'Reimbursable') state.payeeOverrides[payeeKey(t.payee)] = sel.value;
        saveState(); render();
      });
    });
    body.querySelectorAll('.split-reimb').forEach(btn => {
      btn.addEventListener('click', () => {
        const t = state.transactions.find(x => x.id === btn.dataset.id); if (!t || (t.amount || 0) >= 0) return;
        if (!confirm('Split 50/50 — keep half as "' + t.category + '", move the other half to Reimbursable?')) return;
        const orig = t.amount;
        const keepHalf = Math.round(orig / 2 * 100) / 100;
        const reimbHalf = Math.round((orig - keepHalf) * 100) / 100;
        // New row carries Sarah's half; original keeps its category with half the amount.
        state.transactions.push(Object.assign({}, t, { id: t.id + '_reimb', amount: reimbHalf, category: 'Reimbursable' }));
        t.amount = keepHalf;
        saveState(); render();
      });
    });
    body.querySelectorAll('.src-select').forEach(sel => {
      sel.addEventListener('change', () => {
        const t = state.transactions.find(x => x.id === sel.dataset.id); if (!t) return;
        t.source = sel.value;
        saveState(); render();
      });
    });
    body.querySelectorAll('.del').forEach(btn => {
      btn.addEventListener('click', () => {
        state.transactions = state.transactions.filter(x => x.id !== btn.dataset.id);
        saveState(); render();
      });
    });
  }

  // ========== Spending tab: status strip + recent activity + where it's going ==========

  function shortDateLabel(iso) {
    const todayStr = todayISO();
    if (iso === todayStr) return 'Today';
    const y = new Date(); y.setDate(y.getDate() - 1);
    if (iso === y.getFullYear() + '-' + pad2(y.getMonth() + 1) + '-' + pad2(y.getDate())) return 'Yesterday';
    const d = parseISO(iso);
    const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return DOW[d.getDay()] + ' ' + d.getDate() + ' ' + MON[d.getMonth()];
  }

  // Opens the detail/tools block + Transactions panel with the given filters preset.
  function openTxPanel(opts) {
    opts = opts || {};
    document.getElementById('transactionReview').open = true;
    const setVal = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined) el.value = v; };
    setVal('txSearch', '');
    setVal('txFilter', opts.filter || 'recent');
    setVal('txCatFilter', opts.cat || 'all');
    setVal('txTypeFilter', opts.type || 'expense');
    setVal('txSrcFilter', 'all');
    const detailsEl = document.getElementById('txTable') && document.getElementById('txTable').closest('details');
    if (detailsEl) detailsEl.open = true;
    safeCall('renderTransactions', renderTransactions);
    if (detailsEl) detailsEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function fmtDayMon(iso) {
    const d = parseISO(iso);
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getDate() + ' ' + MON[d.getMonth()];
  }

  // Fortnight-equivalent budget for a category, or null when fortnight pacing is
  // meaningless. Pot-funded categories are null: mapped to Accruals, or quarterly/annual
  // WITHOUT a spending-account mapping (same convention as the accruals pot — an annual
  // forecast on a Living Well category like Subscriptions $1,000/yr is a real ~$38/fn
  // budget, not a pot bill). Otherwise explicit forecasts convert per-fortnight, else
  // inferred from since-baseline spend BEFORE the window so the current fortnight doesn't
  // grade itself.
  function categoryFnBudget(cat, fnStartISO) {
    const o = (state.categoryAnnualForecast || {})[cat];
    const mapped = (state.categoryAccount || {})[cat] || '';
    if (mapped === 'Accruals') return null;
    if (o != null) {
      const shape = typeof o === 'number' ? { amount: o, period: 'year' } : { amount: +o.amount || 0, period: o.period || 'year' };
      if (shape.amount <= 0) return null;
      const lumpy = shape.period === 'quarter' || shape.period === 'year';
      if (lumpy && (mapped === '' || mapped === 'Essentials')) return null;  // pot bill
      const PPY = { fortnight: 26, month: 12, quarter: 4, year: 1 };
      return shape.amount * (PPY[shape.period] || 1) / 26;
    }
    const startISO = baselineISO();
    const days = Math.round((parseISO(fnStartISO) - parseISO(startISO)) / 86400000);
    if (days < 28) return null;  // not enough history to infer a typical fortnight
    let spent = 0;
    (state.transactions || []).forEach(t => {
      if (t.category !== cat || t.amount >= 0 || t.excluded) return;
      if (t.date < startISO || t.date >= fnStartISO) return;
      spent += -t.amount;
    });
    if (spent <= 0) return null;
    return spent * 14 / days;
  }

  // Status strip: the Living Well hero card, anchored to the pay-aligned
  // fortnight. The 12-month run-rate card beside it is filled by renderForecast via the
  // fcTotal/fcBar/forecastStatus ids.
  // Fortnight spending budget. Living Well is now the sole spending account — Essentials
  // was folded into it Aug 2026. Used by the hero card and the feed's on-track colouring.
  function combinedFnBudget() {
    const funding = state.accountFortnightly || {};
    return (+funding['Living Well'] || 0);
  }

  function renderStatusStrip() {
    const fnCard = document.getElementById('stFnCard');
    if (!fnCard) return;
    const todayStr = todayISO();
    const { fnStartISO, fnDayIndex } = currentFortnight();
    const day = Math.min(14, Math.max(1, fnDayIndex));

    // --- Spending hero: Living Well vs an even-pace marker (Essentials folded in Aug 2026) ---
    const funding = state.accountFortnightly || {};
    const lwFn = +funding['Living Well'] || 0;
    const fn = lwFn;
    // Computed core spend — fallback when the live balance isn't synced
    let fnSpent = 0;
    (state.transactions || []).forEach(t => {
      const src = t.source || '';
      if (src !== 'Living Well') return;
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < fnStartISO || t.date > todayStr) return;
      fnSpent += -t.amount;
    });
    // Prefer the live balance when synced — same logic as the old per-account cards.
    const lwInfo = acctBalanceInfo('Living Well');
    let drawn, remaining, splitTxt;
    if (lwInfo.hasBalance && fn > 0) {
      drawn = Math.max(0, lwFn - lwInfo.amount);
      remaining = lwInfo.amount;
      splitTxt = '';
    } else {
      drawn = fnSpent; remaining = fn - fnSpent;
      splitTxt = '';
    }
    // Family payments and the Giving direct debits are fixed fortnightly bank transfers, not
    // variable spend. Front-load them in the expected pace so the bar isn't amber/red the
    // moment they land.
    let day1Offset = 0;
    (state.transactions || []).forEach(t => {
      if ((t.category !== 'Family payments' && !(t.category || '').startsWith('Giving')) || t.amount >= 0 || t.excluded) return;
      if (t.date < fnStartISO || t.date > todayStr) return;
      day1Offset += -t.amount;
    });
    day1Offset = Math.min(day1Offset, fn);
    const expected = day1Offset + Math.max(0, fn - day1Offset) * day / 14;
    const pct = fn > 0 ? Math.min(100, drawn / fn * 100) : 0;
    const expPct = fn > 0 ? Math.min(100, expected / fn * 100) : 0;
    const fnCls = fn <= 0 ? '' : drawn <= expected * 1.02 ? 'green' : drawn <= fn ? 'amber' : 'red';
    const fillCol = fnCls === 'red' ? '#ef4444' : fnCls === 'amber' ? '#f59e0b' : '#10b981';
    const paceTxt = fn <= 0 ? '' : drawn <= expected * 1.02 ? 'on pace' : drawn <= fn ? 'ahead of pace' : 'over budget';
    fnCard.className = 'status-card ' + fnCls;
    fnCard.innerHTML =
      '<div class="label">Spending · day ' + day + '/14 · since payday ' + fmtDayMon(fnStartISO) + '</div>' +
      '<div class="value">' + fmt(remaining) + ' <span style="font-size:13px; font-weight:normal; color:var(--text4);">left</span></div>' +
      '<div class="bar">' +
        '<div style="position:absolute; left:0; top:0; bottom:0; width:' + pct + '%; background:' + fillCol + '; border-radius:5px;"></div>' +
        '<div style="position:absolute; top:0; bottom:0; left:' + expPct + '%; width:2px; background:var(--text); opacity:0.5;" title="Even-pace marker for day ' + day + '"></div>' +
      '</div>' +
      '<div class="sub" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' +
        fmt(drawn) + ' of ' + fmt(fn) + ' spent' + (paceTxt ? ' · ' + paceTxt : '') +
        (splitTxt ? ' · ' + splitTxt : '') + '</div>';

    // --- Subtle Savings line (sits beside the Accruals line) ---
    const savSubtle = document.getElementById('savingsSubtle');
    if (savSubtle) {
      const sav = acctBalanceInfo('Savings');
      savSubtle.innerHTML = sav.hasBalance
        ? 'Sav <strong style="color:var(--text2); font-weight:600;">' + fmtCompact(sav.amount) + '</strong>' + freeWithdrawalLock('Savings')
        : '';
    }

    // --- Subtle Accruals line: just the balance. ("vs pot" was misleading and the
    // next-bill projection was dropped too — the accruals table under detail & tools
    // has the full picture when needed.) ---
    const subtle = document.getElementById('accrualsSubtle');
    if (subtle) {
      const accInfo = acctBalanceInfo('Accruals');
      subtle.innerHTML = accInfo.hasBalance
        ? 'Accr <strong style="color:var(--text2); font-weight:600;">' + fmtCompact(accInfo.amount) + '</strong>' + freeWithdrawalLock('Accruals')
        : '';
    }
  }

  // Recent activity feed — spending since the last payday (pay-aligned fortnight), grouped
  // by day. Category chips are clickable to recategorise in place (learns the payee rule).
  function renderRecentActivity() {
    const wrap = document.getElementById('recentActivity');
    if (!wrap) return;
    const { fnStartISO } = currentFortnight();
    const startISO = fnStartISO, todayStr = todayISO();
    // Credits imported with no payee rule land in the excluded "Income" category, which
    // used to hide refunds (e.g. Oura). Show all money-in except transfers/investing/tax
    // and the salary itself (it has its own payday ritual).
    const HIDE_CREDIT_CATS = new Set(['Transfer', 'Investing', 'Tax']);
    const isSalary = t => /bnz salar/i.test((t.description || '') + ' ' + (t.payee || ''));
    const rows = (state.transactions || []).filter(t => {
      if (t.date < startISO || t.date > todayStr || t.excluded || t.amount === 0) return false;
      if (t.amount < 0) return !EXCLUDED_CATS.has(t.category);
      return !HIDE_CREDIT_CATS.has(t.category) && !isSalary(t);
    });
    rows.sort((a, b) => b.date.localeCompare(a.date) || (b.id || '').localeCompare(a.id || ''));
    const spendTotal = rows.reduce((s, t) => t.amount < 0 ? s + -t.amount : s, 0);
    setText('raSummary', 'since payday ' + fmtDayMon(startISO) + ' · ' + fmt(spendTotal) + ' spent');
    if (rows.length === 0) {
      wrap.innerHTML = '<div class="tx-empty">No spending since payday yet.</div>';
      return;
    }
    // No tag for Living Well — it's the default account, so tagging every row is noise;
    // only the exceptions (other accounts) get a marker.
    // Per-category pace colouring: an expense row is red when ITS CATEGORY's spend so far
    // this fortnight exceeds its allowance through the END of the current week — half the
    // fortnight budget in week 1, the full budget in week 2. Day-level pro-rating flagged
    // every normal weekly grocery shop red for the rest of the day it landed; spending in
    // these categories is lumpy-weekly, not daily. Pot-funded categories (quarterly/annual
    // override, or mapped to Accruals), one-offs, credits, and categories with no usable
    // budget stay neutral.
    const day = Math.min(14, Math.max(1, currentFortnight().fnDayIndex));
    const _catStatus = {};
    const statusFor = cat => {
      if (cat in _catStatus) return _catStatus[cat];
      let st = null;
      // Only behavioural (Living Well) categories get pace-graded. Essentials categories
      // are scheduled bills paid as lumps (Family payments, Rates, Health) — pro-rating
      // flags them red for being paid on time, and the colour can't drive behaviour.
      // categoryFnBudget returns null for pot-funded categories, so they stay neutral too.
      if ((state.categoryAccount || {})[cat] === 'Living Well' &&
          !ONEOFF_CATS.has(cat) && !EXCLUDED_CATS.has(cat)) {
        const budget = categoryFnBudget(cat, startISO);
        if (budget != null && budget > 0) {
          let spent = 0;
          (state.transactions || []).forEach(t => {
            if (t.category !== cat || t.excluded) return;
            if (t.date < startISO || t.date > todayStr) return;
            spent += -t.amount;   // refunds reduce the category's spend
          });
          const week = day <= 7 ? 1 : 2;
          const allowed = budget * (week === 1 ? 0.5 : 1);
          st = { cls: spent <= allowed * 1.02 ? 'ok' : 'over', spent, allowed, week, budget };
        }
      }
      _catStatus[cat] = st;
      return st;
    };
    const SRC_TAG = { 'Essentials': 'ESS', 'Accruals': 'ACC', 'Savings': 'SAV' };
    const MAXROWS = 15;
    const shown = rows.slice(0, MAXROWS);
    let html = '', curDay = null;
    shown.forEach(t => {
      if (t.date !== curDay) {
        if (curDay !== null) html += '</div>';
        curDay = t.date;
        const dayTotal = rows.reduce((s, x) => (x.date === t.date && x.amount < 0) ? s + -x.amount : s, 0);
        html += '<div class="ra-day"><div class="ra-day-head"><span>' + shortDateLabel(t.date) + '</span><span>' + (dayTotal > 0 ? fmt(dayTotal) : '') + '</span></div>';
      }
      const credit = t.amount > 0;
      const src = SRC_TAG[t.source || ''] || '';
      const st = credit ? null : statusFor(t.category);
      const stTitle = st
        ? escapeHtml(t.category) + ': ' + fmt(st.spent) + ' this fortnight vs ' + fmt(st.allowed) +
          ' allowed through week ' + st.week + ' (' + fmt(st.budget) + '/fn)'
        : '';
      html += '<div class="ra-row' + (st ? ' ' + st.cls : '') + '"' + (stTitle ? ' title="' + stTitle + '"' : '') + '>' +
        '<span class="ra-payee" title="' + escapeHtml(t.description || '') + '">' + escapeHtml(t.payee || '(no payee)') +
          (src ? '<span class="ra-src">' + src + '</span>' : '') + '</span>' +
        '<button class="ra-cat' + (t.category === 'Other' ? ' other' : '') + '" data-id="' + t.id + '" title="Click to change category">' + escapeHtml(t.category) + '</button>' +
        '<span class="ra-amt' + (credit ? ' credit' : '') + '">' + (credit ? '+' : '−') + fmt(Math.abs(t.amount)) + '</span>' +
      '</div>';
    });
    html += '</div>';
    html += '<div style="margin-top:10px; text-align:right;">' +
      (rows.length > MAXROWS ? '<span style="font-size:11px; color:var(--text4); margin-right:8px;">+' + (rows.length - MAXROWS) + ' more</span>' : '') +
      '<a href="#" id="raShowAll" style="font-size:12px; color:#2563eb; text-decoration:none;">All transactions →</a></div>';
    wrap.innerHTML = html;

    wrap.querySelectorAll('.ra-cat').forEach(btn => btn.addEventListener('click', () => {
      const t = (state.transactions || []).find(x => x.id === btn.dataset.id); if (!t) return;
      const sel = document.createElement('select');
      sel.className = 'cat-select';
      sel.innerHTML = CATEGORIES.map(c => '<option value="' + c.name + '"' + (c.name === t.category ? ' selected' : '') + '>' + c.name + (c.excluded ? ' (excl)' : c.oneOff ? ' (one-off)' : '') + '</option>').join('');
      btn.replaceWith(sel);
      sel.focus();
      sel.addEventListener('change', () => {
        t.category = sel.value;
        if (t.payee && sel.value !== 'Reimbursable') state.payeeOverrides[payeeKey(t.payee)] = sel.value;
        saveState(); render();
      });
      sel.addEventListener('blur', () => safeCall('renderRecentActivity', renderRecentActivity));
    }));
    const showAll = document.getElementById('raShowAll');
    if (showAll) showAll.addEventListener('click', e => { e.preventDefault(); openTxPanel({ filter: 'fortnight' }); });
  }

  // Where it's going — per-category raw spend for the current pay fortnight, shown as
  // plain "spent / fortnight budget" numbers (budget omitted when none applies). One-off
  // spend shows as a footnote.
  function renderWhereItGoes() {
    const wrap = document.getElementById('whereItGoes');
    if (!wrap) return;
    const { fnStartISO } = currentFortnight();
    const startISO = fnStartISO;
    const endISO = todayISO();

    const catSpend = {};
    let oneOff = 0, total = 0;
    (state.transactions || []).forEach(t => {
      if (t.excluded || EXCLUDED_CATS.has(t.category) || t.amount >= 0) return;
      if (t.date < startISO || t.date > endISO) return;
      if (ONEOFF_CATS.has(t.category)) { oneOff += -t.amount; return; }
      catSpend[t.category] = (catSpend[t.category] || 0) + -t.amount;
      total += -t.amount;
    });
    setText('wigLabel', 'since payday ' + fmtDayMon(startISO) + ' · ' + fmt(total));

    const entries = Object.entries(catSpend).sort((a, b) => b[1] - a[1]);
    if (entries.length === 0 && oneOff === 0) {
      wrap.innerHTML = '<div class="tx-empty">No spending recorded for this fortnight.</div>';
      return;
    }
    const TOP_N = 8;
    const top = entries.slice(0, TOP_N);
    const rest = entries.slice(TOP_N);
    const restSum = rest.reduce((s2, x) => s2 + x[1], 0);
    let html = top.map(([cat, amt]) => {
      const budget = categoryFnBudget(cat, startISO);
      // ~ marks budgets inferred from history (vs any explicit forecast)
      const inferred = budget != null && (state.categoryAnnualForecast || {})[cat] == null;
      const budgetTitle = budget == null ? '' :
        (inferred ? ' · ~' + fmt(budget) + '/fn inferred from since-baseline average' : ' · ' + fmt(budget) + '/fn from your forecast');
      return '<div class="wig-row" data-cat="' + escapeHtml(cat) + '" title="Show ' + escapeHtml(cat) + ' transactions' + budgetTitle + '">' +
        '<span class="wig-name" style="flex:1; width:auto;">' + escapeHtml(cat) + '</span>' +
        '<span class="wig-amt" style="width:auto;">' + fmt(amt) +
          (budget != null ? ' <span style="color:var(--text4); font-weight:normal;">/ ' + (inferred ? '~' : '') + fmt(budget) + '</span>' : '') +
        '</span>' +
      '</div>';
    }).join('');
    if (restSum > 0) {
      html += '<div class="wig-row" style="cursor:default;" title="">' +
        '<span class="wig-name" style="flex:1; width:auto; color:var(--text4);">Other ' + rest.length + ' cats</span>' +
        '<span class="wig-amt" style="width:auto; color:var(--text4);">' + fmt(restSum) + '</span>' +
      '</div>';
    }
    if (oneOff > 0) {
      html += '<div style="font-size:11px; color:var(--text4); margin-top:8px;">+ ' + fmt(oneOff) + ' one-off (Renovation / Vehicle / Legal) — excluded from core totals</div>';
    }
    wrap.innerHTML = html;
    wrap.querySelectorAll('.wig-row[data-cat]').forEach(row => row.addEventListener('click', () => {
      openTxPanel({ cat: row.dataset.cat, filter: 'fortnight' });
    }));
  }

  // ========== Income / saving rate ==========

  // (renderNetWorthProjection removed — projection UI was retired earlier; function was orphaned)

  // ========== Event timeline ==========
  // (renderEventTimeline removed — the event-timeline UI was retired; function was orphaned.)

  // (B2 deployment schedule removed Sep 2026 — it staged a 3-tranche DCA from the Simplicity
  // Cash Fund into Balanced. The Cash Fund is wound down and the consolidation moved the money
  // in one step, so there is nothing left to stage.)

  // ========== 12-month rolling forecast ==========
  function renderForecast() {
    const empty = document.getElementById('forecastEmpty');
    const content = document.getElementById('forecastContent');
    if (!empty || !content) return;
    const txCount = (state.transactions||[]).length;
    if (txCount === 0) { empty.style.display = ''; content.style.display = 'none'; }
    empty.style.display = 'none'; content.style.display = '';

    // Rolling 12-month annualized view: project the next 12 months based on observed rate
    // since baseline. Compare to the flat $142K post-retirement target.
    //   - "Since-baseline" spend is what's actually flowed through (the data signal).
    //   - For auto-inferred categories: forecast = daily_rate × 365 (annualize the run rate).
    //   - For overrides: amount × periods_per_year. Annual × 1, quarterly × 4, etc.
    //   - "To stay on $142K" is computed over the remaining days in a 12-month window.
    const { startISO, daysElapsed, ANNUAL_DAYS, daysRemaining, fortnightsRemaining, monthsElapsed, monthsRemaining, catRows, totalYtd, totalForecast, totalRemaining, gap, avgPerMonth, remainingBudget, monthlyBudget, target } = currentForecast;

    // Status colour — derived from how far over/under the $142K target the year-end forecast lands.
    const warnThreshold = target * 0.05;  // ±5% of target
    let statusColor, statusBg, statusLabel, statusClass;
    // Short labels — this renders in the subtle one-line strip, so every character counts
    if (gap <= -warnThreshold) {       statusColor = '#047857'; statusBg = '#10b981'; statusLabel = 'on track'; statusClass = 'green'; }
    else if (gap <= 0)             {   statusColor = '#047857'; statusBg = '#10b981'; statusLabel = 'on track'; statusClass = 'green'; }
    else if (gap <= warnThreshold) {   statusColor = '#b45309'; statusBg = '#f59e0b'; statusLabel = 'slightly over'; statusClass = 'amber'; }
    else                           {   statusColor = '#b91c1c'; statusBg = '#ef4444'; statusLabel = 'over budget'; statusClass = 'red'; }

    const statusPill = document.getElementById('forecastStatus');
    if (statusPill) { statusPill.textContent = statusLabel; statusPill.className = 'pill ' + statusClass; }
    const panel = document.getElementById('forecastPanel');
    if (panel) panel.style.borderLeftColor = statusBg;

    setText('fcTotal', fmtCompact(totalForecast));
    { const e = document.getElementById('fcTotal'); if (e) e.style.color = statusColor; }
    const ytdLabel = baselineIsSet() ? baselineLabel() : 'YTD';
    const overUnder = target - totalForecast;
    setText('fcTotalSub', '· ' +
      (overUnder >= 0 ? fmtCompact(overUnder) + ' under' : fmtCompact(-overUnder) + ' over'));
    setText('fcCatYtdHead', ytdLabel);
    // Prominent bar: fill = forecast, marker = budget. Under budget → bar stops short of the
    // marker (green); over → bar extends past it (red). Both scaled to the larger of the two.
    { const bar = document.getElementById('fcBar');
      const marker = document.getElementById('fcTargetMarker');
      const scaleMax = Math.max(totalForecast, target) || 1;
      if (bar) { bar.style.width = (totalForecast / scaleMax * 100) + '%'; bar.style.background = statusBg; }
      if (marker) marker.style.left = (target / scaleMax * 100) + '%';
    }


    // Predicted monthly spend (kept for internal advice text)
    const predictedMonthly = monthsRemaining > 0 ? totalRemaining / monthsRemaining : 0;

    // To stay on $142K — convert remaining budget to per-fortnight rate over the rest of the 12-month window
    setText('fcBudgetLabel', 'To stay on $142K');
    const fortnightlyBudgetVal = fortnightsRemaining > 0 ? remainingBudget / fortnightsRemaining : 0;
    const fcBudgetEl = document.getElementById('fcBudget');
    if (remainingBudget < 0) {
      setText('fcBudget', '$0');
      if (fcBudgetEl) fcBudgetEl.style.color = '#b91c1c';
      setText('fcBudgetSub', 'already over by ' + fmtCompact(-remainingBudget));
    } else if (fortnightsRemaining <= 0) {
      setText('fcBudget', '—');
      setText('fcBudgetSub', '12-month window full');
    } else {
      setText('fcBudget', fmt(fortnightlyBudgetVal));
      const avgFortnight = predictedMonthly * 12 / 26;
      if (fcBudgetEl) fcBudgetEl.style.color = fortnightlyBudgetVal < avgFortnight ? '#b91c1c' : '#047857';
      setText('fcBudgetSub', fortnightsRemaining.toFixed(1) + ' fortnights left in 12-month window');
    }

    // Avg fortnight — actual raw core spend. When a baseline is set, use the since-baseline
    // window (otherwise we'd be averaging pre-baseline lifestyle into the new tracking period).
    // When no baseline is set, fall back to a rolling 90-day window.
    // Avg fortnight — observed raw spend rate. With < 45 days of data the figure is noisy,
    // so the colour and sublabel get a "low confidence" treatment.
    let avgWindowStart, avgWindowDays, avgWindowLabel, avgWindowSubLabel;
    if (baselineIsSet()) {
      avgWindowStart = baselineISO();
      avgWindowDays = baselineDaysElapsed();
      avgWindowLabel = 'Avg fortnight · ' + baselineLabel();
      avgWindowSubLabel = avgWindowDays + ' day' + (avgWindowDays === 1 ? '' : 's') + ' since baseline · target $' + Math.round(fortnightlyBudgetVal).toLocaleString('en-NZ') + '/fn';
    } else {
      avgWindowStart = daysAgoISO(90);
      avgWindowDays = 90;
      avgWindowLabel = 'Avg fortnight · last 3 mo';
      avgWindowSubLabel = 'over last 90 days · target $' + Math.round(fortnightlyBudgetVal).toLocaleString('en-NZ') + '/fn';
    }
    const windowSpend = spendingInRange(avgWindowStart, todayISO(), { raw: true });
    const avgFortnight = avgWindowDays > 0 ? windowSpend * 14 / avgWindowDays : 0;
    const lowConfidence = avgWindowDays < 45;
    setText('fcAvgFnLabel', avgWindowLabel + (lowConfidence ? ' · low confidence' : ''));
    setText('fcAvgFn', fmt(avgFortnight));
    const fcAvgFnEl = document.getElementById('fcAvgFn');
    if (lowConfidence) {
      // De-emphasize when window is too short to be meaningful
      if (fcAvgFnEl) fcAvgFnEl.style.color = '#9ca3af';
      avgWindowSubLabel = 'only ' + avgWindowDays + ' day' + (avgWindowDays === 1 ? '' : 's') + ' of data — firm up after 45 days';
    } else if (fortnightlyBudgetVal > 0) {
      if (fcAvgFnEl) fcAvgFnEl.style.color = avgFortnight <= fortnightlyBudgetVal ? '#047857' : '#b91c1c';
    } else {
      if (fcAvgFnEl) fcAvgFnEl.style.color = '';
    }
    setText('fcAvgFnSub', avgWindowSubLabel);

    const body = document.getElementById('fcCatBody');
    body.innerHTML = '';
    catRows.forEach(r => {
      const amount = r.isManual ? r.shape.amount : '';
      const period = r.isManual ? r.shape.period : 'year';
      const periodOpts = [
        ['fortnight', 'Fortnightly'],
        ['month', 'Monthly'],
        ['quarter', 'Quarterly'],
        ['year', 'Annual']
      ].map(([v, l]) => '<option value="' + v + '"' + (v === period ? ' selected' : '') + '>' + l + '</option>').join('');
      const acctMap = state.categoryAccount || {};
      const acctSel = acctMap[r.cat] || '';
      const acctOpts = '<option value="">—</option>' + STANDARD_ACCOUNTS.map(a => '<option value="' + a + '"' + (a === acctSel ? ' selected' : '') + '>' + a + '</option>').join('');
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + escapeHtml(r.cat) + '</td>' +
        '<td><select class="fc-cat-acct" data-cat="' + escapeHtml(r.cat) + '" style="font-size: 12px; padding: 3px 6px; border: 1px solid #d1d5db; border-radius: 5px;">' + acctOpts + '</select></td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums;">' + fmt(r.ytdAmt) + '</td>' +
        '<td style="text-align:right; white-space: nowrap;">' +
          '<input type="number" class="num fc-cat-amount" data-cat="' + escapeHtml(r.cat) + '" value="' + amount + '" placeholder="auto" min="0" step="50" style="width: 90px;"> ' +
          '<select class="fc-cat-period" data-cat="' + escapeHtml(r.cat) + '" style="font-size: 12px; padding: 3px 6px; border: 1px solid #d1d5db; border-radius: 5px;">' + periodOpts + '</select>' +
        '</td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap;">' + fmt(r.forecastCat) + (r.isManual ? ' <span class="pill gray" style="font-size:10px;" title="Manual override">manual</span>' : (r.ytdAmt > 0 ? ' <button class="btn sm fc-accept" data-cat="' + escapeHtml(r.cat) + '" data-amount="' + Math.round(r.forecastCat) + '" title="Accept this inferred amount as a manual annual override" style="font-size: 10px; padding: 2px 6px; margin-left: 4px;">use</button>' : ' <span style="color:#9ca3af; font-size:11px;">(auto)</span>')) + '</td>' +
        '<td style="text-align:right; color: #4b5563; font-variant-numeric: tabular-nums;">' + fmt(r.remaining) + '</td>' +
        '<td style="text-align:right; color: #6b7280; font-variant-numeric: tabular-nums;">' + fmt(fortnightsRemaining > 0 ? r.remaining / fortnightsRemaining : 0) + '</td>';
      body.appendChild(tr);
    });

    function commitOverride(cat) {
      const amtEl = body.querySelector('.fc-cat-amount[data-cat="' + CSS.escape(cat) + '"]');
      const perEl = body.querySelector('.fc-cat-period[data-cat="' + CSS.escape(cat) + '"]');
      if (!amtEl || !perEl) return;
      const v = parseFloat(amtEl.value);
      state.categoryAnnualForecast = state.categoryAnnualForecast || {};
      if (isNaN(v) || v < 0 || amtEl.value === '') {
        delete state.categoryAnnualForecast[cat];
      } else {
        state.categoryAnnualForecast[cat] = { amount: v, period: perEl.value };
      }
      saveState(); render();
    }
    body.querySelectorAll('.fc-accept').forEach(btn => btn.addEventListener('click', () => {
      const cat = btn.dataset.cat;
      const amt = +btn.dataset.amount;
      state.categoryAnnualForecast = state.categoryAnnualForecast || {};
      state.categoryAnnualForecast[cat] = { amount: amt, period: 'year' };
      saveState(); render();
    }));
    body.querySelectorAll('.fc-cat-acct').forEach(sel => sel.addEventListener('change', () => {
      const cat = sel.dataset.cat;
      state.categoryAccount = state.categoryAccount || {};
      if (sel.value === '') delete state.categoryAccount[cat];
      else state.categoryAccount[cat] = sel.value;
      saveState(); render();
    }));
    body.querySelectorAll('.fc-cat-amount').forEach(inp => inp.addEventListener('change', () => commitOverride(inp.dataset.cat)));
    // Period changes only commit if there's an amount. Otherwise the dropdown
    // selection stays in the DOM and applies the moment the user types a value.
    body.querySelectorAll('.fc-cat-period').forEach(sel => sel.addEventListener('change', () => {
      const amtEl = body.querySelector('.fc-cat-amount[data-cat="' + CSS.escape(sel.dataset.cat) + '"]');
      if (amtEl && amtEl.value !== '' && !isNaN(parseFloat(amtEl.value))) {
        commitOverride(sel.dataset.cat);
      }
    }));
  }

  // (renderMonthlyTracking removed — monthly summary table was retired, function was orphaned)

  // (renderAccountMismatch removed — category-allocation-check UI was retired)

  // ========== Weekly review (digest + insights + copy-LLM) ==========
  // Tiny baseline date input inline in the 12-month forecast header.
  function renderBaselineInput() {
    const input = document.getElementById('baselineInput');
    if (input) input.value = baselineISO();
  }

  function renderSundayDigest() {
    const panel = document.getElementById('weeklyReview');
    if (!panel) return;
    const today = new Date();
    // Find the most recent Sunday (0 = Sunday). If today is Sunday, this *is* it.
    const sunday = new Date(today); sunday.setHours(0,0,0,0); sunday.setDate(sunday.getDate() - sunday.getDay());
    const sundayISO = sunday.getFullYear() + '-' + pad2(sunday.getMonth()+1) + '-' + pad2(sunday.getDate());
    const todayStr = todayISO();

    // Get transactions added since last Sunday (using date OR importedAt). The panel
    // is a <details> now — visibility is controlled by the user via the disclosure widget.
    const newTx = (state.transactions||[]).filter(t => (t.importedAt && t.importedAt >= sundayISO) || (t.date >= sundayISO && t.date <= todayStr));
    if (newTx.length === 0) {
      document.getElementById('digestSummary').textContent = 'No new transactions since ' + sundayISO + ' — insights below reflect the rolling last 7 days.';
      document.getElementById('digestDetail').innerHTML = '';
      return;
    }

    // Total spent since Sunday (exclude transfers/income/tax/investing/renovation)
    let totalSpend = 0, expenseCount = 0;
    newTx.forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount < 0) { totalSpend += -t.amount; expenseCount++; }
    });

    // New payees — never seen before this Sunday
    const oldPayees = new Set();
    (state.transactions||[]).forEach(t => {
      if (t.date < sundayISO && !(t.importedAt && t.importedAt >= sundayISO)) {
        if (t.payee) oldPayees.add(payeeKey(t.payee));
      }
    });
    const newPayeeMap = {};
    newTx.forEach(t => {
      if (t.amount >= 0) return;
      const k = payeeKey(t.payee);
      if (k && !oldPayees.has(k) && !newPayeeMap[k]) newPayeeMap[k] = { name: t.payee, amount: -t.amount };
    });
    const newPayees = Object.values(newPayeeMap).sort((a,b) => b.amount - a.amount).slice(0, 5);

    // Uncategorised count (in Other)
    const uncategorised = newTx.filter(t => t.category === 'Other' && t.amount < 0).length;

    // Largest single transaction
    let largest = null;
    newTx.forEach(t => {
      if (t.amount >= 0 || EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category)) return;
      if (!largest || -t.amount > -largest.amount) largest = t;
    });

    // Build summary
    const daysSince = Math.round((today - sunday) / 86400000);
    const summary = expenseCount + ' transactions · ' + fmt(totalSpend) + ' spent · ' + daysSince + ' day' + (daysSince === 1 ? '' : 's') + ' since ' + sundayISO;
    document.getElementById('digestSummary').textContent = summary;

    const lines = [];
    if (newPayees.length > 0) {
      lines.push('<strong>New payees:</strong> ' + newPayees.map(p => escapeHtml(p.name) + ' (' + fmt(p.amount) + ')').join(' · '));
    }
    if (uncategorised > 0) {
      lines.push('<strong style="color:#b45309;">' + uncategorised + ' transaction' + (uncategorised === 1 ? '' : 's') + '</strong> in "Other" — review in the Categorise-by-payee panel');
    }
    if (largest) {
      lines.push('<strong>Largest single tx:</strong> ' + escapeHtml(largest.payee||'(no payee)') + ' · ' + fmt(-largest.amount) + ' · ' + escapeHtml(largest.category));
    }
    document.getElementById('digestDetail').innerHTML = lines.join('<br>') || '<span style="color:#9ca3af;">No new payees or uncategorised items to flag.</span>';
  }

  // ========== Weekly review & AI advice ==========
  // Rule-based observations over the last 7 days (compared to prior 7 days), plus a
  // "Copy summary for AI advice" button that builds a markdown brief you can paste
  // into Claude / ChatGPT for personalised analysis. The static HTML artifact can't
  // call an LLM directly, so we surface the structured context for human-in-the-loop.
  function buildWeeklySnapshot() {
    const today = new Date();
    const todayStr = todayISO();
    const week1End = todayStr;
    const week1Start = daysAgoISO(6);              // last 7 days inclusive
    const week2End = daysAgoISO(7);
    const week2Start = daysAgoISO(13);             // prior 7 days
    const year = today.getFullYear();
    const yearStart = year + '-01-01';
    const dayOfYear = Math.floor((today - new Date(year, 0, 1)) / 86400000) + 1;

    // Pay-cycle aligned current fortnight
    const { fnStartISO, fnDayIndex } = currentFortnight();

    // Per-category spend totals for both windows (RAW cash, excluding transfers/income/etc.)
    function isSpend(t) {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return false;
      if (t.amount >= 0) return false;
      return true;
    }
    function inRange(t, s, e) { return t.date >= s && t.date <= e; }

    const txs = state.transactions || [];
    const week1Cat = {}, week2Cat = {};
    let week1Total = 0, week2Total = 0;
    txs.forEach(t => {
      if (!isSpend(t)) return;
      if (inRange(t, week1Start, week1End)) {
        week1Cat[t.category] = (week1Cat[t.category] || 0) + (-t.amount);
        week1Total += -t.amount;
      } else if (inRange(t, week2Start, week2End)) {
        week2Cat[t.category] = (week2Cat[t.category] || 0) + (-t.amount);
        week2Total += -t.amount;
      }
    });

    // Top movers — categories with the biggest week-over-week increase
    const allCats = new Set([...Object.keys(week1Cat), ...Object.keys(week2Cat)]);
    const movers = [];
    allCats.forEach(c => {
      const w1 = week1Cat[c] || 0, w2 = week2Cat[c] || 0;
      const delta = w1 - w2;
      if (Math.abs(delta) >= 50) movers.push({ cat: c, w1, w2, delta });
    });
    movers.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));

    // New payees (never seen before this week)
    const oldPayees = new Set();
    txs.forEach(t => { if (t.date < week1Start && t.payee) oldPayees.add(payeeKey(t.payee)); });
    const newPayeeMap = {};
    txs.forEach(t => {
      if (!isSpend(t)) return;
      if (!inRange(t, week1Start, week1End)) return;
      const k = payeeKey(t.payee);
      if (k && !oldPayees.has(k) && !newPayeeMap[k]) newPayeeMap[k] = { name: t.payee, amount: -t.amount, category: t.category };
    });
    const newPayees = Object.values(newPayeeMap).sort((a,b) => b.amount - a.amount);

    // Uncategorised this week
    const uncategorised = txs.filter(t => isSpend(t) && inRange(t, week1Start, week1End) && t.category === 'Other');

    // Largest single tx this week
    let largest = null;
    txs.forEach(t => {
      if (!isSpend(t) || !inRange(t, week1Start, week1End)) return;
      if (!largest || -t.amount > -largest.amount) largest = t;
    });

    // Per-account fortnight status
    const fortnightly = state.accountFortnightly || {};
    const tracked = STANDARD_ACCOUNTS.filter(a => a !== 'Savings');
    const acctStatus = tracked.map(acct => {
      const budget = +fortnightly[acct] || 0;
      let spent = 0;
      txs.forEach(t => {
        if ((t.source||'') !== acct) return;
        if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
        if (t.amount >= 0) return;
        if (t.date < fnStartISO || t.date > todayStr) return;
        spent += -t.amount;
      });
      return { acct, budget, spent, pct: budget > 0 ? spent / budget : null, fnDayIndex };
    });

    // Spend totals — measured from the user's tracking baseline (defaults to year-start
    // but can be set forward, e.g. May 2026, to exclude pre-baseline lifestyle costs).
    const baselineStart = baselineISO();
    const baselineDaysIn = baselineDaysElapsed();
    const baselineWindowDays = baselineDaysToYearEnd();
    const ytd = spendingInRange(baselineStart, todayStr, { raw: true });
    const ytdAmortized = spendingInRange(baselineStart, todayStr);
    const pace = ytdAmortized * baselineWindowDays / Math.max(1, baselineDaysIn);  // projected total for baseline → year-end

    // Net worth from totalsFromState
    const totals = totalsFromState();

    // Target for the comparison — pro-rated to baseline → year-end if a baseline is set,
    // full $142K otherwise. Always keep the full-year figure for reference too.
    const proratedTarget = baselineIsSet() ? baselineTargetForWindow() : TARGETS.targetSpend;

    return {
      today: todayStr, year, dayOfYear,
      week1Start, week1End, week2Start, week2End,
      week1Total, week2Total, weekDelta: week1Total - week2Total,
      week1Cat, week2Cat, movers,
      newPayees, uncategorised, largest,
      fnStartISO, fnDayIndex, acctStatus,
      ytd, ytdAmortized, pace, target: proratedTarget, fullYearTarget: TARGETS.targetSpend,
      baselineISO: baselineStart, baselineIsSet: baselineIsSet(), baselineLabel: baselineLabel(),
      baselineDaysIn, baselineWindowDays,
      netWorth: totals.total, preKs: totals.preKs,
    };
  }

  function renderWeeklyAdvice() {
    const list = document.getElementById('weeklyInsightList');
    const countPill = document.getElementById('weeklyInsightCount');
    if (!list) return;
    const txCount = (state.transactions || []).length;
    if (txCount === 0) {
      list.innerHTML = '<span style="color:#9ca3af;">Import some transactions to see weekly observations.</span>';
      if (countPill) countPill.textContent = '0';
      return;
    }
    const snap = buildWeeklySnapshot();

    const insights = [];

    // 1) Week-over-week total
    if (snap.week1Total > 0 || snap.week2Total > 0) {
      const arrow = snap.weekDelta > 0 ? '↑' : (snap.weekDelta < 0 ? '↓' : '→');
      const color = snap.weekDelta > 0 ? '#b45309' : (snap.weekDelta < 0 ? '#047857' : '#6b7280');
      insights.push('<strong>This week:</strong> ' + fmt(snap.week1Total) + ' spent · <span style="color:' + color + ';">' + arrow + ' ' + fmt(Math.abs(snap.weekDelta)) + '</span> vs prior 7 days (' + fmt(snap.week2Total) + ').');
    }

    // 2) Account fortnight status
    const overAccts = snap.acctStatus.filter(a => a.budget > 0 && a.pct > 1);
    const underAccts = snap.acctStatus.filter(a => a.budget > 0 && a.pct <= 1);
    if (overAccts.length > 0) {
      const txt = overAccts.map(a => escapeHtml(a.acct) + ' ' + fmt(a.spent) + '/' + fmt(a.budget) + ' (' + Math.round(a.pct*100) + '%)').join(' · ');
      insights.push('<strong style="color:#b91c1c;">Over fortnight budget</strong> on day ' + snap.acctStatus[0].fnDayIndex + '/14: ' + txt + '.');
    } else if (underAccts.length > 0) {
      const txt = underAccts.map(a => escapeHtml(a.acct) + ' ' + Math.round(a.pct*100) + '%').join(' · ');
      insights.push('<strong style="color:#047857;">Account budgets on track</strong> (day ' + snap.acctStatus[0].fnDayIndex + '/14): ' + txt + '.');
    }

    // 3) Pace vs target (pro-rated if a baseline is set)
    const gap = snap.pace - snap.target;
    const targetLabel = snap.baselineIsSet ? fmtCompact(snap.target) + ' (pro-rated ' + snap.baselineLabel + ' → year-end)' : '$142K target';
    if (gap > snap.target * 0.05) {
      insights.push('<strong style="color:#b91c1c;">Projected ' + fmt(snap.pace) + '</strong> — ' + fmtCompact(gap) + ' over ' + targetLabel + '.');
    } else if (gap < -snap.target * 0.05) {
      insights.push('<strong style="color:#047857;">Projected ' + fmt(snap.pace) + '</strong> — ' + fmtCompact(-gap) + ' under ' + targetLabel + '.');
    } else {
      insights.push('<strong>Projected ' + fmt(snap.pace) + '</strong> — within ±5% of ' + targetLabel + '.');
    }

    // 4) Top movers
    if (snap.movers.length > 0) {
      const top3 = snap.movers.slice(0, 3).map(m => {
        const arrow = m.delta > 0 ? '↑' : '↓';
        const color = m.delta > 0 ? '#b45309' : '#047857';
        return escapeHtml(m.cat) + ' <span style="color:' + color + ';">' + arrow + fmt(Math.abs(m.delta)) + '</span>';
      }).join(' · ');
      insights.push('<strong>Top movers vs prior week:</strong> ' + top3 + '.');
    }

    // 5) Largest single tx
    if (snap.largest) {
      insights.push('<strong>Largest tx:</strong> ' + escapeHtml(snap.largest.payee || '(no payee)') + ' · ' + fmt(-snap.largest.amount) + ' · ' + escapeHtml(snap.largest.category) + '.');
    }

    // 6) New payees
    if (snap.newPayees.length > 0) {
      const txt = snap.newPayees.slice(0, 5).map(p => escapeHtml(p.name) + ' (' + fmt(p.amount) + ')').join(' · ');
      insights.push('<strong>New payees:</strong> ' + txt + '.');
    }

    // 7) Uncategorised
    if (snap.uncategorised.length > 0) {
      const total = snap.uncategorised.reduce((s, t) => s + (-t.amount), 0);
      insights.push('<strong style="color:#b45309;">' + snap.uncategorised.length + ' uncategorised</strong> tx this week (' + fmt(total) + ') — review in Categorise-by-payee.');
    }

    list.innerHTML = insights.map(i => '<div style="padding: 4px 0; border-bottom: 1px solid #f3f4f6;">' + i + '</div>').join('');
    if (countPill) countPill.textContent = insights.length + ' observation' + (insights.length === 1 ? '' : 's');
  }

  // Shared minimal "who is the user" header used in BOTH prompts so the LLM has just
  // enough orientation without overloading. Detailed plan numbers go in the plan prompt,
  // detailed transaction context in the spend prompt.
  function llmIdentityHeader() {
    const daysToRet = Math.max(0, Math.round((new Date('2027-03-31') - new Date()) / 86400000));
    return [
      '# Who I am',
      '',
      'Mark Rees · 51 (born 27 Sept 1974) · BNZ employee planning to retire March 2027 (~' + daysToRet + ' days away) · annual core spend target $142,000 (NZD).',
    ].join('\n');
  }

  // SPEND-focused prompt: weekly transactions, fortnight budgets, category drift.
  // Coach should answer tactical "what do I do this week / fortnight" questions.
  function buildLLMPrompt() {
    const snap = buildWeeklySnapshot();
    const todayStr = snap.today;

    const orientationLines = [
      '## Where the plan stands (for context only — don\'t spend much time on this)',
      '',
      '- **Net worth:** ' + fmt(snap.netWorth) + ' · sustainable draw @ 3.5% on pre-KS pool = ' + fmt(snap.preKs * 0.035) + '/yr vs $142K post-retirement target.',
    ];
    if (snap.baselineIsSet) {
      orientationLines.push('- **Tracking baseline:** ' + snap.baselineISO + ' (' + snap.baselineLabel + '). Pre-baseline spending is excluded from these numbers — it reflected a higher-income lifestyle that no longer applies.');
      orientationLines.push('- **Spend ' + snap.baselineLabel + ':** ' + fmt(snap.ytd) + ' raw / ' + fmt(snap.ytdAmortized) + ' amortized over ' + snap.baselineDaysIn + ' days.');
      orientationLines.push('- **Projected ' + snap.baselineLabel + ' → year-end:** ' + fmt(snap.pace) + ' vs pro-rated target ' + fmt(snap.target) + ' (' + (snap.pace > snap.target ? 'over by ' : 'under by ') + fmtCompact(Math.abs(snap.pace - snap.target)) + '). Full-year target reference: $142K.');
    } else {
      orientationLines.push('- **Year-end spend pace:** ' + fmt(snap.pace) + ' vs $142K target (' + (snap.pace > snap.target ? 'over by ' : 'under by ') + fmtCompact(Math.abs(snap.pace - snap.target)) + ').');
    }
    const orientation = orientationLines.join('\n');

    // Current fortnight per account
    const acctLines = snap.acctStatus.map(a => {
      if (a.budget === 0) return '- **' + a.acct + ':** no budget set · ' + fmt(a.spent) + ' spent so far';
      return '- **' + a.acct + ':** ' + fmt(a.spent) + ' / ' + fmt(a.budget) + ' (' + Math.round(a.pct * 100) + '%) on day ' + a.fnDayIndex + '/14';
    });
    const acctBlock = ['## Current fortnight (pay-cycle aligned)', '', ...acctLines].join('\n');

    // This week vs prior week
    const movers = snap.movers.slice(0, 8).map(m => {
      const arrow = m.delta > 0 ? '↑' : '↓';
      return '- **' + m.cat + ':** ' + fmt(m.w1) + ' this week vs ' + fmt(m.w2) + ' prior · ' + arrow + ' ' + fmt(Math.abs(m.delta));
    });
    const weekBlock = [
      '## Last 7 days (' + snap.week1Start + ' → ' + snap.week1End + ')',
      '',
      '- **This week total:** ' + fmt(snap.week1Total),
      '- **Prior 7 days total:** ' + fmt(snap.week2Total),
      '- **Week-over-week:** ' + (snap.weekDelta >= 0 ? '+' : '-') + fmt(Math.abs(snap.weekDelta)),
      '',
      '### Top movers by category',
      '',
      movers.length ? movers.join('\n') : '- (no significant category moves)',
    ].join('\n');

    // Top categories — measured from the baseline forward, not from Jan 1
    const ytdByCat = {};
    (state.transactions || []).forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < snap.baselineISO || t.date > todayStr) return;
      ytdByCat[t.category] = (ytdByCat[t.category] || 0) + (-t.amount);
    });
    const overrides = state.categoryAnnualForecast || {};
    function categoryForecast(cat) {
      const ytdAmt = ytdByCat[cat] || 0;
      const o = overrides[cat];
      const daysLeft = Math.max(0, Math.floor((new Date(snap.year + 1, 0, 1) - new Date()) / 86400000));
      const PERIODS = { fortnight: daysLeft/14, month: daysLeft/30.44, quarter: daysLeft/91.31, year: 1 };
      if (o != null) {
        const shape = (typeof o === 'number') ? { amount: o, period: 'year' } : { amount: +o.amount || 0, period: o.period || 'year' };
        if (shape.period === 'year') return Math.max(ytdAmt, shape.amount);
        return ytdAmt + shape.amount * (PERIODS[shape.period] || 0);
      }
      // Extrapolate the baseline-window daily rate to the full window length
      return ytdAmt * snap.baselineWindowDays / Math.max(1, snap.baselineDaysIn);
    }
    const topCats = Object.keys(ytdByCat).map(c => ({ cat: c, ytd: ytdByCat[c], forecast: categoryForecast(c) }))
      .sort((a,b) => b.forecast - a.forecast).slice(0, 8);
    const catLines = topCats.map(c => '- **' + c.cat + ':** ' + (snap.baselineIsSet ? snap.baselineLabel + ' ' : 'YTD ') + fmt(c.ytd) + ' · projected ' + fmt(c.forecast) + ' (' + ((c.forecast / snap.target) * 100).toFixed(1) + '% of pro-rated target)');
    const catBlock = ['## Top 8 spending categories — ' + (snap.baselineIsSet ? snap.baselineLabel + ' & projected' : 'YTD & projected'), '', ...catLines].join('\n');

    // Flags
    const flags = [];
    if (snap.newPayees.length > 0) {
      flags.push('- **New payees this week:** ' + snap.newPayees.slice(0, 5).map(p => p.name + ' (' + fmt(p.amount) + ', ' + p.category + ')').join('; '));
    }
    if (snap.uncategorised.length > 0) {
      const tot = snap.uncategorised.reduce((s, t) => s + (-t.amount), 0);
      flags.push('- **Uncategorised tx this week:** ' + snap.uncategorised.length + ' totalling ' + fmt(tot));
    }
    if (snap.largest) {
      flags.push('- **Largest single tx:** ' + (snap.largest.payee||'(no payee)') + ' · ' + fmt(-snap.largest.amount) + ' · ' + snap.largest.category);
    }
    const flagsBlock = flags.length ? ['## Flags', '', ...flags].join('\n') : '';

    const question = [
      '## What I want from you',
      '',
      'You are my NZ-based spending coach. Focus on the **last 7 days and the current fortnight** — strategic retirement planning lives in a separate review.',
      '',
      '1. Is anything in the last 7 days of actual transactions a behavioural pattern worth interrupting, or is it normal noise?',
      '2. Looking at the current fortnight account budgets (day ' + (snap.acctStatus[0] && snap.acctStatus[0].fnDayIndex || '?') + '/14), do I have headroom or do I need to slow down? Be specific about which account.',
      '3. Which one or two spending **categories** are most worth my attention this fortnight — and what would "good" look like in concrete dollars?',
      '4. Are any new payees or uncategorised items worth flagging — recurring subscriptions, duplicate charges, miscategorisation?',
      '',
      'Be specific to my numbers. Skip generic personal-finance lectures. Push back if my framing is wrong.',
    ].join('\n');

    return [llmIdentityHeader(), '', orientation, '', acctBlock, '', weekBlock, '', catBlock, flagsBlock ? '\n' + flagsBlock : '', '', question].join('\n');
  }

  // PLAN-focused prompt: net worth composition, bucket targets, drawdown, asset allocation,
  // NZ Super timing, KiwiSaver, PIE tax. Coach should answer strategic "is the plan still right"
  // questions — not weekly tactics.




  // ========== Top-of-page Account tracker: fortnight / month / YTD per BNZ account ==========
  // Real account balance = last synced snapshot (acctActualBalance) + net transaction flow since,
  // for transactions tagged with that source (excluding excluded). Single source of truth for the
  // per-account balance shown across the fortnight cards, savings, accruals and account tracker.
  function acctBalanceInfo(acct) {
    const rec = (state.acctActualBalance || {})[acct] || {};
    if (rec.amount == null || isNaN(+rec.amount) || !rec.asOf) return { hasBalance: false, amount: null, asOf: '', net: 0, base: 0 };
    const todayStr = todayISO();
    let net = 0;
    (state.transactions || []).forEach(t => {
      if ((t.source || '') !== acct || t.excluded) return;
      if (t.date <= rec.asOf || t.date > todayStr) return;
      net += t.amount;
    });
    return { hasBalance: true, amount: +rec.amount + net, asOf: rec.asOf, net, base: +rec.amount };
  }
  function acctRealBalance(acct) { const i = acctBalanceInfo(acct); return i.hasBalance ? i.amount : null; }

  // BNZ "Savings" and "Accruals" accounts allow one fee-free withdrawal per calendar month;
  // any further money-out (withdrawal / transfer out) incurs a fee. Count debits sourced from
  // the account this calendar month so the UI can quietly flag once the free one is spent.
  // Returns a small muted padlock (the account is "locked" for further fee-free withdrawals)
  // only after the first withdrawal; stays silent while the free withdrawal is still available.
  function freeWithdrawalLock(acct) {
    const now = new Date();
    const monthStart = now.getFullYear() + '-' + pad2(now.getMonth() + 1) + '-01';
    const todayStr = todayISO();
    let count = 0, lastDate = '';
    (state.transactions || []).forEach(t => {
      if ((t.source || '') !== acct || t.excluded || t.amount >= 0) return;
      if (t.date < monthStart || t.date > todayStr) return;
      count++;
      if (t.date > lastDate) lastDate = t.date;
    });
    if (count < 1) return '';  // free withdrawal still available — stay quiet
    const extra = count - 1;
    const title = 'Free monthly withdrawal used' + (lastDate ? ' on ' + lastDate : '') +
      ' — locked; further withdrawals this month may incur a fee' +
      (extra > 0 ? ' (' + extra + ' already since).' : '.');
    return ' <span title="' + title + '" style="display:inline-block; vertical-align:middle; margin-left:5px; color:var(--text4); cursor:help;">'
      + '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;">'
      + '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg></span>';
  }

  // Uses actual transactions tagged with their source account. Excludes Transfer /
  // Investing / Income / Tax (so it's real living-cost spend per account).


  // Opens the Transactions panel filtered to the current fortnight + chosen account, with
  // type set to "all" so any positive amounts (refunds, credits) show alongside expenses.


  // (renderBucketLevers removed — functionality merged into the Essentials accrual table)

  // ========== Accruals & headroom ==========
  // For categories paid quarterly / monthly / annually (the override period is not 'fortnight'),
  // compute how much should be accruing each fortnight, how much has been accrued since the
  // baseline, how much has actually been spent on that category, and the resulting "pot"
  // balance. Surfaces overdrawn categories (red) and well-funded ones (green) and gives a
  // bucket-level "headroom available this fortnight" number: behavioural budget minus what
  // you've already spent in behavioural categories in the current fortnight.


  // ========== Income panel ==========
  // Surfaces income side (salary, LTI vests, DVRP, refunds) so cashflow planning isn't
  // spend-only. Shows recent, 90-day, and since-baseline windows plus an annualized rate.
  function renderIncome() {
    const wrap = document.getElementById('incomePanel');
    const summary = document.getElementById('incomeSummary');
    if (!wrap) return;
    const txs = state.transactions || [];
    if (txs.length === 0) {
      wrap.innerHTML = '<p style="font-size: 13px; color: #9ca3af;">Import transactions to see income.</p>';
      if (summary) summary.textContent = '—';
      return;
    }

    const todayStr = todayISO();
    const last30Start = daysAgoISO(30);
    const last90Start = daysAgoISO(90);

    // Salary = inflows that look like BNZ payroll (payee "Bank of New Zealand" or description "BNZ Salaries").
    // Everything else categorised as Income is "Other" (LTI vests, DVRP, refunds, contributions from Sarah, etc.).
    function isSalary(t) {
      const p = (t.payee || '').toLowerCase();
      const d = (t.description || '').toLowerCase();
      return p.includes('bank of new zealand') || d.includes('bnz salaries');
    }

    let salaryLast30 = 0, salaryLast90 = 0, salaryCount30 = 0;
    let otherLast30 = 0, otherLast90 = 0, otherCount30 = 0;
    const otherPayees = {};

    txs.forEach(t => {
      if (t.category !== 'Income' || t.excluded) return;
      if (t.amount <= 0) return;
      if (t.date > todayStr) return;
      const salary = isSalary(t);
      if (t.date >= last30Start) {
        if (salary) { salaryLast30 += t.amount; salaryCount30++; }
        else { otherLast30 += t.amount; otherCount30++; }
      }
      if (t.date >= last90Start) {
        if (salary) salaryLast90 += t.amount;
        else { otherLast90 += t.amount; otherPayees[t.payee || '(unknown)'] = (otherPayees[t.payee || '(unknown)'] || 0) + t.amount; }
      }
    });

    const salaryAnnualized = salaryLast90 * 365 / 90;
    const otherAnnualized = otherLast90 * 365 / 90;

    const cardHtml = '<div class="acct-tracker" style="grid-template-columns: repeat(2, 1fr);">' +
      '<div class="acct-card">' +
        '<div class="label">Salary · last 30 days</div>' +
        '<div class="value">' + fmt(salaryLast30) + '</div>' +
        '<div class="sub">' + fmt(salaryAnnualized) + ' annualized · ' + salaryCount30 + ' payment' + (salaryCount30 === 1 ? '' : 's') + '</div>' +
      '</div>' +
      '<div class="acct-card">' +
        '<div class="label">Other · last 30 days</div>' +
        '<div class="value">' + fmt(otherLast30) + '</div>' +
        '<div class="sub">' + fmt(otherAnnualized) + ' annualized · ' + otherCount30 + ' payment' + (otherCount30 === 1 ? '' : 's') + '</div>' +
      '</div>' +
    '</div>';

    // Small "where Other came from" hint — top 3 non-salary payees in last 90 days
    const topOther = Object.entries(otherPayees).sort((a, b) => b[1] - a[1]).slice(0, 3);
    const otherBreakdown = topOther.length > 0
      ? '<div style="font-size: 11px; color: #9ca3af; margin-top: 8px;">Other sources (last 90 days): ' + topOther.map(([p, a]) => escapeHtml(p) + ' ' + fmt(a)).join(' · ') + '</div>'
      : '';

    wrap.innerHTML = cardHtml + otherBreakdown;
    if (summary) {
      summary.textContent = fmt(salaryAnnualized + otherAnnualized) + ' annualized';
    }
  }

  // ========== Account budgets: roll category forecasts up to BNZ accounts ==========
  // For each account (Living Well / Discretionary / Essentials / Savings), compare
  // the fortnightly × 26 annual budget with the sum of forecasts for categories
  // the user has mapped to that account.
  function renderAccountBudgets() {
    const body = document.getElementById('acctBudgetBody');
    const foot = document.getElementById('acctBudgetFoot');
    if (!body) return;

    // Compute per-category forecasts using calendar-year annualization (Jan 1 → Dec 31).
    // Note: renderForecast uses a rolling 12-month window from baseline — these will differ.
    const today = new Date();
    const year = today.getFullYear();
    const yearStart = new Date(year, 0, 1);
    const yearEnd = new Date(year + 1, 0, 1);
    const dayOfYear = Math.floor((today - yearStart) / 86400000) + 1;
    const daysRemaining = Math.max(0, Math.floor((yearEnd - today) / 86400000));
    const PERIODS_IN_REMAINDER = { fortnight: daysRemaining / 14, month: daysRemaining / 30.44, quarter: daysRemaining / 91.31, year: 1 };
    const overrides = state.categoryAnnualForecast || {};
    const acctMap = state.categoryAccount || {};
    const fortnightly = state.accountFortnightly || {};

    // YTD per category
    const catYtd = {};
    (state.transactions||[]).forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < year + '-01-01' || t.date > todayISO()) return;
      catYtd[t.category] = (catYtd[t.category] || 0) + (-t.amount);
    });

    function categoryForecast(cat) {
      const ytdAmt = catYtd[cat] || 0;
      const o = overrides[cat];
      if (o != null) {
        const shape = (typeof o === 'number') ? { amount: o, period: 'year' } : { amount: +o.amount || 0, period: o.period || 'year' };
        if (shape.period === 'year') return Math.max(ytdAmt, shape.amount);
        return ytdAmt + shape.amount * (PERIODS_IN_REMAINDER[shape.period] || 0);
      }
      return ytdAmt * 365 / dayOfYear;
    }

    // Per-account totals
    const acctForecast = {}; STANDARD_ACCOUNTS.forEach(a => acctForecast[a] = 0);
    let unassigned = 0;
    const spendingCategories = CATEGORIES.filter(c => !c.excluded && !c.oneOff).map(c => c.name);
    spendingCategories.forEach(cat => {
      const f = categoryForecast(cat);
      const acct = acctMap[cat];
      if (acct && acctForecast.hasOwnProperty(acct)) acctForecast[acct] += f;
      else if (f > 0) unassigned += f;
    });

    body.innerHTML = '';
    let totalFortnightly = 0, totalAnnual = 0, totalForecast = 0;
    STANDARD_ACCOUNTS.forEach(acct => {
      const fn = +fortnightly[acct] || 0;
      const annual = fn * 26;
      const fc = acctForecast[acct] || 0;
      const gap = fc - annual;
      totalFortnightly += fn; totalAnnual += annual; totalForecast += fc;
      let statusCls, statusLabel;
      if (annual === 0 && fc === 0)     { statusCls = 'gray';  statusLabel = '—'; }
      else if (annual === 0)            { statusCls = 'amber'; statusLabel = 'no budget · ' + fmt(fc) + ' forecast'; }
      else if (fc <= annual)            { statusCls = 'green'; statusLabel = 'Within budget'; }
      else if (fc <= annual * 1.05)     { statusCls = 'amber'; statusLabel = 'Slightly over'; }
      else                              { statusCls = 'red';   statusLabel = 'Over'; }
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td><strong>' + acct + '</strong></td>' +
        '<td style="text-align:right;">$<input type="number" class="num acct-fn-input" data-acct="' + escapeHtml(acct) + '" value="' + fn + '" min="0" step="50" style="width: 90px;"></td>' +
        '<td style="text-align:right; color:#6b7280; font-variant-numeric: tabular-nums;">' + fmt(annual) + '</td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums;">' + fmt(fc) + '</td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums; color:' + (gap > 0 ? '#b91c1c' : '#047857') + ';">' + (gap >= 0 ? '+' : '') + fmt(gap) + '</td>' +
        '<td><span class="pill ' + statusCls + '">' + statusLabel + '</span></td>';
      body.appendChild(tr);
    });
    body.querySelectorAll('.acct-fn-input').forEach(inp => {
      inp.addEventListener('change', () => {
        const v = parseFloat(inp.value);
        state.accountFortnightly = state.accountFortnightly || {};
        state.accountFortnightly[inp.dataset.acct] = isNaN(v) ? 0 : v;
        saveState(); render();
      });
    });
    if (unassigned > 0) {
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td style="color:#9ca3af;"><em>Unassigned categories</em></td>' +
        '<td></td><td></td>' +
        '<td style="text-align:right; color:#9ca3af; font-variant-numeric: tabular-nums;">' + fmt(unassigned) + '</td>' +
        '<td></td>' +
        '<td><span class="pill gray">assign in table above</span></td>';
      body.appendChild(tr);
    }
    const totalGap = totalForecast - totalAnnual;
    foot.innerHTML =
      '<tr style="font-weight: 600; border-top: 2px solid #e5e7eb;">' +
        '<td style="padding-top: 8px;">Total (' + fmt(totalFortnightly) + '/fn × 26)</td>' +
        '<td style="text-align:right; padding-top: 8px;">' + fmt(totalFortnightly) + '</td>' +
        '<td style="text-align:right; padding-top: 8px; color:#6b7280;">' + fmt(totalAnnual) + '</td>' +
        '<td style="text-align:right; padding-top: 8px;">' + fmt(totalForecast + unassigned) + '</td>' +
        '<td style="text-align:right; padding-top: 8px; color:' + ((totalForecast+unassigned) > totalAnnual ? '#b91c1c' : '#047857') + ';">' + ((totalForecast+unassigned-totalAnnual) >= 0 ? '+' : '') + fmt(totalForecast + unassigned - totalAnnual) + '</td>' +
        '<td></td>' +
      '</tr>';
  }

  // ========== By-category month tracking (current or any selected month) ==========
  // selectedMonthOffset = 0 (current month), -1 (last month), +1 (next), etc.
  let selectedMonthOffset = 0;
  // Multi-fortnight trend — total Living Well (controllable) spend per fortnight as bars, with the
  // fortnightly budget as a reference line. Bars over budget go red. Clear read of the trajectory.


  // Savings goals — sinking funds (holidays, renovations). A running balance you top up via regular
  // adds or by sweeping the prior fortnight's Living Well underspend. Distinct from Accruals (bills).



  // Account-grouped fortnight view — each account's funding vs spend this fortnight, with its
  // categories beneath at their fortnightly target + spend. Manage accounts and categories together.
  // Payday distribution — the fixed recipe to run the day salary lands, then leave alone for the
  // fortnight. Salary lands in Living Well. Living Well/Essentials/Accruals share a fixed
  // ~$5,295/fn spending allocation (their accountFortnightly budgets); everything over that is
  // swept to the Savings account. No dynamic per-account "move in/out" — these are standing
  // transfers you set up once.
  // "This fortnight" is manual: it never rolls over on its own. This shows which window is active
  // and surfaces a "Start new fortnight" button once the next payday Wednesday has arrived.
  const MONTHS_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function renderFortnightControls() {
    const btn = document.getElementById('rolloverFnBtn');
    const lbl = document.getElementById('fnStartLabel');
    if (btn && !btn._wired) {
      btn._wired = true;
      btn.addEventListener('click', rolloverFortnight);
    }
    const { fnStartISO, fnDayIndex } = currentFortnight();
    if (lbl) {
      const d = parseISO(fnStartISO);
      lbl.textContent = 'since ' + d.getDate() + ' ' + MONTHS_ABBR[d.getMonth()] + ' · day ' + fnDayIndex;
    }
    if (btn) {
      const due = fortnightRolloverDue();
      btn.style.display = due ? '' : 'none';
      if (due) {
        const a = parseISO(payAlignedStart(todayISO()));
        btn.textContent = '↻ Salary arrived — start new fortnight (' + a.getDate() + ' ' + MONTHS_ABBR[a.getMonth()] + ')';
        btn.style.background = '#f59e0b';
        btn.style.color = '#fff';
        btn.style.borderColor = '#f59e0b';
      }
    }
  }

  function renderPaydayPlan() {
    const wrap = document.getElementById('paydayPlan');
    if (!wrap) return;
    const funding = state.accountFortnightly || {};
    const lw      = +funding['Living Well'] || 0;
    const acc     = +funding['Accruals']    || 0;
    const spendTotal = lw + acc;

    // Monthly giving reserve carried inside the Living Well allocation. The donation DDs leave
    // ~once a month (not every fortnight), so their fortnightly slice accumulates in Living Well
    // through the month and is drawn when the DDs land ~end of month. Surfaced below so the
    // Living Well figure isn't mistaken for fully-spendable discretionary.
    let givingMonthly = 0;
    {
      const _pm = { fortnight: 26 / 12, month: 1, quarter: 1 / 3, year: 1 / 12 };
      const _acctMap = state.categoryAccount || {};
      Object.entries(state.categoryAnnualForecast || {}).forEach(([cat, o]) => {
        if (cat.indexOf('Giving') !== 0 || (_acctMap[cat] || '') !== 'Living Well') return;
        const amt = (typeof o === 'number') ? o : (+o.amount || 0);
        const per = (typeof o === 'object' && o.period) || 'year';
        givingMonthly += amt * (_pm[per] || 0);
      });
    }

    // Only show once salary lands THIS fortnight.
    const { fnStartISO } = currentFortnight();
    const todayStr = todayISO();
    let salary = 0, salDate = '';
    (state.transactions || []).forEach(t => {
      if (t.amount <= 0 || t.excluded) return;
      if (!/bnz salar/i.test((t.description || '') + ' ' + (t.payee || ''))) return;
      if (t.date < fnStartISO || t.date > todayStr) return;
      if (t.date > salDate) { salDate = t.date; salary = t.amount; }
    });

    if (salary <= 0) { wrap.innerHTML = ''; return; }

    // Hide once this salary has been acknowledged by pressing "Start new fortnight" — that's the
    // explicit "I've run the payday distribution" signal (same date the rollover button keys off).
    if ((state.lastRolloverSalaryDate || '') >= salDate) { wrap.innerHTML = ''; return; }

    // Hide once the money's been moved out: sum the transfers that have left Living Well
    // since the salary landed. When they cover ~all of what should move (everything except
    // the amount that stays in Living Well), the distribution is done and the panel clears.
    {
      const toMove = salary - lw;
      let movedOut = 0;
      (state.transactions || []).forEach(t => {
        if ((t.source || '') !== 'Living Well' || t.excluded || t.amount >= 0) return;
        if (t.date < salDate) return;
        if (t.category !== 'Transfer' && t.category !== 'Investing') return;
        movedOut += -t.amount;
      });
      if (toMove > 0 && movedOut >= toMove * 0.9) { wrap.innerHTML = ''; return; }
    }

    // B1: everything over the spending allocation goes to B1 (fixed each fortnight).
    const b1Transfer = Math.max(0, Math.round((salary - spendTotal) * 100) / 100);

    // Accruals: fill the pot deficit (expected pot balance minus real balance).
    let accTransfer = acc;
    {
      const periodPerFnMap = { fortnight: 1, month: 12/26, quarter: 4/26, year: 1/26 };
      const accrualOverrides = state.categoryAnnualForecast || {};
      const acctMap = state.categoryAccount || {};
      const startISO = baselineISO();
      const baselineStart = parseISO(startISO);
      const today = new Date();
      let fnElapsed = 0;
      { const d = parseISO(state.payAnchorDate || startISO);
        while (d > baselineStart) d.setDate(d.getDate() - 14);
        while (d < baselineStart) d.setDate(d.getDate() + 14);
        while (d <= today) { fnElapsed++; d.setDate(d.getDate() + 14); } }
      const catSpend = {};
      (state.transactions || []).forEach(t => {
        if (t.amount >= 0 || t.excluded) return;
        if (t.date < startISO || t.date > todayStr) return;
        catSpend[t.category] = (catSpend[t.category] || 0) + (-t.amount);
      });
      let potBalance = 0;
      Object.entries(accrualOverrides).forEach(([cat, o]) => {
        const shape = typeof o === 'number' ? { amount: o, period: 'year' } : { amount: +o.amount || 0, period: o.period || 'year' };
        if (shape.period === 'fortnight') return;
        const mapped = acctMap[cat] || '';
        if (mapped && mapped !== 'Essentials' && mapped !== 'Accruals') return;
        potBalance += (shape.amount * (periodPerFnMap[shape.period] || 0) * fnElapsed) - (catSpend[cat] || 0);
      });
      potBalance = Math.round(potBalance * 100) / 100;
      const accInfo = acctBalanceInfo('Accruals');
      if (accInfo.hasBalance) {
        const deficit = Math.round((potBalance - accInfo.amount) * 100) / 100;
        accTransfer = Math.max(acc, deficit > 0 ? deficit : 0);
      }
    }

    // Savings: unspent portion of the spending allocation after topping up accounts.
    const savingsTransfer = Math.max(0, Math.round((spendTotal - lw - accTransfer) * 100) / 100);

    const card = (label, amt) =>
      '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;min-width:0;">' +
        '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);font-weight:500;">' + escapeHtml(label) + '</div>' +
        '<div style="font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;">' + fmt(amt) + '</div>' +
      '</div>';

    // The Essentials / Accruals / Savings amounts are derived from live balances — flag
    // when those are stale (pre-today) so a payday distribution isn't run on old numbers.
    const _today = todayISO();
    const staleAsOf = ['Living Well', 'Accruals']
      .map(a => (state.acctActualBalance || {})[a])
      .filter(r => r && r.asOf && r.asOf < _today)
      .map(r => r.asOf).sort()[0] || null;
    wrap.innerHTML =
      '<div style="margin-bottom:20px;">' +
        '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);font-weight:500;margin-bottom:8px;">Payday distribution · ' + fmt(salary) + '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;">' +
          card('Living Well', lw) +
          card('B1', b1Transfer) +
          card('Accruals', accTransfer) +
          card('Savings', savingsTransfer) +
        '</div>' +
        (givingMonthly > 0
          ? '<div style="margin-top:8px; font-size:12px; color:var(--text3);">Of Living Well, ' + fmt(givingMonthly) + '/mo (≈' + fmt(givingMonthly * 12 / 26) + '/fn) is reserved for the donation DDs that leave ~this week each month — keep it, don\'t treat it as spendable.</div>'
          : '') +
        (staleAsOf
          ? '<div style="margin-top:8px; font-size:12px; color:#b45309;">⚠ Balances last synced ' + escapeHtml(staleAsOf) + ' — amounts will adjust when the BNZ sync lands (~90s). Wait for the Synced pill before moving money.</div>'
          : '') +
      '</div>';
  }

  // Per-account collapse state for the fortnight view. Essentials starts collapsed
  // (its categories are mostly fixed bills you rarely need to eyeball); click an
  // account header to toggle. State persists across re-renders within the session.
  const fnAcctCollapsed = { 'Living Well': true, 'Essentials': true, 'Accruals': true, 'Savings': true };



  // Pinned discretionary categories to manage month-to-month (configurable via state.dashWatchCats).

  function renderCurrentMonthByCategory() {
    const body = document.getElementById('curMonthCatBody');
    const emptyDiv = document.getElementById('curMonthCatEmpty');
    const label = document.getElementById('currentMonthLabel');
    const titleEl = document.getElementById('monthByCatTitle');
    if (!body) return;
    body.innerHTML = '';
    { const sm = document.getElementById('monthSummary'); if (sm) sm.innerHTML = ''; }

    const today = new Date();
    const baseYear = today.getFullYear();
    const baseMonth = today.getMonth();
    const m = baseMonth + selectedMonthOffset;
    // Year/month indices may overflow; let Date normalise.
    const mStart = new Date(baseYear, m, 1);
    const mEnd = new Date(baseYear, m + 1, 1);
    const year = mStart.getFullYear();
    const monthIdx = mStart.getMonth();
    const isCurrentMonth = selectedMonthOffset === 0;
    const isPastMonth = mEnd <= today;
    const isFutureMonth = mStart > today;
    const daysInMonth = (mEnd - mStart) / 86400000;
    const daysElapsed = isCurrentMonth ? ((today - mStart) / 86400000) + 1 : (isPastMonth ? daysInMonth : 0);
    const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    if (titleEl) titleEl.textContent = isCurrentMonth ? 'This month by category' : (monthNames[monthIdx] + ' ' + year + ' by category');
    if (label) {
      if (isCurrentMonth) label.textContent = monthNames[monthIdx] + ' · day ' + Math.round(daysElapsed) + '/' + Math.round(daysInMonth);
      else if (isPastMonth) label.textContent = monthNames[monthIdx] + ' ' + year + ' · completed';
      else label.textContent = monthNames[monthIdx] + ' ' + year + ' · upcoming';
    }
    // Enable/disable nav buttons
    const prevBtn = document.getElementById('monthPrev');
    const nextBtn = document.getElementById('monthNext');
    if (prevBtn) prevBtn.disabled = false;
    if (nextBtn) nextBtn.disabled = false;

    const mStartISO = year + '-' + pad2(monthIdx + 1) + '-01';
    const mEndDate = new Date(mEnd.getTime() - 86400000);
    const mEndISO = year + '-' + pad2(monthIdx + 1) + '-' + pad2(mEndDate.getDate());

    // Spend in the selected month per category (RAW cash so monthly lump payments do show in the month they hit)
    const monthByCat = {};
    (state.transactions||[]).forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < mStartISO || t.date > mEndISO) return;
      monthByCat[t.category] = (monthByCat[t.category] || 0) + (-t.amount);
    });

    // Cumulative spend BEFORE the start of the selected month — used for auto-inference
    // and the annual-amount remaining calculation. For current/future months this is just YTD.
    const ytdStart = year + '-01-01';
    const monthsElapsedAtStart = monthIdx;  // 0 for Jan, 1 for Feb, etc.
    const ytdByCat = {};
    (state.transactions||[]).forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < ytdStart || t.date >= mStartISO) return;
      ytdByCat[t.category] = (ytdByCat[t.category] || 0) + (-t.amount);
    });

    const overrides = state.categoryAnnualForecast || {};
    // Union of categories with month spend or with an override set
    const cats = new Set([...Object.keys(monthByCat), ...Object.keys(overrides)]);
    if (cats.size === 0) { emptyDiv.style.display = ''; return; }
    emptyDiv.style.display = 'none';

    const rows = [];
    cats.forEach(cat => {
      const spent = monthByCat[cat] || 0;
      const ytdAtStart = ytdByCat[cat] || 0;
      const o = overrides[cat];
      let monthlyEquivalent;
      let method;
      // Months remaining in the year INCLUDING the selected month (Jan=12 down to Dec=1)
      const monthsRemainingIncl = Math.max(1, 12 - monthIdx);
      if (o == null) {
        // auto — average rate based on data up to the start of the selected month
        monthlyEquivalent = monthsElapsedAtStart > 0 ? ytdAtStart / monthsElapsedAtStart : 0;
        method = 'auto · prior-month avg';
      } else {
        const shape = (typeof o === 'number') ? { amount: o, period: 'year' } : { amount: +o.amount || 0, period: o.period || 'year' };
        if (shape.period === 'year') {
          // Annual amount minus what was already paid in prior months
          const remaining = Math.max(0, shape.amount - ytdAtStart);
          monthlyEquivalent = remaining / monthsRemainingIncl;
          method = remaining <= 0 ? 'annual · paid for year' : 'annual · $' + Math.round(remaining).toLocaleString('en-NZ') + ' left ÷ ' + monthsRemainingIncl + ' mo';
        }
        else if (shape.period === 'quarter')   { monthlyEquivalent = shape.amount / 3; method = 'quarterly ÷ 3'; }
        else if (shape.period === 'fortnight') { monthlyEquivalent = shape.amount * (365.25 / 14) / 12; method = 'fortnightly × 2.17'; }
        else /* month */                       { monthlyEquivalent = shape.amount; method = 'monthly rate'; }
      }
      // Compare against the FULL month's expected amount (not pro-rated to today)
      const expected = monthlyEquivalent;
      const delta = spent - expected;
      const warnThreshold = Math.max(expected * 0.05, 20);  // 5% or $20 cushion
      let statusCls, statusLabel;
      if (expected === 0 && spent === 0)          { statusCls = 'gray'; statusLabel = 'no activity'; }
      else if (expected === 0)                    { statusCls = 'amber'; statusLabel = 'unexpected (no rate set)'; }
      else if (spent <= expected)                 { statusCls = 'green'; statusLabel = 'Within budget'; }
      else if (spent <= expected + warnThreshold) { statusCls = 'amber'; statusLabel = 'Slightly over'; }
      else                                        { statusCls = 'red'; statusLabel = 'Over'; }

      // Skip rows where both the budget and the actual are zero — no signal there
      if (spent === 0 && expected === 0) return;
      rows.push({ cat, spent, expected, delta, statusCls, statusLabel, method, monthlyEquivalent });
    });
    rows.sort((a, b) => b.spent - a.spent);
    if (rows.length === 0) { emptyDiv.style.display = ''; return; }

    // ── Headline summary: total spent vs monthly budget, with pace through the month ──
    const summaryEl = document.getElementById('monthSummary');
    if (summaryEl) {
      const totalSpent = rows.reduce((s, r) => s + r.spent, 0);
      const totalBudget = rows.reduce((s, r) => s + r.expected, 0);
      const monthFrac = Math.min(1, Math.max(0, daysElapsed / daysInMonth));
      // Pace-adjusted budget = how much of the month's budget "should" be spent by now.
      const paceBudget = isCurrentMonth ? totalBudget * monthFrac : totalBudget;
      const spentPct = totalBudget > 0 ? (totalSpent / totalBudget * 100) : 0;
      const overUnder = (isCurrentMonth ? paceBudget : totalBudget) - totalSpent;
      const onTrack = overUnder >= 0;
      const barColor = onTrack ? '#10b981' : '#ef4444';
      const fillPct = Math.min(100, spentPct);
      const paceMarker = isCurrentMonth ? Math.min(100, monthFrac * 100) : 100;
      const statusTxt = isFutureMonth ? 'Upcoming month'
        : (onTrack
            ? '<span style="color:#047857; font-weight:600;">On track</span> · ' + fmt(Math.abs(overUnder)) + (isCurrentMonth ? ' under pace' : ' under budget')
            : '<span style="color:#b91c1c; font-weight:600;">Over</span> · ' + fmt(Math.abs(overUnder)) + (isCurrentMonth ? ' ahead of pace' : ' over budget'));
      summaryEl.innerHTML =
        '<div style="display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px;">' +
          '<div style="font-size:22px; font-weight:700;">' + fmt(totalSpent) +
            ' <span style="font-size:14px; font-weight:400; color:var(--text4);">of ' + fmt(totalBudget) + ' budget</span></div>' +
          '<div style="font-size:13px; color:var(--text4);">' +
            (isCurrentMonth ? ('day ' + Math.round(daysElapsed) + '/' + Math.round(daysInMonth) + ' · ' + Math.round(monthFrac * 100) + '% through month') : '') +
          '</div>' +
        '</div>' +
        // Track with spend fill + a pace marker showing where you "should" be today.
        '<div style="position:relative; height:12px; background:var(--border3, #eef0f3); border-radius:6px; margin:8px 0 6px; overflow:hidden;">' +
          '<div style="position:absolute; left:0; top:0; bottom:0; width:' + fillPct + '%; background:' + barColor + '; border-radius:6px;"></div>' +
          (isCurrentMonth ? '<div style="position:absolute; top:-2px; bottom:-2px; left:' + paceMarker + '%; width:2px; background:var(--text); opacity:0.55;" title="Today\'s pace"></div>' : '') +
        '</div>' +
        '<div style="font-size:13px;">' + statusTxt + '</div>';
    }

    // Cache this-month transactions per category for click-to-expand
    const monthTxByCat = {};
    (state.transactions||[]).forEach(t => {
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      if (t.date < mStartISO || t.date > mEndISO) return;
      (monthTxByCat[t.category] = monthTxByCat[t.category] || []).push(t);
    });

    rows.forEach(r => {
      const txs = monthTxByCat[r.cat] || [];
      const hasTx = txs.length > 0;
      const tr = document.createElement('tr');
      tr.dataset.cat = r.cat;
      if (hasTx) { tr.style.cursor = 'pointer'; tr.title = 'Click to see transactions'; }
      tr.innerHTML =
        '<td><strong>' + escapeHtml(r.cat) + '</strong>' + (hasTx ? ' <span style="font-size:10px; color:#9ca3af;">▸</span>' : '') + '<div class="src">' + r.method + ' · $' + Math.round(r.monthlyEquivalent).toLocaleString('en-NZ') + '/mo</div></td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums;">' + fmt(r.spent) + '</td>' +
        '<td style="text-align:right; color: var(--text4); font-variant-numeric: tabular-nums;">' + fmt(r.expected) + '</td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums; color: ' + (r.delta > 0 ? '#b91c1c' : '#047857') + ';">' + (r.delta >= 0 ? '+' : '') + fmt(r.delta) + '</td>' +
        '<td><span class="pill ' + r.statusCls + '">' + r.statusLabel + '</span></td>';
      body.appendChild(tr);
      if (hasTx) {
        tr.addEventListener('click', () => toggleMonthCatDetail(r.cat, txs));
      }
    });
  }

  // Expand / collapse a per-category transaction list inline.
  function toggleMonthCatDetail(cat, txs) {
    const body = document.getElementById('curMonthCatBody');
    if (!body) return;
    const mainRow = body.querySelector('tr[data-cat="' + CSS.escape(cat) + '"]');
    if (!mainRow) return;
    // Toggle off if already open
    const existing = mainRow.nextElementSibling;
    if (existing && existing.dataset && existing.dataset.detailFor === cat) {
      existing.remove();
      return;
    }
    // Close any other open detail rows
    body.querySelectorAll('tr[data-detail-for]').forEach(n => n.remove());
    const detail = document.createElement('tr');
    detail.dataset.detailFor = cat;
    detail.style.background = 'var(--surface2)';
    const sorted = txs.slice().sort((a, b) => b.date.localeCompare(a.date) || (b.id||'').localeCompare(a.id||''));
    const rowsHtml = sorted.map(t => (
      '<tr style="border-top: 1px solid var(--border3);">' +
        '<td style="padding: 4px 8px; color: var(--text4); white-space: nowrap;">' + t.date + '</td>' +
        '<td style="padding: 4px 8px; color: var(--text2);">' + escapeHtml(t.payee||'(no payee)') + (t.source ? '<div style="font-size:11px; color:var(--text5);">' + escapeHtml(t.source) + '</div>' : '') + '</td>' +
        '<td style="padding: 4px 8px; text-align: right; font-variant-numeric: tabular-nums; color: var(--text2);">' + fmt(-t.amount) + '</td>' +
      '</tr>'
    )).join('');
    detail.innerHTML = '<td colspan="5" style="padding: 8px 16px;">' +
      '<table style="width: 100%; font-size: 12px; border-collapse: collapse;">' +
        '<thead><tr><th style="text-align:left; color:var(--text4); padding: 4px 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px;">Date</th><th style="text-align:left; color:var(--text4); padding: 4px 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px;">Payee</th><th style="text-align:right; color:var(--text4); padding: 4px 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px;">Amount</th></tr></thead>' +
        '<tbody>' + rowsHtml + '</tbody>' +
      '</table></td>';
    mainRow.parentNode.insertBefore(detail, mainRow.nextSibling);
  }

  // ========== Investment returns ==========

  // One-line asset allocation. Look-through growth exposure: each vehicle contributes its
  // approximate growth-asset weight, so the number answers "how much of this actually rides the
  // market" rather than "how much is in a fund called Growth". Weights are indicative Simplicity
  // fund mixes — close enough to steer allocation decisions, not a substitute for the PDS.
  const GROWTH_WEIGHT = { cash: 0, conservative: 0.20, balanced: 0.50, kiwisaverGrowth: 0.80, shares: 1 };

  function renderAllocation() {
    const bar = document.getElementById('allocBar');
    if (!bar) return;
    const t = totalsFromState();
    const gtVal = (+state.gentrack_shares||0) * (+state.gentrack_price||0);
    // Property is excluded — it is a single illiquid asset about to convert to cash, and folding
    // it in would mask the financial-asset mix this line exists to show.
    const segs = [
      { name: 'Cash + TDs',   value: t.b1 + (+state.dvrp_net||0), w: GROWTH_WEIGHT.cash,            color: '#f59e0b' },
      { name: 'Conservative', value: t.b2,                        w: GROWTH_WEIGHT.conservative,    color: '#10b981' },
      { name: 'Balanced',     value: t.b3,                        w: GROWTH_WEIGHT.balanced,        color: '#2563eb' },
      { name: 'KiwiSaver',    value: t.ks,                        w: GROWTH_WEIGHT.kiwisaverGrowth, color: '#a855f7' },
      { name: 'Shares',       value: gtVal,                       w: GROWTH_WEIGHT.shares,          color: '#ec4899' }
    ].filter(x => x.value > 0);
    const total  = segs.reduce((a, x) => a + x.value, 0);
    const growth = segs.reduce((a, x) => a + x.value * x.w, 0);
    if (!total) { bar.innerHTML = ''; setText('allocText', ''); return; }
    const gPct = growth / total * 100;
    bar.innerHTML = segs.map(x =>
      '<span title="' + escapeHtml(x.name) + ' ' + fmt(x.value) + ' · ' + Math.round(x.w * 100) + '% growth" ' +
      'style="width:' + (x.value / total * 100) + '%; background:' + x.color + ';"></span>').join('');
    document.getElementById('allocText').innerHTML =
      '<strong style="color:var(--text);">' + gPct.toFixed(0) + '% growth</strong> · ' +
      (100 - gPct).toFixed(0) + '% defensive';
    document.getElementById('allocLine').title =
      segs.map(x => x.name + ' ' + fmt(x.value) + ' (' + (x.value / total * 100).toFixed(1) + '%)').join('  ·  ') +
      '  —  look-through growth ' + fmt(growth) + ' of ' + fmt(total);
  }

  // ========== Net worth ==========
  function renderNetWorth() {
    const t = totalsFromState();
    const gtVal = (+state.gentrack_shares||0) * (+state.gentrack_price||0);
    const components = [
      { name: 'Bucket 1 — Cash',          value: t.b1,                color: '#f59e0b', group: 'Investable' },
      { name: 'Bucket 2 — Bridge (Conservative)', value: t.b2,           color: '#10b981', group: 'Investable' },
      { name: 'Bucket 3 — Long (Balanced)', value: t.b3,               color: '#2563eb', group: 'Investable' },
      { name: 'Bucket 4 — KiwiSaver',     value: t.ks,                color: '#a855f7', group: 'Investable' },
      { name: 'LTI Tranche 1 (vesting)',  value: +state.lti_tranche1_net||0, color: '#fb923c', group: 'Pending' },
      { name: 'DVRP (pending)',           value: +state.dvrp_net||0,  color: '#fdba74', group: 'Pending' },
      { name: 'Gentrack shares ($' + (+state.gentrack_price||0).toFixed(2) + ')', value: gtVal, color: '#ec4899', group: 'Shares' },
      { name: '21 Nottingham St',         value: +state.property_nottingham||0, color: '#6b7280', group: 'Property' }
    ].filter(c => (c.value || 0) > 0);
    const total = components.reduce((s, c) => s + c.value, 0);

    document.getElementById('netWorthTotal').textContent = fmt(total);

    // Breakdown table
    const body = document.getElementById('netWorthBreakdown');
    body.innerHTML = '';
    const headTr = document.createElement('tr');
    headTr.innerHTML =
      '<th style="text-align:left; padding: 4px 0; color: #6b7280; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; font-weight: 500;">Component</th>' +
      '<th style="text-align:right; padding: 4px 0; color: #6b7280; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; font-weight: 500;">Value</th>' +
      '<th style="text-align:right; padding: 4px 0; color: #6b7280; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; font-weight: 500; width: 60px;">%</th>';
    body.appendChild(headTr);
    components.forEach(c => {
      const pct = total > 0 ? (c.value / total * 100) : 0;
      const tr = document.createElement('tr');
      tr.innerHTML =
        '<td style="padding: 5px 0;"><span style="display:inline-block; width:10px; height:10px; background:' + c.color + '; border-radius:2px; margin-right: 8px; vertical-align: middle;"></span>' + escapeHtml(c.name) + '</td>' +
        '<td style="text-align:right; font-variant-numeric: tabular-nums; padding: 5px 0;">' + fmt(c.value) + '</td>' +
        '<td style="text-align:right; color:#6b7280; padding: 5px 0; width: 60px;">' + pct.toFixed(1) + '%</td>';
      body.appendChild(tr);
    });
    document.getElementById('netWorthFoot').innerHTML =
      '<tr style="font-weight: 600; border-top: 2px solid #e5e7eb;"><td style="padding: 8px 0 4px;">Total</td><td style="padding: 8px 0 4px; text-align:right; font-variant-numeric: tabular-nums;">' + fmt(total) + '</td><td></td></tr>';
  }

  function renderTopPayees() {
    const period = (document.getElementById('topPeriod') || {}).value || 'ytd';
    const today = todayISO();
    const startDate = period === 'ytd' ? startOfYearISO() : daysAgoISO(parseInt(period, 10));
    const map = {};
    (state.transactions||[]).forEach(t => {
      if (t.date < startDate || t.date > today) return;
      if (EXCLUDED_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      const k = t.payee || '(no payee)';
      map[k] = (map[k] || 0) + (-t.amount);
    });
    const arr = Object.entries(map).sort((a,b) => b[1]-a[1]).slice(0, 8);
    const div = document.getElementById('topPayees');
    if (arr.length === 0) { div.innerHTML = '<div style="color:#9ca3af; font-style:italic; padding: 16px 0; text-align:center;">No transactions in this period</div>'; return; }
    div.innerHTML = arr.map(([nm, amt]) => '<div class="row"><span class="nm">' + escapeHtml(nm) + '</span><span class="amt">' + fmt(amt) + '</span></div>').join('');
  }

  // ========== Snapshots ==========
  function renderSnapshotTable() {
    const snaps = (state.snapshots||[]).slice().sort((a,b) => b.date.localeCompare(a.date));
    document.getElementById('snapCount').textContent = snaps.length;
    const body = document.getElementById('snapBody'); body.innerHTML = '';
    document.getElementById('snapEmpty').style.display = snaps.length === 0 ? '' : 'none';
    document.getElementById('snapTable').style.display = snaps.length === 0 ? 'none' : '';
    snaps.forEach(s => {
      const b1 = (s.b1_float||0)+(s.b1_td6||0)+(s.b1_td12||0);
      const total = b1 + (s.b2||0) + (s.b3||0) + (s.ks||0);
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + s.date + '</td><td class="amt">' + fmtCompact(b1) + '</td><td class="amt">' + fmtCompact(s.b2||0) + '</td><td class="amt">' + fmtCompact(s.b3||0) + '</td><td class="amt">' + fmtCompact(s.ks||0) + '</td><td class="amt">' + fmt(total) + '</td><td><button class="del" data-date="' + s.date + '" title="Delete">×</button></td>';
      body.appendChild(tr);
    });
    body.querySelectorAll('.del').forEach(btn => {
      btn.addEventListener('click', () => { state.deletedSnapshotDates = [...new Set([...(state.deletedSnapshotDates || []), btn.dataset.date])]; state.snapshots = state.snapshots.filter(s => s.date !== btn.dataset.date); saveState(); render(); });
    });
  }

  // (renderWeeklyChecklist removed — Sunday-routine checkbox UI was retired)

  document.getElementById('spendImportBNZBtn').addEventListener('click', openCSVImportDialog);

  // ========== Charts ==========
  function updateCharts() {
    // Cumulative YTD
    const year = new Date().getFullYear();
    const yStart = new Date(year, 0, 1);
    const today = new Date();
    const dayOfYear = Math.floor((today - yStart)/86400000) + 1;
    const labels = []; const cumulCore = []; const cumulTotal = []; const tgt = [];
    // Build per-day amortized contributions, then accumulate.
    const dailyCore = new Array(dayOfYear).fill(0);
    const dailyTotal = new Array(dayOfYear).fill(0);
    const yearTxAll = (state.transactions||[]).filter(t => !EXCLUDED_CATS.has(t.category) && !t.excluded && t.amount < 0);
    yearTxAll.forEach(t => {
      const isOneOff = ONEOFF_CATS.has(t.category);
      const n = spreadFor(t.category);
      const raw = -t.amount;
      if (n <= 1) {
        if (!t.date.startsWith(year+'-')) return;
        const idx = Math.floor((parseISO(t.date) - yStart) / 86400000);
        if (idx < 0 || idx >= dayOfYear) return;
        dailyTotal[idx] += raw;
        if (!isOneOff) dailyCore[idx] += raw;
      } else {
        const txDate = parseISO(t.date);
        const perMonth = raw / n;
        for (let i = 0; i < n; i++) {
          const ms = new Date(txDate.getFullYear(), txDate.getMonth() + i, 1);
          const me = new Date(txDate.getFullYear(), txDate.getMonth() + i + 1, 1);
          const monthDays = (me - ms) / 86400000;
          const perDay = perMonth / monthDays;
          const startD = ms > yStart ? ms : yStart;
          const endD = me < new Date(today.getFullYear(), today.getMonth(), today.getDate()+1) ? me : new Date(today.getFullYear(), today.getMonth(), today.getDate()+1);
          if (endD <= startD) continue;
          let cur = new Date(startD);
          while (cur < endD) {
            const idx = Math.floor((cur - yStart) / 86400000);
            if (idx >= 0 && idx < dayOfYear) {
              dailyTotal[idx] += perDay;
              if (!isOneOff) dailyCore[idx] += perDay;
            }
            cur = new Date(cur.getTime() + 86400000);
          }
        }
      }
    });
    let runningCore = 0, runningTotal = 0;
    for (let d = 0; d < dayOfYear; d++) {
      runningCore += dailyCore[d];
      runningTotal += dailyTotal[d];
      if (d % 3 === 0 || d === dayOfYear-1) {
        const day = new Date(year, 0, 1 + d);
        labels.push(day.getFullYear()+'-'+pad2(day.getMonth()+1)+'-'+pad2(day.getDate()));
        cumulCore.push(runningCore); cumulTotal.push(runningTotal); tgt.push(DAILY_TARGET * (d+1));
      }
    }
    const haveTx = runningTotal > 0;
    const haveReno = runningTotal > runningCore + 0.01;
    const ctxC = document.getElementById('chartCum').getContext('2d');
    document.getElementById('emptyCum').style.display = haveTx ? 'none' : '';
    document.getElementById('chartCum').style.display = haveTx ? '' : 'none';
    if (chartCum) chartCum.destroy();
    if (haveTx) {
      const datasets = [
        { label: 'YTD core spend', data: cumulCore, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.10)', fill: true, tension: 0.1, borderWidth: 2.5, pointRadius: 0 },
        { label: 'Target ($142K/yr pace)', data: tgt, borderColor: '#1f2937', borderDash: [6,4], borderWidth: 2, pointRadius: 0, fill: false }
      ];
      if (haveReno) datasets.push({ label: 'Total incl. renovation', data: cumulTotal, borderColor: '#f97316', borderDash: [2,3], borderWidth: 2, pointRadius: 0, fill: false });
      chartCum = new Chart(ctxC, {
        type: 'line',
        data: { labels, datasets },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'bottom', labels: { font: { size: 12 }, boxWidth: 12, padding: 10 } },
            tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmt(c.parsed.y) } } },
          scales: { y: { ticks: { callback: v => fmtCompact(v), font: { size: 11 } }, grid: { color: '#f3f4f6' } },
                    x: { ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { display: false } } }
        }
      });
    }

    // Category breakdown
    const period = document.getElementById('catPeriod').value;
    const catStart = period === 'ytd' ? startOfYearISO() : daysAgoISO(parseInt(period, 10));
    const catMap = {};
    (state.transactions||[]).forEach(t => {
      if (t.date < catStart) return;
      if (EXCLUDED_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      catMap[t.category] = (catMap[t.category] || 0) + (-t.amount);
    });
    const catEntries = Object.entries(catMap).sort((a,b) => b[1]-a[1]);
    const ctxK = document.getElementById('chartCat').getContext('2d');
    const haveCat = catEntries.length > 0;
    document.getElementById('emptyCat').style.display = haveCat ? 'none' : '';
    document.getElementById('chartCat').style.display = haveCat ? '' : 'none';
    if (chartCat) chartCat.destroy();
    if (haveCat) {
      const palette = ['#2563eb','#10b981','#f59e0b','#a855f7','#ef4444','#06b6d4','#84cc16','#ec4899','#6366f1','#14b8a6','#f97316','#8b5cf6','#0ea5e9','#22c55e','#eab308','#d946ef'];
      chartCat = new Chart(ctxK, {
        type: 'doughnut',
        data: { labels: catEntries.map(e=>e[0]), datasets: [{ data: catEntries.map(e=>e[1]), backgroundColor: catEntries.map((_,i) => palette[i % palette.length]), borderColor: '#fff', borderWidth: 2 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: '55%',
          plugins: { legend: { position: 'right', labels: { font: { size: 11 }, boxWidth: 10, padding: 6 } },
            tooltip: { callbacks: { label: c => { const total = catEntries.reduce((s,e)=>s+e[1],0); return c.label + ': ' + fmt(c.parsed) + ' (' + (c.parsed/total*100).toFixed(0) + '%)'; } } } }
        }
      });
    }

    // Balance history
    const snaps = (state.snapshots||[]).slice().sort((a,b) => a.date.localeCompare(b.date));
    const ctxH = document.getElementById('chartHistory').getContext('2d');
    if (chartHistory) chartHistory.destroy();
    document.getElementById('emptyHistory').style.display = snaps.length === 0 ? '' : 'none';
    document.getElementById('chartHistory').style.display = snaps.length === 0 ? 'none' : '';
    if (snaps.length > 0) {
      const lbls = snaps.map(s => s.date);
      const b1s = snaps.map(s => (s.b1_float||0)+(s.b1_td6||0)+(s.b1_td12||0));
      const b2s = snaps.map(s => s.b2||0);
      const b3s = snaps.map(s => s.b3||0);
      const kss = snaps.map(s => s.ks||0);
      const totals = snaps.map((_,i) => b1s[i]+b2s[i]+b3s[i]+kss[i]);
      chartHistory = new Chart(ctxH, {
        type: 'line',
        data: { labels: lbls, datasets: [
          { label: 'Investable', data: totals, borderColor: '#1f2937', backgroundColor: 'rgba(31,41,55,0.06)', fill: true, tension: 0.25, borderWidth: 2.5, pointRadius: 3 },
          { label: 'B3 Growth', data: b3s, borderColor: '#2563eb', backgroundColor: 'transparent', tension: 0.25, borderWidth: 2, pointRadius: 2 },
          { label: 'B2 Balanced', data: b2s, borderColor: '#10b981', backgroundColor: 'transparent', tension: 0.25, borderWidth: 2, pointRadius: 2 },
          { label: 'B4 KiwiSaver', data: kss, borderColor: '#a855f7', backgroundColor: 'transparent', tension: 0.25, borderWidth: 2, pointRadius: 2 },
          { label: 'B1 Cash', data: b1s, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.25, borderWidth: 2, pointRadius: 2 },
          ...((document.getElementById('showNetWorth') || {}).checked !== false ? [
            { label: 'Net worth (incl. Nottingham + shares + pre-bucket)',
              // Use each snapshot's own pre-bucket value (nwExtra) so transfers between pre-bucket
              // holdings and the buckets don't create artificial jumps. Fall back to current
              // pre-bucket for legacy snapshots that predate the nwExtra field.
              data: totals.map((v,i) => v + (snaps[i].nwExtra != null ? snaps[i].nwExtra : snapshotNwExtra())),
              borderColor: '#dc2626', borderDash: [6,4], backgroundColor: 'transparent', tension: 0.25, borderWidth: 2, pointRadius: 0 }
          ] : [])
        ]},
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'bottom', labels: { font: { size: 12 }, boxWidth: 12, padding: 10 } },
            tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmt(c.parsed.y) } } },
          scales: { y: { ticks: { callback: v => fmtCompact(v), font: { size: 11 } }, grid: { color: '#f3f4f6' } }, x: { ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { display: false } } }
        }
      });
    }

    // Monthly bar chart — last 12 months of core spend
    const monthLabels = []; const monthVals = [];
    const today2 = new Date();
    for (let i = 11; i >= 0; i--) {
      const d = new Date(today2.getFullYear(), today2.getMonth() - i, 1);
      const next = new Date(today2.getFullYear(), today2.getMonth() - i + 1, 1);
      const startISO = d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-01';
      // End of month — last day in this month
      const lastDay = new Date(next.getTime() - 86400000);
      const endISO = lastDay.getFullYear()+'-'+pad2(lastDay.getMonth()+1)+'-'+pad2(lastDay.getDate());
      // Amortized spending — Body corp / Insurance / Rates get spread across months
      const total = spendingInRange(startISO, endISO);
      monthLabels.push(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()] + ' ' + (d.getFullYear()+'').slice(2));
      monthVals.push(total);
    }
    const haveMonths = monthVals.some(v => v > 0);
    document.getElementById('emptyMonthly').style.display = haveMonths ? 'none' : '';
    document.getElementById('chartMonthly').style.display = haveMonths ? '' : 'none';
    if (chartMonthly) chartMonthly.destroy();
    if (haveMonths) {
      const monthlyTarget = TARGETS.targetSpend / 12;
      chartMonthly = new Chart(document.getElementById('chartMonthly').getContext('2d'), {
        data: { labels: monthLabels, datasets: [
          { type: 'bar', label: 'Core spend', data: monthVals, backgroundColor: monthVals.map(v => v === 0 ? '#e5e7eb' : v > monthlyTarget * 1.1 ? '#ef4444' : v > monthlyTarget ? '#f59e0b' : '#10b981') },
          { type: 'line', label: 'Monthly target ($' + Math.round(monthlyTarget).toLocaleString('en-NZ') + ')', data: monthLabels.map(() => monthlyTarget), borderColor: '#1f2937', borderDash: [6,4], borderWidth: 2, pointRadius: 0, fill: false }
        ]},
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom', labels: { font: { size: 12 }, boxWidth: 12, padding: 10 } },
            tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmt(c.parsed.y) } } },
          scales: { y: { ticks: { callback: v => fmtCompact(v), font: { size: 11 } }, grid: { color: '#f3f4f6' } }, x: { ticks: { font: { size: 11 } }, grid: { display: false } } } }
      });
    }

    // Day-of-week — average spend per weekday in last 90 days
    const dowPeriod = (document.getElementById('dowPeriod') || {}).value || '90';
    const dowStart = dowPeriod === 'ytd' ? startOfYearISO() : daysAgoISO(parseInt(dowPeriod, 10));
    const dowTotal = [0,0,0,0,0,0,0];
    const dowCount = [0,0,0,0,0,0,0];
    const seenDates = new Set();
    (state.transactions||[]).forEach(t => {
      if (t.date < dowStart) return;
      if (EXCLUDED_CATS.has(t.category) || ONEOFF_CATS.has(t.category) || t.excluded) return;
      if (t.amount >= 0) return;
      const d = parseISO(t.date).getDay(); // 0=Sun
      dowTotal[d] += -t.amount;
      seenDates.add(t.date + ':' + d);
    });
    // Count distinct dates per weekday so average per day-occurrence is correct
    const dayCountPerDow = [0,0,0,0,0,0,0];
    const uniqueDates = new Set();
    (state.transactions||[]).forEach(t => { if (t.date >= dowStart) uniqueDates.add(t.date); });
    uniqueDates.forEach(d => { dayCountPerDow[parseISO(d).getDay()]++; });
    const dowAvg = dowTotal.map((sum, i) => dayCountPerDow[i] > 0 ? sum / dayCountPerDow[i] : 0);
    // Reorder Mon-first for readability
    const order = [1,2,3,4,5,6,0];
    const dowLabels = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const dowData = order.map(i => dowAvg[i]);
    const haveDow = dowData.some(v => v > 0);
    const emptyDowEl = document.getElementById('emptyDow');
    const chartDowEl = document.getElementById('chartDow');
    if (!emptyDowEl || !chartDowEl) return;  // Day-of-week chart removed from UI — bail
    emptyDowEl.style.display = haveDow ? 'none' : '';
    chartDowEl.style.display = haveDow ? '' : 'none';
    if (chartDow) chartDow.destroy();
    if (haveDow) {
      chartDow = new Chart(chartDowEl.getContext('2d'), {
        type: 'bar',
        data: { labels: dowLabels, datasets: [{ label: 'Avg spend / day', data: dowData, backgroundColor: dowData.map((v,i) => i >= 5 ? '#f59e0b' : '#2563eb') }] },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => 'Avg: ' + fmt(c.parsed.y) + '/day' } } },
          scales: { y: { ticks: { callback: v => fmtCompact(v), font: { size: 11 } }, grid: { color: '#f3f4f6' } }, x: { ticks: { font: { size: 12 } }, grid: { display: false } } } }
      });
    }
  }

  // ========== CSV import ==========
  function parseCSV(text) {
    const rows = []; let row = [], field = '', inQuotes = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (inQuotes) {
        if (c === '"' && text[i+1] === '"') { field += '"'; i++; }
        else if (c === '"') inQuotes = false;
        else field += c;
      } else {
        if (c === '"') inQuotes = true;
        else if (c === ',') { row.push(field); field = ''; }
        else if (c === '\n' || c === '\r') {
          if (field !== '' || row.length > 0) { row.push(field); rows.push(row); row = []; field = ''; }
          if (c === '\r' && text[i+1] === '\n') i++;
        } else field += c;
      }
    }
    if (field !== '' || row.length > 0) { row.push(field); rows.push(row); }
    return rows;
  }

  function findColIdx(headers, ...candidates) {
    const lower = headers.map(h => h.toLowerCase().trim());
    for (const c of candidates) { const i = lower.indexOf(c.toLowerCase()); if (i !== -1) return i; }
    return -1;
  }

  const MONTHS = { jan:1, feb:2, mar:3, apr:4, may:5, jun:6, jul:7, aug:8, sep:9, sept:9, oct:10, nov:11, dec:12,
                    january:1, february:2, march:3, april:4, june:6, july:7, august:8, september:9, october:10, november:11, december:12 };

  function normaliseDate(s) {
    if (s == null) return null;
    s = String(s).trim();
    if (!s) return null;
    // Strip any time component ("2026-05-04 12:34", "04/05/2026T12:34:56Z", etc.)
    s = s.replace(/T\d{2}:.*$/, '').replace(/\s+\d{2}:\d{2}.*$/, '').trim();
    if (!s) return null;

    // ISO YYYY-MM-DD
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;

    // YYYY/MM/DD or YYYY.MM.DD
    let m = s.match(/^(\d{4})[\/\.\-](\d{1,2})[\/\.\-](\d{1,2})$/);
    if (m) return m[1] + '-' + pad2(+m[2]) + '-' + pad2(+m[3]);

    // DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY (NZ default)
    m = s.match(/^(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{4})$/);
    if (m) return m[3] + '-' + pad2(+m[2]) + '-' + pad2(+m[1]);

    // DD/MM/YY (2-digit year)
    m = s.match(/^(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2})$/);
    if (m) {
      const yy = +m[3];
      const year = yy < 70 ? 2000 + yy : 1900 + yy;
      return year + '-' + pad2(+m[2]) + '-' + pad2(+m[1]);
    }

    // Compact YYYYMMDD (e.g. "20260504")
    m = s.match(/^(\d{4})(\d{2})(\d{2})$/);
    if (m) return m[1] + '-' + m[2] + '-' + m[3];

    // DD Mon YYYY (e.g. "4 May 2026", "04-May-2026", "4-May-26")
    m = s.match(/^(\d{1,2})[\s\-\/]([A-Za-z]+)[\s\-\/](\d{2,4})$/);
    if (m) {
      const mon = MONTHS[m[2].toLowerCase()];
      if (mon) {
        let year = +m[3];
        if (year < 100) year = year < 70 ? 2000 + year : 1900 + year;
        return year + '-' + pad2(mon) + '-' + pad2(+m[1]);
      }
    }

    // Mon DD YYYY or Mon DD, YYYY
    m = s.match(/^([A-Za-z]+)[\s\-]+(\d{1,2})[,\s\-]+(\d{2,4})$/);
    if (m) {
      const mon = MONTHS[m[1].toLowerCase()];
      if (mon) {
        let year = +m[3];
        if (year < 100) year = year < 70 ? 2000 + year : 1900 + year;
        return year + '-' + pad2(mon) + '-' + pad2(+m[2]);
      }
    }

    // YYYY-Mon-DD
    m = s.match(/^(\d{4})[\s\-\/]([A-Za-z]+)[\s\-\/](\d{1,2})$/);
    if (m) {
      const mon = MONTHS[m[2].toLowerCase()];
      if (mon) return m[1] + '-' + pad2(mon) + '-' + pad2(+m[3]);
    }

    return null;
  }

  // Parse BNZ amount strings: "-145.20", "(145.20)", "+50.00", "$1,234.56", " 145.20 "
  function parseAmount(s) {
    if (s == null) return NaN;
    let str = String(s).trim().replace(/[\$,\s]/g, '');
    let neg = false;
    const paren = str.match(/^\((.+)\)$/);
    if (paren) { neg = true; str = paren[1]; }
    const n = parseFloat(str);
    if (isNaN(n)) return NaN;
    return neg ? -n : n;
  }

  // Detect the header row inside a BNZ export. BNZ prefixes the data with metadata
  // lines like account name, date range, etc. We scan rows looking for one that has
  // BOTH a date-like header AND an amount-like header.
  function findHeaderRow(rows) {
    const dateRe = /^(date|transaction date|posted date)$/i;
    const amtRe  = /^(amount|value)$/i;
    for (let i = 0; i < Math.min(rows.length, 30); i++) {
      const cells = rows[i].map(c => (c||'').trim());
      const hasDate = cells.some(c => dateRe.test(c));
      const hasAmt  = cells.some(c => amtRe.test(c));
      if (hasDate && hasAmt) return i;
    }
    return -1;
  }

  function parseCSVToRows(text, sourceLabel) {
    const rows = parseCSV(text).filter(r => r.length > 0 && r.some(c => (c||'').trim() !== ''));
    if (rows.length < 2) return { rows: [], error: 'CSV is empty or has only one row.' };
    const headerIdx = findHeaderRow(rows);
    if (headerIdx < 0) return { rows: [], error: 'Could not find Date and Amount columns in this CSV.' };
    const headers = rows[headerIdx];
    const iDate  = findColIdx(headers, 'Date', 'Transaction Date', 'Posted Date');
    const iAmt   = findColIdx(headers, 'Amount', 'Value');
    const iPayee = findColIdx(headers, 'Payee', 'Other Party', 'Merchant');
    const iDesc  = findColIdx(headers, 'Particulars', 'Reference', 'Memo', 'Description', 'Code');
    const result = [];
    for (let r = headerIdx + 1; r < rows.length; r++) {
      const row = rows[r];
      if (row.filter(c => (c||'').trim()).length < 2) continue;
      const date   = normaliseDate(row[iDate]);
      const amount = parseAmount(row[iAmt]);
      if (!date || isNaN(amount) || amount === 0) continue;
      const payee = (iPayee >= 0 ? (row[iPayee]||'') : '').trim() ||
                    (iDesc  >= 0 ? (row[iDesc] ||'') : '').trim() || '(unknown)';
      const description = (iDesc >= 0 && iDesc !== iPayee ? (row[iDesc]||'').trim() : '');
      result.push({ date, payee, amount, description, source: sourceLabel || '' });
    }
    return { rows: result };
  }

  // Detect account from filename first, then fall back to CSV metadata rows.
  // Filename prefixes: normalise to lowercase letters only and check each known account.
  function detectAccountFromText(text, filename) {
    const accounts = ['Living Well', 'Discretionary', 'Essentials', 'Accruals', 'Savings'];
    // Filename check — strip extension, normalise to lowercase letters only, try prefix match
    if (filename) {
      const norm = filename.replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z]/g, '');
      for (const acct of accounts) {
        const acctNorm = acct.toLowerCase().replace(/[^a-z]/g, '');
        if (norm.startsWith(acctNorm) || acctNorm.startsWith(norm.slice(0, Math.max(3, acctNorm.length)))) return acct;
      }
    }
    // File content check — scan all rows for a match
    const rows = parseCSV(text);
    const headerIdx = findHeaderRow(rows);
    const limit = headerIdx >= 0 ? headerIdx : Math.min(rows.length, 15);
    for (let i = 0; i < limit; i++) {
      const line = rows[i].join(' ');
      for (const acct of accounts) {
        if (line.toLowerCase().includes(acct.toLowerCase())) return acct;
      }
    }
    return null;
  }

  // Extract closing balance and most-recent date from a BNZ CSV.
  // BNZ exports newest-first, so the first data row has the current balance.
  function extractCSVClosingBalance(text) {
    const rows = parseCSV(text).filter(r => r.some(c => (c||'').trim()));
    const headerIdx = findHeaderRow(rows);
    if (headerIdx < 0 || headerIdx + 1 >= rows.length) return { balance: null, date: null };
    const headers = rows[headerIdx];
    const iBalance = findColIdx(headers, 'Balance');
    const iDate    = findColIdx(headers, 'Date', 'Transaction Date', 'Posted Date');
    const firstDataRow = rows[headerIdx + 1];
    const balance = iBalance >= 0 ? parseAmount(firstDataRow[iBalance]) : null;
    const date    = iDate    >= 0 ? normaliseDate(firstDataRow[iDate])   : null;
    return { balance: (balance != null && !isNaN(balance)) ? balance : null, date };
  }

  function openCSVImportDialog() {
    if (document.getElementById('_csvImportDlg')) return;
    const dlg = document.createElement('div');
    dlg.id = '_csvImportDlg';
    dlg.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif;padding:16px';
    const cs = 'background:var(--surface,#fff);color:var(--text,#1a1a1a)';
    const inputCs = 'background:var(--input-bg,#fff);color:var(--text,#1a1a1a);border:1px solid var(--border2,#d1d5db)';
    const acctOptions = ['Living Well','Discretionary','Essentials','Accruals','Savings']
      .map(a => '<option value="' + a + '">' + a + '</option>').join('');

    dlg.innerHTML =
      '<div style="' + cs + ';border-radius:12px;padding:22px;width:600px;max-width:100%;max-height:90vh;display:flex;flex-direction:column;gap:0;box-shadow:0 24px 48px rgba(0,0,0,.35)">' +

      '<div id="_csvStep1">' +
        '<div style="font-weight:700;font-size:15px;margin-bottom:2px">Import Historical CSV</div>' +
        '<div style="font-size:12px;color:var(--text4,#6b7280);margin-bottom:14px">Import historical BNZ CSV exports. For ongoing imports, use Import from Akahu instead.</div>' +
        '<label for="_csvFileInput" style="display:flex;align-items:center;justify-content:center;padding:28px;border:2px dashed var(--border2,#d1d5db);border-radius:8px;cursor:pointer;gap:8px;font-size:13px;color:var(--text4,#6b7280);transition:border-color .15s" id="_csvDropLabel">'+
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
          'Choose CSV files or drop here' +
          '<input type="file" id="_csvFileInput" accept=".csv,text/csv" multiple style="display:none">' +
        '</label>' +
        '<div id="_csvFileList" style="margin-top:10px"></div>' +
        '<div id="_csvDlgStatus" style="margin-top:6px;font-size:12px;min-height:14px"></div>' +
        '<div style="display:flex;gap:8px;margin-top:12px">' +
          '<button id="_csvPreviewBtn" style="flex:1;padding:8px 14px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;display:none">Preview</button>' +
          '<button id="_csvCancel" style="padding:8px 14px;' + cs + ';border:1px solid var(--border2,#d1d5db);border-radius:6px;cursor:pointer;font-size:13px">Cancel</button>' +
        '</div>' +
      '</div>' +

      '<div id="_csvStep2" style="display:none;flex-direction:column;gap:10px;overflow:hidden">' +
        '<div id="_csvReviewSummary" style="font-size:13px;font-weight:600"></div>' +
        '<div style="overflow-y:auto;max-height:52vh;border:1px solid var(--border,#e5e7eb);border-radius:6px">' +
          '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
            '<thead><tr style="' + cs + ';position:sticky;top:0;border-bottom:1px solid var(--border,#e5e7eb)">' +
              '<th style="padding:6px 8px;text-align:left;color:var(--text4,#6b7280);font-size:11px;text-transform:uppercase">Date</th>' +
              '<th style="padding:6px 8px;text-align:left;color:var(--text4,#6b7280);font-size:11px;text-transform:uppercase">Payee</th>' +
              '<th style="padding:6px 8px;text-align:left;color:var(--text4,#6b7280);font-size:11px;text-transform:uppercase">Account</th>' +
              '<th style="padding:6px 8px;text-align:right;color:var(--text4,#6b7280);font-size:11px;text-transform:uppercase">Amount</th>' +
              '<th style="padding:6px 8px;text-align:center;color:var(--text4,#6b7280);font-size:11px;text-transform:uppercase">Status</th>' +
            '</tr></thead>' +
            '<tbody id="_csvReviewBody"></tbody>' +
          '</table>' +
        '</div>' +
        '<div style="display:flex;gap:8px">' +
          '<button id="_csvConfirm" style="flex:1;padding:8px 14px;background:#047857;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px"></button>' +
          '<button id="_csvBack" style="padding:8px 14px;' + cs + ';border:1px solid var(--border2,#d1d5db);border-radius:6px;cursor:pointer;font-size:13px">Back</button>' +
          '<button id="_csvCancelR" style="padding:8px 14px;' + cs + ';border:1px solid var(--border2,#d1d5db);border-radius:6px;cursor:pointer;font-size:13px">Cancel</button>' +
        '</div>' +
      '</div>' +
      '</div>';

    document.body.appendChild(dlg);
    function close() { const el = document.getElementById('_csvImportDlg'); if (el) document.body.removeChild(el); }
    dlg.addEventListener('click', e => { if (e.target === dlg) close(); });
    dlg.querySelector('#_csvCancel').addEventListener('click', close);
    dlg.querySelector('#_csvCancelR').addEventListener('click', close);

    const dedupeOf = t => t.date + '|' + parseFloat(t.amount).toFixed(2) + '|' + (t.payee||'').toLowerCase() + '|' + (t.source||'');

    // fileData: array of { name, text, detectedAcct, rows }
    dlg._fileData = [];

    function renderFileList() {
      const listEl = document.getElementById('_csvFileList');
      const previewBtn = document.getElementById('_csvPreviewBtn');
      if (!dlg._fileData.length) { listEl.innerHTML = ''; previewBtn.style.display = 'none'; return; }
      listEl.innerHTML = dlg._fileData.map((f, i) => {
        const detected = f.detectedAcct ? '' : ' style="border-color:#f59e0b"';
        return '<div style="padding:7px 0;border-bottom:1px solid var(--border3,#f3f4f6)">' +
          '<div style="display:flex;align-items:center;gap:8px;font-size:13px">' +
            '<div style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text4,#6b7280)" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + '</div>' +
            '<select data-fidx="' + i + '" style="padding:3px 6px;border-radius:5px;font-size:12px;' + inputCs + '"' + detected + '>' +
              '<option value="">— pick account —</option>' + acctOptions +
            '</select>' +
            '<span style="font-size:11px;color:var(--text4,#6b7280);white-space:nowrap">' + f.rows.length + ' rows</span>' +
          '</div>' +
        '</div>';
      }).join('');
      // Set detected values
      dlg._fileData.forEach((f, i) => {
        const sel = listEl.querySelector('select[data-fidx="' + i + '"]');
        if (sel && f.detectedAcct) sel.value = f.detectedAcct;
      });
      // Wire account-change
      listEl.querySelectorAll('select[data-fidx]').forEach(sel => {
        sel.addEventListener('change', () => {
          dlg._fileData[+sel.dataset.fidx].detectedAcct = sel.value;
          sel.style.borderColor = sel.value ? '' : '#f59e0b';
        });
      });
      previewBtn.style.display = 'block';
    }

    function readFiles(files) {
      const statusEl = document.getElementById('_csvDlgStatus');
      statusEl.textContent = 'Reading ' + files.length + ' file' + (files.length > 1 ? 's' : '') + '…';
      dlg._fileData = [];
      let remaining = files.length;
      Array.from(files).forEach(file => {
        const reader = new FileReader();
        reader.onload = function() {
          const text = reader.result;
          const detected = detectAccountFromText(text, file.name);
          const parsed = parseCSVToRows(text, detected || '');
          const cb = extractCSVClosingBalance(text);
          dlg._fileData.push({ name: file.name, text, detectedAcct: detected, rows: parsed.rows || [], closingBalance: cb.balance, closingDate: cb.date });
          remaining--;
          if (remaining === 0) {
            dlg._fileData.sort((a, b) => a.name.localeCompare(b.name));
            statusEl.textContent = '';
            renderFileList();
          }
        };
        reader.readAsText(file);
      });
    }

    const csvFileInput = dlg.querySelector('#_csvFileInput');
    csvFileInput.addEventListener('change', () => { if (csvFileInput.files.length) readFiles(csvFileInput.files); csvFileInput.value = ''; });

    // Drag-and-drop on the label
    const dropLabel = dlg.querySelector('#_csvDropLabel');
    dropLabel.addEventListener('dragover', e => { e.preventDefault(); dropLabel.style.borderColor = '#2563eb'; });
    dropLabel.addEventListener('dragleave', () => { dropLabel.style.borderColor = ''; });
    dropLabel.addEventListener('drop', e => { e.preventDefault(); dropLabel.style.borderColor = ''; if (e.dataTransfer.files.length) readFiles(e.dataTransfer.files); });

    function buildPreview() {
      const allRows = [];
      for (const f of dlg._fileData) {
        const acct = f.detectedAcct || '';
        f.rows.forEach(r => allRows.push(Object.assign({}, r, { source: acct })));
      }
      const existingCount = {};
      (state.transactions||[]).forEach(t => { const k = dedupeOf(t); existingCount[k] = (existingCount[k]||0) + 1; });
      const batchSeen = {};
      let newCount = 0, dupCount = 0;
      const classified = allRows.map(row => {
        const k = dedupeOf(row);
        batchSeen[k] = (batchSeen[k]||0) + 1;
        const isDup = batchSeen[k] <= (existingCount[k]||0);
        isDup ? dupCount++ : newCount++;
        return Object.assign({}, row, { isDup });
      });
      document.getElementById('_csvReviewBody').innerHTML = classified.map(r => {
        const amtStr = (r.amount < 0 ? '−' : '+') + '$' + Math.abs(r.amount).toLocaleString('en-NZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const statusHtml = r.isDup
          ? '<span style="font-size:10px;padding:1px 6px;border-radius:999px;background:#f3f4f6;color:#6b7280">dup</span>'
          : '<span style="font-size:10px;padding:1px 6px;border-radius:999px;background:#d1fae5;color:#065f46">new</span>';
        return '<tr style="border-bottom:1px solid var(--border3,#f3f4f6);' + (r.isDup ? 'opacity:0.45' : '') + '">' +
          '<td style="padding:5px 8px;color:var(--text4,#6b7280);white-space:nowrap">' + r.date + '</td>' +
          '<td style="padding:5px 8px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(r.payee) + '</td>' +
          '<td style="padding:5px 8px;font-size:11px;color:var(--text4,#6b7280);white-space:nowrap">' + escapeHtml(r.source || '—') + '</td>' +
          '<td style="padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums;color:' + (r.amount < 0 ? '#b91c1c' : '#047857') + '">' + amtStr + '</td>' +
          '<td style="padding:5px 8px;text-align:center">' + statusHtml + '</td>' +
          '</tr>';
      }).join('');
      const sources = [...new Set(classified.map(r => r.source).filter(Boolean))];
      document.getElementById('_csvReviewSummary').innerHTML =
        '<span style="color:#047857">' + newCount + ' new</span>' +
        (dupCount ? ' · <span style="color:var(--text4,#6b7280)">' + dupCount + ' duplicates</span>' : '') +
        ' — ' + classified.length + ' total' +
        (sources.length ? ' <span style="color:var(--text4,#6b7280);font-weight:400">(' + sources.join(', ') + ')</span>' : '');
      const confirmBtn = document.getElementById('_csvConfirm');
      confirmBtn.textContent = newCount > 0 ? 'Import ' + newCount + ' transaction' + (newCount === 1 ? '' : 's') : 'Nothing to import';
      confirmBtn.style.background = newCount > 0 ? '#047857' : '#9ca3af';
      confirmBtn.disabled = newCount === 0;
      dlg._allRows = classified;

      document.getElementById('_csvStep1').style.display = 'none';
      document.getElementById('_csvStep2').style.display = 'flex';
    }

    dlg.querySelector('#_csvPreviewBtn').addEventListener('click', function() {
      const missing = dlg._fileData.filter(f => !f.detectedAcct);
      if (missing.length) {
        document.getElementById('_csvDlgStatus').innerHTML = '<span style="color:#b91c1c;">Please select an account for: ' + missing.map(f => escapeHtml(f.name)).join(', ') + '</span>';
        return;
      }
      buildPreview();
    });

    dlg.querySelector('#_csvBack').addEventListener('click', function() {
      document.getElementById('_csvStep2').style.display = 'none';
      document.getElementById('_csvStep1').style.display = 'block';
    });

    dlg.querySelector('#_csvConfirm').addEventListener('click', function() {
      try {
        const rows = dlg._allRows.filter(r => !r.isDup);
        const importDate = todayISO();
        rows.forEach(function(row) {
          const overrideKey = payeeKey(row.payee);
          let category = state.payeeOverrides[overrideKey] || autoCategorise(row.payee, row.description || '');
          if (row.amount > 0 && category === 'Other') category = 'Income';
          state.transactions.push({
            id: 'tx_' + Date.now() + '_' + Math.random().toString(36).slice(2,8),
            date: row.date, amount: row.amount, payee: row.payee,
            description: row.description || '', category,
            source: row.source || '', importedAt: importDate
          });
        });
        const sources = [...new Set(rows.map(r => r.source).filter(Boolean))];
        sources.forEach(s => { if (!state.sources.includes(s)) state.sources.push(s); });
        state.lastImport = { date: importDate, count: rows.length, source: sources.join(', ') };
        saveState(); render();
        const statusEl2 = document.getElementById('importBNZTxStatus2');
        if (statusEl2) statusEl2.innerHTML = '<span style="color:#047857;">&#10003; ' + rows.length + ' added from ' + (sources.length ? sources.join(', ') : 'CSV') + '</span>';
      } finally { close(); }
    });
  }


  async function syncGtkPrice() {
    if (!location.protocol.startsWith('http')) return;
    try {
      const res = await fetch(API + '/share-price?symbol=GTK.NZ');
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'HTTP ' + res.status);
      if (typeof data.price === 'number' && data.price > 0 && data.price !== state.gentrack_price) {
        state.gentrack_price = data.price;
        saveState(); render();
      }
    } catch (_) {}
  }

  // ========== Controls ==========
  const MANUAL_FIELDS = [
    ['b1_td6', 'BNZ 6-month term deposit'], ['b1_td12', 'BNZ 12-month term deposit'],
    ['dvrp_net', 'DVRP net value'], ['gentrack_shares', 'Gentrack share count'],
    ['property_nottingham', 'Nottingham property estimate'],
    ['westpac_td_jun18', 'Westpac term deposit 1'], ['westpac_td_jun20', 'Westpac term deposit 2'],
    ['b2_peak', 'Bridge bucket high-water mark'], ['b3_peak', 'Long-term bucket high-water mark']
  ];
  function renderManualStatus() {
    setText('manualUpdated', state.manualUpdatedAt ? 'Manual values updated ' + new Date(state.manualUpdatedAt).toLocaleDateString('en-NZ') : 'Manual values — last update not recorded');
  }
  function openManualEditor() {
    const form = document.getElementById('manualForm');
    form.innerHTML = MANUAL_FIELDS.map(([key, label]) => '<label>' + label + '<input required type="number" min="0" step="any" name="' + key + '" value="' + (+state[key] || 0) + '"></label>').join('');
    document.getElementById('manualError').textContent = '';
    document.getElementById('manualDialog').showModal();
  }
  function saveManualHoldings() {
    const form = document.getElementById('manualForm');
    if (!form.reportValidity()) return;
    const values = {};
    for (const [key] of MANUAL_FIELDS) {
      const value = Number(form.elements.namedItem(key).value);
      if (!Number.isFinite(value) || value < 0) return;
      values[key] = value;
    }
    Object.assign(state, values);
    state.manualUpdatedAt = new Date().toISOString();
    const date = todayISO();
    const t = totalsFromState();
    state.snapshots = (state.snapshots || []).filter(s => s.date !== date);
    state.snapshots.push({date, b1_float:state.b1_float, b1_td6:state.b1_td6, b1_td12:state.b1_td12, b2:t.b2, b3:t.b3, ks:t.ks, nwExtra:snapshotNwExtra()});
    saveState(); render(); document.getElementById('manualDialog').close();
  }

  // Defensive helper — attach a listener only if the element exists.
  function on(id, evt, fn) { const el = document.getElementById(id); if (el) el.addEventListener(evt, fn); }

  on('txFilter', 'change', renderTransactions);
  on('txCatFilter', 'change', renderTransactions);
  on('txTypeFilter', 'change', renderTransactions);
  on('txSrcFilter', 'change', renderTransactions);
  on('catPeriod', 'change', updateCharts);
  on('bulkScope', 'change', renderBulkPayees);
  on('bulkPeriod', 'change', renderBulkPayees);
  on('bulkSrcFilter', 'change', renderBulkPayees);
  on('topPeriod', 'change', renderTopPayees);
  on('showNetWorth', 'change', updateCharts);
  on('baselineInput', 'change', () => {
    const v = document.getElementById('baselineInput').value;
    if (!v) return;
    state.baselineDate = v;
    saveState();
    render();
  });
  on('baselineResetBtn', 'click', () => {
    delete state.baselineDate;
    saveState();
    render();
  });

  on('payAnchorInput', 'change', () => {
    const v = document.getElementById('payAnchorInput').value;
    if (!v) return;
    state.payAnchorDate = v;
    saveState();
    render();
  });

  // "Review now" on the needs-categorising banner → reveal tools, open Categorise-by-payee, scroll.
  on('needCatReview', 'click', () => {
    document.getElementById('transactionReview').open = true;
    const bulk = document.getElementById('bulkDetails');
    if (bulk) { bulk.open = true; bulk.dataset.userToggled = '1'; bulk.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  });



  // (digestDismiss handler removed — Weekly review is always visible now)

  // Copy LLM prompt for weekly AI advice
  on('copyLLMPromptBtn', 'click', async () => {
    const status = document.getElementById('copyLLMStatus');
    try {
      const prompt = buildLLMPrompt();
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(prompt);
      } else {
        // Fallback for older browsers
        const ta = document.createElement('textarea');
        ta.value = prompt; ta.style.position = 'fixed'; ta.style.left = '-9999px';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      if (status) {
        status.textContent = '✓ Copied ' + prompt.length.toLocaleString() + ' chars — paste into Claude or ChatGPT.';
        status.style.color = '#047857';
        setTimeout(() => { status.textContent = ''; }, 8000);
      }
    } catch (err) {
      if (status) {
        status.textContent = 'Copy failed: ' + (err.message || err);
        status.style.color = '#b91c1c';
      }
    }
  });
  on('monthPrev', 'click', () => { selectedMonthOffset--; renderCurrentMonthByCategory(); });
  on('monthNext', 'click', () => { selectedMonthOffset++; renderCurrentMonthByCategory(); });
  on('monthToday', 'click', () => { selectedMonthOffset = 0; renderCurrentMonthByCategory(); });

  on('bulkDetails', 'toggle', () => {
    const d = document.getElementById('bulkDetails');
    if (d) d.dataset.userToggled = '1';
  });



  // ========== Akahu (live bank balances) ==========
  async function fetchAkahuAccounts() {
    // Fetch via local proxy (akahu-proxy.py) to avoid CORS restrictions from file://
    let resp;
    try {
      resp = await fetch(API + '/accounts');
    } catch (e) {
      throw new Error('Bank connection unavailable. Try again shortly.');
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || 'Proxy returned HTTP ' + resp.status);
    }
    const data = await resp.json();
    return data.items || [];
  }

  function fmtBal(n) {
    if (typeof n !== 'number') return '—';
    return '$' + n.toLocaleString('en-NZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }



  function syncAkahuToState(accounts) {
    if (!accounts || !accounts.length) return;
    const today = todayISO();
    const prevCachedAccounts = state.cachedAkahuAccounts || [];
    state.cachedAkahuAccounts = accounts;
    // Plan bucket fields (B1 float, B2, B3, KiwiSaver, Westpac TDs)
    // Only update state when Akahu's value has actually changed since the last fetch.
    // If Akahu returns the same balance as cached, treat it as no new data — skip the write
    // so any manually-entered value (e.g. a pending transfer not yet settled in Akahu) is preserved.
    // For Simplicity, Akahu's balance.current lags the unit price by a day.
    // The portfolio metadata's shares × price is current, so prefer that when available.
    // For Simplicity, balance.current lags the unit price by a day, so prefer shares × price.
    // Exception: funds flagged useCurrent (the Conservative/Cash fund) report a stale unit price,
    // so use balance.current for those.
    const akahuValue = (a, useCurrent) => {
      const cur = (a.balance && typeof a.balance.current === 'number') ? a.balance.current : null;
      if (!useCurrent) {
        const port = ((a.meta || {}).portfolio || [])[0];
        if (port && typeof port.shares === 'number' && typeof port.price === 'number') {
          // A wound-down fund keeps its stale share count in the portfolio metadata long after
          // the money has left: in Sep 2026 Growth + Balanced reported $2.74M of units against
          // a $0 balance, and marked them to market daily so the phantom looked live. A zero
          // balance.current is authoritative — never let shares x price resurrect an empty fund.
          if (cur === 0) return 0;
          return port.shares * port.price;
        }
      }
      return cur;
    };
    AKAHU_SYNC_MAP.forEach(m => {
      const a = accounts.find(x => (x.connection && x.connection.name) === m.connection && x.name === m.name);
      if (a) {
        const akahuVal = akahuValue(a, m.useCurrent);
        if (akahuVal === null) return;
        const prev = prevCachedAccounts.find(x => x._id === a._id);
        const prevVal = prev ? akahuValue(prev, m.useCurrent) : null;
        // One-time migration: if state was last synced from balance.current (old logic) and
        // now differs from shares × price (new logic), accept the migration.
        const stateVal = +state[m.stateKey] || 0;
        const prevRaw = prev && prev.balance && typeof prev.balance.current === 'number' ? prev.balance.current : null;
        const stateMatchesOldRaw = prevRaw !== null && Math.abs(stateVal - prevRaw) < 0.01;
        // Sync only if Akahu has new data (value changed), we have no prior reading,
        // or the state still holds the pre-migration balance.current value.
        if (prevVal === null || akahuVal !== prevVal || stateMatchesOldRaw) {
          state[m.stateKey] = akahuVal;
        }
      }
    });
    // Fund switch in transit — drop the entry once the destination has essentially all of it
    // (98% tolerates unit-price drift between the sell and the buy).
    if (state.switch_pending) {
      const sp = state.switch_pending;
      const arrived = Math.max(0, (+state[sp.to]||0) - (+sp.toBaseline||0));
      if (arrived >= (+sp.amount||0) * 0.98) state.switch_pending = null;
    }
    // B2 in-transit: b2PendingRemaining() already decays the pending as money lands; once
    // virtually all of it has arrived (98% — tolerates market drift), drop the entry entirely.
    if (state.b2_pending) {
      const landed = (+state.conservative_balance||0) + (+state.b2_balance||0) + (+state.b2_cash||0);
      if (landed >= (+state.b2_pending.baseline||0) + (+state.b2_pending.amount||0) * 0.98) {
        state.b2_pending = null;
      }
    }
    // BNZ account actual balances for the fortnightly flow tracker
    if (!state.acctActualBalance) state.acctActualBalance = {};
    BNZ_ACCOUNTS.forEach(m => {
      const a = accounts.find(x => (x.connection && x.connection.name) === 'BNZ' && x.name === m.akahu);
      if (a && a.balance && typeof a.balance.current === 'number') {
        state.acctActualBalance[m.plan] = { amount: a.balance.current, asOf: today };
      }
    });
    // Snapshot — replaces any existing entry for today
    const snap = { date: today, b1_float: +state.b1_float||0, b1_td6: +state.b1_td6||0, b1_td12: +state.b1_td12||0, b2: totalsFromState().b2, b3: totalsFromState().b3, ks: +state.ks_balance||0, nwExtra: snapshotNwExtra() };
    state.snapshots = (state.snapshots||[]).filter(s => s.date !== today);
    if (!(state.deletedSnapshotDates || []).includes(today)) state.snapshots.push(snap);
    saveState(); render();
  }

  // (doForceRefresh / doAkahuSync removed — consolidated into runSync, the single sync path.)

  // Build lookup: Akahu account name → canonical source label
  const AKAHU_ACCT_SOURCE = Object.fromEntries(BNZ_ACCOUNTS.map(a => [a.akahu, a.plan]));


  async function importAkahuTransactions() {
    const acctMap = {};
    akahuAccounts.forEach(a => { acctMap[a._id] = a; });
    const existingDates = (state.transactions||[]).map(t => t.date).filter(Boolean).sort();
    const latestDate = existingDates.length ? existingDates[existingDates.length - 1] : null;
    let startDate = null;
    if (latestDate) {
      const d = new Date(latestDate + 'T00:00:00');
      d.setDate(d.getDate() - 3);
      startDate = d.getFullYear() + '-' + pad2(d.getMonth()+1) + '-' + pad2(d.getDate());
    }
    const resp = await fetch(API + '/transactions' + (startDate ? '?start=' + startDate : ''));
    if (!resp.ok) throw new Error('Proxy returned HTTP ' + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    const importDate = todayISO();
    const dedupeKey = t => t.date + '|' + parseFloat(t.amount).toFixed(2) + '|' + (t.payee||'').toLowerCase() + '|' + (t.source||'');
    // The fuzzy key exists only to avoid re-importing CSV-era rows that predate Akahu ids.
    // Akahu rows dedupe on _id alone — matching the fuzzy key against other Akahu rows
    // collapsed two genuine same-day identical purchases (e.g. two Anthropic charges).
    const existingFuzzy = new Set((state.transactions||[])
      .filter(t => !String(t.id||'').startsWith('akahu_')).map(dedupeKey));
    const existingIds = new Set((state.transactions||[]).map(t => t.id).filter(Boolean));
    let added = 0;
    (data.items || []).forEach(tx => {
      const acct = acctMap[tx._account];
      if (!acct || (acct.connection && acct.connection.name) !== 'BNZ') return;
      const source = AKAHU_ACCT_SOURCE[acct.name];
      if (!source) return;
      const date = (tx.date || '').slice(0, 10);
      if (!date) return;
      const amount = typeof tx.amount === 'number' ? tx.amount : parseFloat(tx.amount);
      if (isNaN(amount) || amount === 0) return;
      const payee = (tx.merchant && tx.merchant.name) || tx.description || '(unknown)';
      const row = { date, amount, payee, description: tx.description || '', source };
      if (existingIds.has('akahu_' + tx._id) || existingFuzzy.has(dedupeKey(row))) return;
      const category = state.payeeOverrides[payeeKey(payee)] || autoCategorise(payee, tx.description || '');
      state.transactions.push({ id: 'akahu_' + tx._id, date, amount, payee, description: tx.description || '', category, source, importedAt: importDate });
      existingIds.add('akahu_' + tx._id);
      added++;
    });
    if (added > 0) {
      const sources = [...new Set(state.transactions.filter(t => t.importedAt === importDate).map(t => t.source).filter(Boolean))];
      sources.forEach(s => { if (!state.sources.includes(s)) state.sources.push(s); });
      state.lastImport = { date: importDate, count: added, source: 'Akahu' };
      saveState(); render();
    }
    return added;
  }

  function updateLastSyncDisplay() {
    const el = document.getElementById('lastSyncPill');
    if (!el) return;
    // Successful background checks need no persistent header status.
    if (!el.dataset.failed) el.style.display = 'none';
  }

  // Hourly background sync — same canonical path as page-load / pill-click.
  setInterval(() => { runSync(); }, 60 * 60 * 1000);

  updateLastSyncDisplay();

  // Tabs
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === t.dataset.tab));
      // Force chart resize for the newly visible tab
      setTimeout(() => { if (chartHistory) chartHistory.resize(); if (chartCum) chartCum.resize(); if (chartCat) chartCat.resize(); }, 50);
    });
  });

  function exportBackup() {
    downloadBackup(cleanState(JSON.parse(JSON.stringify(state))), 'financial-plan-data.json');
  }
  function downloadBackup(data, name) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = name;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  on('exportBackupBtn', 'click', exportBackup);

  // Import backup → load a JSON file and replace current state.
  // No programmatic .click() — the visible label opens the picker.
  // No confirm/alert dialogs — they may be blocked in the sandboxed iframe.
  // Feedback is surfaced through the green "migrate" banner instead.
  function showImportMessage(html, isError) {
    const b = document.getElementById('migrateBanner');
    b.style.background = isError ? '#fee2e2' : '#d1fae5';
    b.style.borderColor = isError ? '#fca5a5' : '#6ee7b7';
    b.style.color = isError ? '#7f1d1d' : '#065f46';
    b.innerHTML = html;
    b.classList.add('show');
    b.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  document.getElementById('backupFileInput2').addEventListener('change', e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onerror = () => showImportMessage('<strong>Couldn\'t read file:</strong> ' + escapeHtml(reader.error ? reader.error.message : 'unknown error'), true);
    reader.onload = () => {
      try {
        const parsed = JSON.parse(reader.result);
        if (!parsed || typeof parsed !== 'object') throw new Error('Not a valid backup file (expected a JSON object).');
        // Quick sanity check: does it look like our schema?
        const looksLikeBackup = parsed.hasOwnProperty('b1_float') || parsed.hasOwnProperty('transactions') || parsed.hasOwnProperty('snapshots') || parsed.hasOwnProperty('categoryBudgets');
        if (!looksLikeBackup) throw new Error('File doesn\'t look like a Financial Plan backup — no recognised fields found.');
        validateBackup(parsed);
        if (!Array.isArray(parsed.transactions) || !Array.isArray(parsed.snapshots) || !parsed.payeeOverrides || typeof parsed.payeeOverrides !== 'object') throw new Error('Backup is missing transactions, snapshots, or learned rules.');
        pendingBackup = cleanState(applyMigrations(mergeWithDefaults(parsed)));
        setText('backupPreviewText', pendingBackup.transactions.length + ' transactions and ' + pendingBackup.snapshots.length + ' snapshots. Restoring replaces current records; an export of your current state will be downloaded first.');
        document.getElementById('backupPreview').showModal();
        return;
      } catch(err) {
        showImportMessage('<strong>Couldn\'t import that file:</strong> ' + escapeHtml(err.message), true);
      }
    };
    reader.readAsText(file);
  });

  function validateBackup(parsed) {
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid backup');
    for (const key of ['categoryAnnualForecast', 'categoryAccount', 'accountFortnightly', 'categorySpread', 'migrations']) {
      if (parsed[key] != null && (typeof parsed[key] !== 'object' || Array.isArray(parsed[key]))) throw new Error('Invalid ' + key);
    }
    for (const tx of parsed.transactions || []) {
      if (!tx || typeof tx.id !== 'string' || !tx.id || !/^\d{4}-\d{2}-\d{2}$/.test(tx.date) || !Number.isFinite(tx.amount)) throw new Error('Invalid transaction in backup');
    }
    for (const snap of parsed.snapshots || []) {
      if (!snap || !/^\d{4}-\d{2}-\d{2}$/.test(snap.date)) throw new Error('Invalid snapshot in backup');
    }
  }
  let pendingBackup = null;
  on('confirmBackupImport', 'click', () => {
    if (!pendingBackup) return;
    exportBackup(); state = pendingBackup; pendingBackup = null;
    saveState(); render(); document.getElementById('backupPreview').close();
    if (syncClient.revision === 0) syncClient.initialise();
  });
  on('cancelBackupImport', 'click', () => { pendingBackup = null; document.getElementById('backupPreview').close(); });

  async function onResume() {
    await syncFromServer();
    if (!syncClient.ready || syncClient.conflict) return;
    const last = state.akahuLastFetch ? Date.parse(state.akahuLastFetch) : 0;
    if (Date.now() - last > 10 * 60 * 1000) runSync();
  }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') onResume(); });
  window.addEventListener('pageshow', e => { if (e.persisted) onResume(); });
  window.addEventListener('online', onResume);

  // Sync: visible status, immediate fetch, then poll as banks respond (they can be slow). Runs on
  // load and on demand (click the sync pill). Guards against overlapping runs.
  let _syncing = false;
  // The single sync path — used on page load, sync-pill click, and the hourly interval.
  // Balances + transactions in three passes (immediate, then after the bank /refresh responds),
  // plus sticky "money arrived" alerts for Westpac credits landing.
  async function runSync() {
    if (_syncing || !syncClient || !syncClient.ready || syncClient.conflict) return;
    _syncing = true;
    const pill = document.getElementById('lastSyncPill');
    const setPill = (txt, failed = false) => { if (pill) { pill.style.display = failed ? '' : 'none'; pill.dataset.failed = failed ? '1' : ''; pill.textContent = txt; } };
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    // Snapshot balances before sync so we can detect new money arriving.
    const prevBalMap = {};
    (state.cachedAkahuAccounts || []).forEach(a => {
      prevBalMap[a._id] = a.balance && typeof a.balance.current === 'number' ? a.balance.current : null;
    });
    const fetchSync = async () => {
      try {
        akahuAccounts = await fetchAkahuAccounts();
        syncAkahuToState(akahuAccounts);
        await importAkahuTransactions();   // pull new transactions, not just balances
        await syncGtkPrice();
        state.akahuLastFetch = new Date().toISOString();
        saveState();
        return true;
      } catch (e) { return false; }
    };
    setPill('Syncing…');
    let ok = await fetchSync();
    let refreshed = false;
    try {
      const response = await fetch(API + '/refresh', {method:'POST', headers:{'X-Finance-Client':'2.0.3'}});
      const result = await response.json(); refreshed = response.ok && result.success;
      if (refreshed) { state.lastBackgroundRefresh = new Date().toISOString(); saveState(); }
    } catch (_) {}
    await sleep(8000);  await fetchSync();               // first pass as banks respond
    await sleep(8000);  ok = (await fetchSync()) || ok;      // catch slower banks
    // BNZ via Akahu typically takes 60–90 s to report after /refresh — the early passes
    // only catch the fast case. Keep polling to ~90 s so fresh balances actually land
    // this visit (the payday card computes transfer amounts from them).
    setPill('Syncing… waiting for bank');
    await sleep(30000); ok = (await fetchSync()) || ok;
    await sleep(45000); ok = (await fetchSync()) || ok;
    // Sticky alert when a Westpac (non-TD) account goes from empty to funded — money has landed.
    if (ok && Array.isArray(akahuAccounts)) {
      akahuAccounts.forEach(a => {
        const conn = a.connection && a.connection.name;
        const next = a.balance && typeof a.balance.current === 'number' ? a.balance.current : null;
        const prev = Object.prototype.hasOwnProperty.call(prevBalMap, a._id) ? prevBalMap[a._id] : null;
        if (next === null) return;
        if (!state.westpacAlerts) state.westpacAlerts = [];
        if (conn === 'Westpac' && a.type !== 'TERMDEPOSIT' && next > 0 && (prev === 0 || prev === null)) {
          // Westpac: sticky "money arrived" when an empty account gets funded.
          if (!state.westpacAlerts.some(x => x.id === a._id)) {
            state.westpacAlerts.push({ id: a._id, name: a.name || 'Westpac', kind: 'arrived', amount: next, at: new Date().toISOString() });
          }
        }
        // Simplicity balance-change banners retired 2026-07-09 — fund movement now arrives as
        // week-over-week deltas in the Monday weekly digest email instead.
      });
      saveState(); safeCall('renderWestpacAlerts', renderWestpacAlerts);
    }
    setPill(ok ? 'Bank refresh unavailable · Retry' : 'Bank unavailable · Retry', !ok || !refreshed);
    _syncing = false;
  }
  { const pill = document.getElementById('lastSyncPill'); if (pill) pill.addEventListener('click', runSync); }
  buildCatFilter();
  syncClient = new FinanceSync.Client({
    api: API, initial: cleanState(state), storage: localStorage, key: 'finance-sync-v1', fetch: window.fetch.bind(window),
    onStatus: setSyncStatus,
    onState: incoming => {
      const before = JSON.stringify(incoming);
      state = cleanState(applyMigrations(mergeWithDefaults(incoming)));
      document.getElementById('appContent').inert = false;
      document.getElementById('syncConflict').hidden = true;
      render();
      if (JSON.stringify(state) !== before) saveState();
    },
    onEmpty: () => {
      document.getElementById('appContent').inert = false;
      setSyncStatus('No saved data on Home Assistant — import a backup in Settings', true);
    },
    onConflict: paths => {
      setText('conflictText', 'Another device changed the same records (' + paths.length + '). Your local changes are kept until you choose.');
      document.getElementById('syncConflict').hidden = false;
    }
  });
  state = cleanState(syncClient.local);
  document.getElementById('appContent').inert = true;
  on('retrySave', 'click', () => syncFromServer());
  on('conflictRemote', 'click', () => { exportBackup(); syncClient.resolve('remote'); });
  on('conflictLocal', 'click', () => syncClient.resolve('local'));
  on('openManualEditor', 'click', openManualEditor);
  on('saveManualHoldings', 'click', saveManualHoldings);
  on('cancelManualHoldings', 'click', () => document.getElementById('manualDialog').close());
  on('openTransactions', 'click', () => openTxPanel({filter:'all', type:'all'}));
  on('txSearch', 'input', renderTransactions);
  fetch(API + '/status').then(r => r.json()).then(status => {
    setText('bankConnectionStatus', status.bankConfigured ? 'Connected through Home Assistant' : 'Configure your bank connection in Home Assistant → Finance → Configuration');
  }).catch(() => setText('bankConnectionStatus', 'Connection status unavailable'));
  async function mergeAutoSnapshots() {
    try {
      const response = await fetch(API + '/auto-snapshots');
      if (!response.ok) return;
      const snapshots = await response.json();
      const dates = new Set(state.snapshots.map(s => s.date));
      const deleted = new Set(state.deletedSnapshotDates || []);
      let added = false;
      for (const snap of snapshots) {
        if (snap.date && !dates.has(snap.date) && !deleted.has(snap.date)) {
          state.snapshots.push(snap); dates.add(snap.date); added = true;
        }
      }
      if (added) { saveState(); render(); }
    } catch (_) {}
  }
  syncFromServer().then(async ok => { if (ok) { await mergeAutoSnapshots(); runSync(); } });
