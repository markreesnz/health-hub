/* Versioned historical compatibility. Run after the authoritative state is read. */
  function applyMigrations(s) {
    if ((s.schemaVersion || 0) >= 1) return s;
    const manualTDs = {b1_td6:s.b1_td6, b1_td12:s.b1_td12};
    // BNZ TD ladder set up Jun 2026 — update any state still at $0
    if (!s.b1_td6  || s.b1_td6  === 0) s.b1_td6  = 126153.53;
    if (!s.b1_td12 || s.b1_td12 === 0) s.b1_td12 = 126296.23;
    // Aug 2026: term deposits refreshed to actual balances (6M $125,751.13 / 12M $125,843.89).
    if (!s._tdUpdateAug2026) { s.b1_td6 = 125751.13; s.b1_td12 = 125843.89; s._tdUpdateAug2026 = true; }
    // Sep 2026: refreshed from the BNZ app. These are PIE term deposits — the balance accrues
    // and Akahu does not report them at all, so they only move when read off manually.
    // 6M $126,153.53 (matures 8 Dec 2026) · 12M $126,296.23 (matures 8 Jun 2027).
    if (!s._tdUpdateSep2026) { s.b1_td6 = 126153.53; s.b1_td12 = 126296.23; s._tdUpdateSep2026 = true; }
    // Sep 2026: non-KiwiSaver investments moved into the Simplicity Conservative Fund. Akahu
    // zeroed balance.current on Growth/Balanced/Cash but kept stale share counts, so every sync
    // from 8 Sep booked ~$2.74M of units that no longer existed while the real $3.3M in
    // Conservative went unmapped. Zero the wound-down funds and drop the poisoned snapshots;
    // they rebuild correctly from the next sync.
    // Sep 2026: $1.8M Conservative -> Balanced, seeded while Akahu still showed the pre-switch
    // balances. Self-clears in syncAkahuToState once the buy lands.
    // Sep 2026: Nottingham had been carried at its Apr 2025 purchase price for 17 months.
    // Re-base to the pessimistic sale estimate. Only overwrite the stale purchase-price anchor —
    // if a real appraisal or valuation has since been entered, leave it alone.
    // Sep 2026: DVRP had only the tax haircut applied. Add the 10/12 pro-rata for the part-year
    // worked and the 25% forfeited on leaving. Only correct the old figure — leave a manual edit.
    if (!s._dvrpProRataSep2026) {
      if (Math.abs((+s.dvrp_net||0) - 146400) < 1) s.dvrp_net = 91500;
      s._dvrpProRataSep2026 = true;
    }
    if (!s._nottinghamRebaseSep2026) {
      if (Math.abs((+s.property_nottingham||0) - 1620000) < 1) s.property_nottingham = 1310000;
      s._nottinghamRebaseSep2026 = true;
    }
    // Sep 2026: conservative_balance is only populated by an Akahu sync, so between deploying
    // the new sync map and the next successful fetch it sat at 0 — which emptied B2 entirely and
    // made switchInFlight read the whole $1.8M as already gone from the source. Seed it with the
    // Akahu balance at the time of the switch; the next sync overwrites it with live data.
    // Sep 2026: the drawdown triggers compare each bucket against a manually-held high-water
    // mark. The consolidation changed what B2 and B3 ARE — B3 is now $1.8M of Simplicity Balanced
    // bought deliberately, not a fallen Growth Fund — so b3_peak was still carrying the Growth
    // peak of $2,358,736 and reported a phantom 24% drawdown, firing "pause all refills; live on
    // B1". Prior peak history does not survive a change of vehicle; re-base both to the holdings
    // as established.
    if (!s._peakRebaseSep2026) {
      s.b3_peak = 1800000;
      s.b2_peak = 1520282;
      s._peakRebaseSep2026 = true;
    }
    if (!s._conservativeSeedSep2026) {
      if (!(+s.conservative_balance)) s.conservative_balance = 3320281.62;
      s._conservativeSeedSep2026 = true;
    }
    if (!s._switchSep2026) {
      s.switch_pending = { from: 'conservative_balance', to: 'b2_balance',
                           amount: 1800000, fromBaseline: 3320281.62, toBaseline: 0 };
      s._switchSep2026 = true;
    }
    if (!s._conservativeSep2026) {
      s.b2_balance = 0; s.b2_cash = 0; s.b3_balance = 0;
      s.snapshots = (s.snapshots || []).filter(x => !x.date || x.date < '2026-09-08');
      s._conservativeSep2026 = true;
    }
    // Aug 2026: giving categories removed. Retag any historical giving transactions to 'Other'
    // and exclude them, so they stay out of the core run-rate (they were oneOff/excluded before).
    // Also ensure the new Wine category is funded from Living Well.
    if (!s._removeGivingWineAug2026) {
      const _GIVING = new Set(['Giving — EA', 'Giving — Forest & Bird', 'Giving — Kaibosh', 'Gifts & donations']);
      (s.transactions || []).forEach(t => {
        if (_GIVING.has(t.category)) { t.category = 'Other'; t.excluded = true; }
      });
      ['Giving — EA', 'Giving — Forest & Bird', 'Giving — Kaibosh', 'Gifts & donations'].forEach(n => {
        if (s.categoryAnnualForecast) delete s.categoryAnnualForecast[n];
        if (s.categoryAccount) delete s.categoryAccount[n];
      });
      s.categoryAccount = s.categoryAccount || {};
      if (!s.categoryAccount['Wine']) s.categoryAccount['Wine'] = 'Living Well';
      s._removeGivingWineAug2026 = true;
    }
    // Jun 2026: $58,020.79 deposited to Simplicity Balanced, not yet visible via Akahu.
    // Counted in B2 until the synced balance jumps by ~the amount, then auto-cleared
    // in syncAkahuToState. null = cleared (don't re-seed).
    // b2_balance had been manually bumped to include the deposit (71,153.91) — reset it
    // to the Akahu-reported value so the pending entry carries the $58k without double-counting.
    if (s.b2_pending === undefined) {
      if (Math.abs((+s.b2_balance||0) - 71153.91) < 0.01) s.b2_balance = 13133.12;
      s.b2_pending = { amount: 58020.79, baseline: +s.b2_balance || 0 };
    }
    if (s.b2_pending && s.b2_pending.amount === 58000) s.b2_pending.amount = 58020.79;
    // Repair: if pending was seeded while b2_balance still included the deposit, undo the double-count
    if (s.b2_pending && Math.abs((+s.b2_pending.baseline||0) - 71153.91) < 0.01) {
      s.b2_balance = 13133.12;
      s.b2_pending.baseline = 13133.12;
    }
    // Giving: three $300/MONTH direct debits (EA NZ, Forest & Bird, Kaibosh) drawn from the
    // Living Well account (2026-07, "to start with"). One-shot and self-healing across earlier
    // giving generations: strip any prior giving lines, reversing whatever per-fortnight top-up
    // each added to its account, then set the new monthly split on Living Well and top that
    // account up by the $900/month (≈$415.38/fn) total. Relative bumps preserve manual overrides.
    if (!s._givingV3) {
      s.categoryAnnualForecast = s.categoryAnnualForecast || {};
      s.categoryAccount = s.categoryAccount || {};
      s.accountFortnightly = s.accountFortnightly || {};
      const _PPY = { fortnight: 26, month: 12, quarter: 4, year: 1 };
      const _GIVE = ['Giving — EA', 'Giving — Forest & Bird', 'Giving — Kaibosh'];
      if (s.categoryAnnualForecast['Giving']) {                 // undo the original single line (+923/fn on Essentials)
        s.accountFortnightly['Essentials'] = (+s.accountFortnightly['Essentials'] || 0) - 923;
        delete s.categoryAnnualForecast['Giving'];
        delete s.categoryAccount['Giving'];
      }
      _GIVE.forEach(_n => {                                      // undo any prior three-way lines on their account
        const _o = s.categoryAnnualForecast[_n], _acct = s.categoryAccount[_n];
        if (_o && _acct) {
          const _amt = (typeof _o === 'number') ? _o : (+_o.amount || 0);
          const _per = (typeof _o === 'object' && _o.period) || 'fortnight';
          s.accountFortnightly[_acct] = (+s.accountFortnightly[_acct] || 0) - _amt * (_PPY[_per] || 26) / 26;
        }
        delete s.categoryAnnualForecast[_n];
        delete s.categoryAccount[_n];
      });
      let _addFn = 0;                                            // set the new monthly split on Living Well
      _GIVE.forEach(_n => {
        s.categoryAnnualForecast[_n] = { amount: 300, period: 'month' };
        s.categoryAccount[_n] = 'Living Well';
        _addFn += 300 * 12 / 26;
      });
      s.accountFortnightly['Living Well'] = (+s.accountFortnightly['Living Well'] || 0) + _addFn;
      s._givingV3 = true;
    }
    // Aug 2026 — three changes rolled into one guarded, self-healing migration:
    //  1. Donations stopped: reverse the three recurring DDs' (EA / Forest & Bird / Kaibosh)
    //     fortnightly slice from whatever account funds them, then drop all four giving lines
    //     (incl. Gifts & donations, which had no explicit account funding). The categories are
    //     marked oneOff in CATEGORIES so historical giving stays categorised but stops forecasting.
    //  2. Essentials account closed: fold its fortnightly budget into Living Well, reassign every
    //     category paid from it to Living Well, retag its historical transactions, and drop it
    //     from sources + balance tracking.
    //  3. Quarterly water rates added on Living Well: Kensington $538/qtr, Nottingham scaled by
    //     rateable value 1,620/770 → $1,131.90/qtr.
    if (!s._essWaterAug2026) {
      s.categoryAnnualForecast = s.categoryAnnualForecast || {};
      s.categoryAccount = s.categoryAccount || {};
      s.accountFortnightly = s.accountFortnightly || {};
      const _PPY2 = { fortnight: 26, month: 12, quarter: 4, year: 1 };
      const _fnEq = (o) => {
        if (o == null) return 0;
        const amt = (typeof o === 'number') ? o : (+o.amount || 0);
        const per = (typeof o === 'object' && o.period) || 'year';
        return amt * (_PPY2[per] || 1) / 26;
      };
      // 1) Stop donations — reverse the recurring DDs' funding, then drop all four giving lines.
      ['Giving — EA', 'Giving — Forest & Bird', 'Giving — Kaibosh'].forEach(n => {
        const o = s.categoryAnnualForecast[n], acct = s.categoryAccount[n];
        if (o != null && acct) s.accountFortnightly[acct] = (+s.accountFortnightly[acct] || 0) - _fnEq(o);
      });
      ['Giving — EA', 'Giving — Forest & Bird', 'Giving — Kaibosh', 'Gifts & donations'].forEach(n => {
        delete s.categoryAnnualForecast[n];
        delete s.categoryAccount[n];
      });
      // 2) Fold the closed Essentials account into Living Well.
      s.accountFortnightly['Living Well'] = (+s.accountFortnightly['Living Well'] || 0) + (+s.accountFortnightly['Essentials'] || 0);
      delete s.accountFortnightly['Essentials'];
      Object.keys(s.categoryAccount).forEach(cat => {
        if (s.categoryAccount[cat] === 'Essentials') s.categoryAccount[cat] = 'Living Well';
      });
      (s.transactions || []).forEach(t => { if ((t.source || '') === 'Essentials') t.source = 'Living Well'; });
      if (Array.isArray(s.sources)) s.sources = s.sources.filter(x => x !== 'Essentials');
      if (s.acctActualBalance) delete s.acctActualBalance['Essentials'];
      // 3) Add water rates on Living Well. Billed quarterly (Kensington $538/qtr,
      //    Nottingham scaled by rateable value 1,620/770 → $1,131.90/qtr) but paid
      //    FORTNIGHTLY via Tiakiwai direct debit spreading the annualised bill, so
      //    they're modelled as smooth fortnightly spend: quarterly × 4 / 26 per fortnight.
      const _waterQtr = { 'Water — Kensington': 538, 'Water — Nottingham': Math.round(538 * 1620 / 770 * 100) / 100 };
      Object.entries(_waterQtr).forEach(([n, qtr]) => {
        const o = { amount: Math.round(qtr * 4 / 26 * 100) / 100, period: 'fortnight' };
        s.categoryAnnualForecast[n] = o;
        s.categoryAccount[n] = 'Living Well';
        s.accountFortnightly['Living Well'] = (+s.accountFortnightly['Living Well'] || 0) + _fnEq(o);
      });
      s._essWaterAug2026 = true;
    }
    // Aug 2026 correction — water was first shipped as a lumpy quarterly bill; it's
    // actually paid fortnightly (Tiakiwai DD spreads the annualised bill). Convert the
    // two water categories quarter → fortnight, keeping the same annual total, so the
    // Living Well budget (which already carries the fortnightly-equivalent) is unchanged.
    if (!s._waterFortnightlyAug2026) {
      s.categoryAnnualForecast = s.categoryAnnualForecast || {};
      const _waterQtr = { 'Water — Kensington': 538, 'Water — Nottingham': Math.round(538 * 1620 / 770 * 100) / 100 };
      Object.entries(_waterQtr).forEach(([n, qtr]) => {
        if (s.categoryAnnualForecast[n]) {
          s.categoryAnnualForecast[n] = { amount: Math.round(qtr * 4 / 26 * 100) / 100, period: 'fortnight' };
        }
      });
      s._waterFortnightlyAug2026 = true;
    }
    // Aug 2026 — set Nottingham water to the ACTUAL first bill: $811.94/qtr (Tiaki Wai
    // account WW901093098, 1 Jul–30 Sep 2026) → $124.91/fn. Supersedes the earlier CV-scaled
    // ($1,131.90) and $710 estimate figures. Adjust the Living Well budget by the fortnightly
    // delta computed from whatever's currently stored (self-healing across prior estimates).
    if (!s._waterNottinghamActualAug2026) {
      s.categoryAnnualForecast = s.categoryAnnualForecast || {};
      s.accountFortnightly = s.accountFortnightly || {};
      const _cur = s.categoryAnnualForecast['Water — Nottingham'];
      if (_cur) {
        const _newFn = Math.round(811.94 * 4 / 26 * 100) / 100; // 124.91
        const _PPY3 = { fortnight: 26, month: 12, quarter: 4, year: 1 };
        const _curAmt = (typeof _cur === 'number') ? _cur : (+_cur.amount || 0);
        const _curPer = (typeof _cur === 'object' && _cur.period) || 'year';
        const _curFn = _curAmt * (_PPY3[_curPer] || 1) / 26;
        const _acct = (s.categoryAccount || {})['Water — Nottingham'] || 'Living Well';
        s.accountFortnightly[_acct] = Math.round(((+s.accountFortnightly[_acct] || 0) - _curFn + _newFn) * 100) / 100;
        s.categoryAnnualForecast['Water — Nottingham'] = { amount: _newFn, period: 'fortnight' };
      }
      s._waterNottinghamActualAug2026 = true;
    }
    s.migrations = s.migrations || {};
  // Jul 2026: LTI Tranche 1 paid out — $92,520.34 net is in transit to the Simplicity Cash
  // Fund. Tracked as B2 pending (auto-clears in syncAkahuToState once the funds rise by ~the
  // amount). The old lti_tranche1_net holding is retired.
  if (!s.migrations.lti1_paid_jul2026) {
    const ltiNet = 92520.34;
    if (s.b2_pending) s.b2_pending.amount = (+s.b2_pending.amount||0) + ltiNet;
    else s.b2_pending = { amount: ltiNet, baseline: (+s.b2_balance||0) + (+s.b2_cash||0) };
    delete s.lti_tranche1_net;
    s.migrations.lti1_paid_jul2026 = true;
  }
  // Jul 2026: the LTI pending was carried at its full $92,520.34 even after ~$42.5K of it had
  // landed in the funds, overstating net worth. Re-anchor to what is actually still in transit
  // against the 2026-07-06 balances (Balanced 132,663.12 + Cash 652,462.46 = 785,125.58).
  if (!s.migrations.b2_pending_reanchor_jul2026) {
    if (s.b2_pending) s.b2_pending = { amount: 50000, baseline: 785125.58 };
    s.migrations.b2_pending_reanchor_jul2026 = true;
  }

    for (const [key, value] of Object.entries(manualTDs)) {
      if (typeof value === 'number' && Number.isFinite(value)) s[key] = value;
    }
    s.schemaVersion = 1;
    return s;
  }

